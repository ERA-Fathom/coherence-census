"""
Run deepagents' published "deep_research" example once, record every tool call it makes, and read it.

    python runner.py "<research question>"

What it does, in order:
  1. Imports the vendored example unchanged (deep_research/, see VENDORED_FROM.txt), with two wrappers
     applied before the import: langchain.chat_models.init_chat_model returns the FATHOM_MODEL model
     through OpenRouter when that variable is set (the example hardcodes claude-sonnet-4-5), and
     deepagents.create_deep_agent adds the langchain-fathom middleware (the published package, in
     "store" mode with deepresearch_map.json) so its own verdict lands on the agent state.
  2. Runs the agent with a callback handler that records every tool call the orchestrator and its
     research sub-agents make, with the task() delegation each call ran under.
  3. Writes the run folder: tool_trace.json (the calls), messages.json (the orchestrator's message
     history), files.json (the files the agent wrote), middleware.json (langchain-fathom's verdict over
     the orchestrator's own tool calls), ops.json (the ops deep_research_ops.py maps from the trace),
     read.json (the read's verdict over the whole run), run.json (settings and timings).
  4. Appends one row per run to runs/research_map.db (SQLite), the map this folder accumulates.

Environment:
  OPENROUTER_API_KEY   the model key (run_mac.sh loads it from the macOS Keychain once you store it)
  TAVILY_API_KEY       the search key the example's tavily_search needs
  FATHOM_MODEL         e.g. openai/gpt-4o-mini (an OpenRouter model id; default: the example's own model,
                       which needs ANTHROPIC_API_KEY)
  FATHOM_API_KEY       key for the read (default: the rate-limited demo key)
  FATHOM_MAP_DB        where the SQLite map lives (default runs/research_map.db)
  FATHOM_RECURSION     LangGraph recursion limit for the run (default 200)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "deep_research"))

from deep_research_ops import ops_from_trace, summary  # noqa: E402

TAVILY_RESULT = re.compile(r"^## (.*?)\n\*\*URL:\*\* (\S+)", re.M)


def jsonable(x):
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if hasattr(x, "model_dump"):
        return jsonable(x.model_dump())
    return str(x)


def install_wrappers():
    """The two substitutions, applied before the vendored agent.py is imported. Returns what was set."""
    import langchain.chat_models as lcm
    import deepagents

    model_name = os.environ.get("FATHOM_MODEL")
    if model_name:
        from langchain_openai import ChatOpenAI

        def init_chat_model(model=None, **kw):
            temp = kw.get("temperature", 0.0)
            # OpenRouter routing: only providers that honor every request parameter (tools included), and an
            # optional pinned provider list (FATHOM_PROVIDER="Together,Fireworks") with fallbacks off, so a
            # weaker open model does not land on a provider whose tool-call parser drops the call.
            provider = {"require_parameters": True}
            if os.environ.get("FATHOM_PROVIDER"):
                provider["order"] = [x.strip() for x in os.environ["FATHOM_PROVIDER"].split(",") if x.strip()]
                provider["allow_fallbacks"] = False
            extra = {} if "127.0.0.1" in os.environ.get("OPENROUTER_BASE_URL", "") else {"extra_body": {"provider": provider}}
            return ChatOpenAI(model=model_name.removeprefix("openrouter/"),
                              base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                              api_key=os.environ.get("OPENROUTER_API_KEY", "missing"), temperature=temp, **extra)

        lcm.init_chat_model = init_chat_model

    from langchain_fathom import FathomMiddleware
    orig = deepagents.create_deep_agent

    class StashingFathomMiddleware(FathomMiddleware):
        """on_finding='store' puts the verdict on the agent state, and LangGraph drops keys the state schema
        does not declare, so on deepagents the stored verdict never reaches the caller. This keeps a copy."""
        last_verdict = None

        def after_agent(self, state, runtime=None):
            messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
            StashingFathomMiddleware.last_verdict = self._run_read(list(messages))
            return super().after_agent(state, runtime)

    def create_deep_agent(*a, **k):
        mw = list(k.get("middleware") or [])
        mw.append(StashingFathomMiddleware(on_finding="store", mapping_path=str(HERE / "deepresearch_map.json")))
        k["middleware"] = mw
        return orig(*a, **k)

    deepagents.create_deep_agent = create_deep_agent
    install_wrappers.middleware_cls = StashingFathomMiddleware
    return {"model": model_name or "the example's own (anthropic:claude-sonnet-4-5-20250929)",
            "middleware": "langchain-fathom FathomMiddleware(on_finding='store'), verdict stashed by the runner"}


class ToolTrace:
    """A LangChain callback handler that records every tool call in the run, with its ancestry."""

    def __init__(self):
        from langchain_core.callbacks import BaseCallbackHandler

        trace = self

        class Handler(BaseCallbackHandler):
            def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kw):
                trace.parent[str(run_id)] = str(parent_run_id) if parent_run_id else None

            def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kw):
                trace.parent[str(run_id)] = str(parent_run_id) if parent_run_id else None
                name = (serialized or {}).get("name") or ""
                args = inputs if isinstance(inputs, dict) else trace._parse(input_str)
                trace.calls.append({"n": len(trace.calls), "run_id": str(run_id), "parent_run_id": str(parent_run_id) if parent_run_id else None,
                                    "tool": name, "args": jsonable(args), "t_start": time.time()})
                trace.open[str(run_id)] = trace.calls[-1]
                if name == "task":
                    trace.task_runs[str(run_id)] = len([c for c in trace.calls if c["tool"] == "task"])

            def on_tool_end(self, output, *, run_id, **kw):
                trace._close(str(run_id), output, ok=None)

            def on_tool_error(self, error, *, run_id, **kw):
                trace._close(str(run_id), f"Error: {error}", ok=False)

        self.handler = Handler()
        self.calls: List[Dict[str, Any]] = []
        self.open: Dict[str, Dict[str, Any]] = {}
        self.parent: Dict[str, Optional[str]] = {}
        self.task_runs: Dict[str, int] = {}

    @staticmethod
    def _parse(s):
        try:
            return json.loads(s)
        except (TypeError, ValueError):
            return {"input": str(s)}

    def _close(self, run_id, output, ok):
        c = self.open.pop(run_id, None)
        if c is None:
            return
        content = output
        status = None
        if hasattr(output, "content"):
            content = output.content
            status = getattr(output, "status", None)
        if isinstance(content, list):
            content = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        text = str(content or "")
        if ok is None:
            ok = not (str(status or "").lower() == "error" or text.lower().startswith("error"))
        c["ok"] = ok
        c["t_end"] = time.time()
        c["output_chars"] = len(text)
        c["output_head"] = text[:1500]
        if c["tool"] == "tavily_search":
            c["results"] = [[url, title] for title, url in TAVILY_RESULT.findall(text)]

    def finish(self):
        """Attribute each call to the task() delegation it ran under, or to the orchestrator."""
        for c in self.calls:
            who = "orchestrator"
            p = c["parent_run_id"]
            seen = 0
            while p and seen < 200:
                if p in self.task_runs:
                    who = f"research-agent (task {self.task_runs[p]})"
                    break
                p = self.parent.get(p)
                seen += 1
            c["agent"] = who
            c.pop("parent_run_id", None)
        return self.calls


def merge_orchestrator_calls(calls, messages):
    """Add orchestrator tool calls the callback did not see (deepagents runs write_todos through its planning
    middleware without a tool run) from the orchestrator's own message history, in message order."""
    def key(name, args):
        return name, json.dumps(jsonable(args), sort_keys=True)
    seen = {key(c["tool"], c["args"]) for c in calls}
    merged = list(calls)
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            name, args = tc.get("name", ""), tc.get("args", {})
            if key(name, args) in seen:
                continue
            seen.add(key(name, args))
            entry = {"n": None, "run_id": tc.get("id"), "tool": name, "args": jsonable(args), "ok": True,
                     "agent": "orchestrator", "source": "messages"}
            # place it before the first traced call that comes after it in the orchestrator's history
            later = [c for c in merged if c["agent"] == "orchestrator" and c.get("source") != "messages"
                     and _msg_index(messages, c) > _msg_index(messages, entry)]
            idx = merged.index(later[0]) if later else len(merged)
            merged.insert(idx, entry)
    for i, c in enumerate(merged):
        c["n"] = i
    return merged


def _msg_index(messages, call):
    k = (call["tool"], json.dumps(jsonable(call["args"]), sort_keys=True))
    for i, m in enumerate(messages):
        for tc in getattr(m, "tool_calls", None) or []:
            if (tc.get("name", ""), json.dumps(jsonable(tc.get("args", {})), sort_keys=True)) == k:
                return i
    return 10**9


def write_map_row(db: Path, row: dict):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS research_runs (
        run TEXT PRIMARY KEY, stamp TEXT, question TEXT, model TEXT, elapsed_s REAL, error TEXT,
        tool_calls INTEGER, searches INTEGER, distinct_queries INTEGER, repeated_searches INTEGER,
        delegations INTEGER, distinct_delegations INTEGER, sources_held INTEGER, report_citations INTEGER,
        files_written INTEGER, ops INTEGER, coherent INTEGER, findings INTEGER, findings_by_kind TEXT,
        middleware_ops INTEGER, middleware_findings INTEGER, report TEXT)""")
    if "report" not in [r[1] for r in con.execute("PRAGMA table_info(research_runs)")]:
        con.execute("ALTER TABLE research_runs ADD COLUMN report TEXT")
    row = {"report": None, **row}
    con.execute("INSERT OR REPLACE INTO research_runs (run,stamp,question,model,elapsed_s,error,tool_calls,searches,"
                "distinct_queries,repeated_searches,delegations,distinct_delegations,sources_held,report_citations,"
                "files_written,ops,coherent,findings,findings_by_kind,middleware_ops,middleware_findings,report) VALUES "
                "(:run,:stamp,:question,:model,:elapsed_s,:error,:tool_calls,:searches,:distinct_queries,"
                ":repeated_searches,:delegations,:distinct_delegations,:sources_held,:report_citations,:files_written,"
                ":ops,:coherent,:findings,:findings_by_kind,:middleware_ops,:middleware_findings,:report)", row)
    con.commit()
    con.close()


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    question = sys.argv[1]
    if not os.environ.get("TAVILY_API_KEY"):
        raise SystemExit("set TAVILY_API_KEY first (the example's search tool needs it)")
    if os.environ.get("FATHOM_MODEL") and not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("set OPENROUTER_API_KEY first (FATHOM_MODEL routes through OpenRouter)")

    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = HERE / "runs" / f"{stamp}_{slug}"
    run_dir.mkdir(parents=True)

    settings = install_wrappers()
    import agent as example  # the vendored deep_research/agent.py, built at import
    from langchain_core.messages import HumanMessage, messages_to_dict

    trace = ToolTrace()
    t0 = time.time()
    run_error = None
    result = {}
    try:
        result = example.agent.invoke({"messages": [HumanMessage(content=question)]},
                                      config={"callbacks": [trace.handler],
                                              "recursion_limit": int(os.environ.get("FATHOM_RECURSION", "200"))})
    except Exception as e:  # noqa: BLE001
        run_error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0
    calls = trace.finish()
    calls = merge_orchestrator_calls(calls, result.get("messages", []) if isinstance(result, dict) else [])

    files = jsonable(result.get("files") or {}) if isinstance(result, dict) else {}
    messages = messages_to_dict(result.get("messages", [])) if isinstance(result, dict) else []
    middleware = jsonable(result.get("fathom")) if isinstance(result, dict) else None
    if middleware is None:
        middleware = jsonable(getattr(install_wrappers, "middleware_cls", None) and install_wrappers.middleware_cls.last_verdict)
    (run_dir / "tool_trace.json").write_text(json.dumps({"calls": calls}, indent=1))
    (run_dir / "messages.json").write_text(json.dumps(messages, indent=1))
    (run_dir / "files.json").write_text(json.dumps(files, indent=1))
    (run_dir / "middleware.json").write_text(json.dumps(middleware, indent=1))
    (run_dir / "run.json").write_text(json.dumps({"question": question, **settings, "elapsed_s": round(elapsed, 1),
                                                   "error": run_error, "tool_calls": len(calls),
                                                   "recursion_limit": int(os.environ.get("FATHOM_RECURSION", "200"))}, indent=1))

    final = ""
    ai = [m for m in messages if m.get("type") == "ai"]
    if ai:
        c = ai[-1]["data"].get("content")
        final = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    if run_error is None and not calls and not final.strip():
        out_tok = None
        if ai:
            out_tok = ((ai[-1]["data"].get("usage_metadata") or {}).get("output_tokens"))
        run_error = (f"empty completion: the model returned no text and no tool call ({out_tok} output tokens). "
                     "A provider that cannot parse the model's tool call returns it as nothing; pin one with "
                     "FATHOM_PROVIDER or change FATHOM_MODEL. This run is not a clean run.")
        print(f"\nRUN PRODUCED NOTHING. {run_error}")
        (run_dir / "run.json").write_text(json.dumps({**json.load(open(run_dir / "run.json")), "error": run_error}, indent=1))
    ops = ops_from_trace(calls, final)
    (run_dir / "ops.json").write_text(json.dumps(ops, indent=1))
    s = summary(ops)
    if s["report"] == "none" and run_error is None:
        print("\nNO REPORT DELIVERED: the run ended without /final_report.md and without a report in the final message.")

    verdict = None
    from fathom_read.client import read, ReadError
    from fathom_read.cli import render
    from fathom_read.ops import Op
    try:
        if not ops:
            raise ReadError("no ops to read (the run committed nothing)")
        verdict = read([Op.from_dict(o) for o in ops])
        (run_dir / "read.json").write_text(json.dumps(verdict.as_dict(), indent=1))
        print("\n" + render(verdict, title=f"fathom read: {question[:60]}"))
    except ReadError as e:
        print(f"\nfathom read failed: {e}\n(ops.json is saved; run `fathom read ops.json` later)")

    by_kind = {}
    if verdict is not None:
        for f in verdict.findings:
            k = f.kind + ("/" + f.key[:40] if f.kind == "stale_reference" else "")
            by_kind[k] = by_kind.get(k, 0) + 1
    write_map_row(Path(os.environ.get("FATHOM_MAP_DB") or HERE / "runs" / "research_map.db"), {
        "run": run_dir.name, "stamp": stamp, "question": question, "model": settings["model"], "elapsed_s": round(elapsed, 1),
        "error": run_error, "tool_calls": len(calls), **s, "ops": len(ops),
        "coherent": None if verdict is None else int(verdict.coherent),
        "findings": None if verdict is None else len(verdict.findings), "findings_by_kind": json.dumps(by_kind),
        "middleware_ops": (middleware or {}).get("ops_read") if isinstance(middleware, dict) else None,
        "middleware_findings": len((middleware or {}).get("findings", [])) if isinstance(middleware, dict) else None})

    print(f"\nrun folder: {run_dir}\ntool calls: {len(calls)}  searches: {s['searches']} ({s['distinct_queries']} distinct, "
          f"{s['repeated_searches']} repeated)  delegations: {s['delegations']} ({s['distinct_delegations']} distinct)  "
          f"sources held: {s['sources_held']}  report citations: {s['report_citations']}  report: {s['report']}  ops: {len(ops)}")
    if isinstance(middleware, dict):
        print(f"langchain-fathom middleware (orchestrator's own tool calls): {middleware.get('ops_read')} ops, "
              f"{len(middleware.get('findings', []))} findings")
    if run_error:
        raise SystemExit(f"run failed after {elapsed:.0f}s: {run_error}\n(trace and state saved to {run_dir})")


if __name__ == "__main__":
    main()
