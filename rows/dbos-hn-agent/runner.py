"""
Run DBOS's published Hacker News research agent once, as published, and record its step stream.

What this does:
  1. Starts DBOS with a SQLite system database in a fresh run folder.
  2. Runs `agentic_research_workflow(topic, max_iterations)` from the vendored example, unchanged.
  3. Records every DBOS step the workflow calls: name, args, result, ok. DBOS itself records step
     outputs in its system database; this runner adds the arguments so the read can see what each
     step acted on. The DBOS table is exported beside it for provenance.
  4. Maps the step stream to committed-state ops (hn_agent_ops.py) and sends them to the read.

Environment:
  OPENAI_API_KEY   required by the example. To route through OpenRouter, also set
  OPENAI_BASE_URL  https://openrouter.ai/api/v1  and  FATHOM_MODEL  openai/gpt-4o-mini
  FATHOM_MODEL     model name passed to the example's LLM calls (default: the example's gpt-4o-mini)
  FATHOM_API_KEY   key for the read and the repair (free keys from the fathom README; the demo key is rate-limited)
  FATHOM_GATE=1    put the repair in front of the follow-up step (see gate_followup)

Usage:
  python runner.py "<topic>" [max_iterations]
"""
from __future__ import annotations

import functools
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "hacker-news-agent"))
sys.path.insert(0, str(HERE))

from dbos import DBOS  # noqa: E402

import hacker_news_agent.agent as agent_mod  # noqa: E402
import hacker_news_agent.api as api_mod  # noqa: E402
import hacker_news_agent.workflows as wf  # noqa: E402
from hacker_news_agent.models import AGENT_STATUS  # noqa: E402
from hn_agent_ops import ops_from_steps  # noqa: E402

STEPS: list = []
GATE: list = []          # one entry per gated follow-up decision, at outcome level
GATE_ON = os.environ.get("FATHOM_GATE", "") not in ("", "0", "false", "no")
FOLLOWUP_MARK = "Generate focused follow-up queries"


def _norm(q):
    return " ".join(str(q or "").lower().split())


def committed_ops():
    """The run's committed-state ops so far, the same mapping the read consumes."""
    return ops_from_steps(STEPS)


def gate_followup(call, messages, **kw):
    """The repair in front of the follow-up step, served by the Fathom service.

    The agent proposes follow-up queries. The service checks each proposal against the run's committed
    state and returns a decision: proceed (every proposal is new), filter (keep the proposals that do not
    contradict the committed state), or reground (every proposal contradicts it, and the response carries
    the committed facts to put back in front of the agent before asking again). The policy behind the
    decision runs in the service; this runner only applies what comes back. Requires fathom-read >= 0.5.0
    and a service key (free, see the fathom README).
    """
    try:
        from fathom_read.client import reground
    except ImportError as e:  # pragma: no cover
        raise SystemExit("FATHOM_GATE=1 needs fathom-read >= 0.5.0 (pip install -U fathom-read)") from e
    raw = call(messages, **kw)
    try:
        proposed = json.loads(raw.strip().strip("`").replace("json\n", "", 1)) if raw else []
    except ValueError:
        proposed = []
    if not isinstance(proposed, list):
        proposed = []
    ops = committed_ops()
    committed = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "queries"]
    entry = {"iteration": len(committed), "committed": committed, "proposed": proposed, "regrounds": 0}
    proposals = [{"op": "add", "kind": "research", "key": "queries", "value": _norm(q)} for q in proposed]
    verdict = reground(ops, proposals=proposals)
    if verdict.decision == "proceed":
        entry["outcome"] = "clean"
        GATE.append(entry)
        return raw
    if verdict.decision == "filter":
        keep = [q for q in proposed if _norm(q) in set(verdict.keep)]
        entry["outcome"] = "filtered"
        entry["chosen"] = keep[0]
        GATE.append(entry)
        return json.dumps(keep)
    # reground: put the committed facts the service returned in front of the agent and ask again
    msgs = [dict(m) for m in messages]
    for attempt in range(2):
        msgs[-1] = dict(msgs[-1])
        msgs[-1]["content"] = messages[-1]["content"] + verdict.prompt_note(proposed)
        entry["regrounds"] += 1
        raw2 = call(msgs, **kw)
        try:
            again = json.loads(raw2.strip().strip("`").replace("json\n", "", 1))
        except ValueError:
            again = []
        proposals = [{"op": "add", "kind": "research", "key": "queries", "value": _norm(q)} for q in again] if isinstance(again, list) else []
        verdict = reground(ops, proposals=proposals) if proposals else verdict
        if proposals and verdict.decision in ("proceed", "filter"):
            keep = again if verdict.decision == "proceed" else [q for q in again if _norm(q) in set(verdict.keep)]
            entry["outcome"] = "regrounded"
            entry["chosen"] = keep[0]
            GATE.append(entry)
            return json.dumps(keep)
        proposed = again if isinstance(again, list) else proposed
    entry["outcome"] = "unresolved"
    GATE.append(entry)
    return raw


def jsonable(x):
    if hasattr(x, "model_dump"):
        return jsonable(x.model_dump())
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)


def record(step_fn, name: str, argnames: list):
    """Wrap a DBOS step so its arguments and result land in the step stream."""

    @functools.wraps(step_fn)
    def wrapper(*args, **kwargs):
        bound = {n: v for n, v in zip(argnames, args)}
        bound.update(kwargs)
        entry = {"step_name": name, "args": jsonable(bound), "result": None, "ok": True,
                 "started_at": time.time()}
        try:
            out = step_fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["ended_at"] = time.time()
            STEPS.append(entry)
            raise
        entry["result"] = jsonable(out)
        entry["ended_at"] = time.time()
        STEPS.append(entry)
        return out

    return wrapper


def patch_example():
    if getattr(patch_example, "done", False):
        return
    patch_example.done = True
    # Model override without touching the vendored code.
    model = os.environ.get("FATHOM_MODEL")
    if model:
        orig = agent_mod.call_llm

        def call_llm(messages, **kw):
            kw.pop("model", None)
            return orig(messages, model=model, **kw)

        agent_mod.call_llm = call_llm

    # Step recording. workflows.py imported these names, so patch them where the workflow looks them up.
    wf.search_hackernews_step = record(api_mod.search_hackernews_step, "search_hackernews_step", ["query", "max_results"])
    wf.get_comments_step = record(api_mod.get_comments_step, "get_comments_step", ["story_id", "max_comments"])
    wf.evaluate_results_step = record(agent_mod.evaluate_results_step, "evaluate_results_step", ["topic", "query", "stories", "comments"])
    wf.should_continue_step = record(agent_mod.should_continue_step, "should_continue_step", ["topic", "all_findings", "current_iteration", "max_iterations"])
    wf.generate_follow_ups_step = record(agent_mod.generate_follow_ups_step, "generate_follow_ups_step", ["topic", "current_findings", "iteration"])
    wf.synthesize_findings_step = record(agent_mod.synthesize_findings_step, "synthesize_findings_step", ["topic", "all_findings"])


def install_gate():
    """Put the read in front of the follow-up step. Idempotent."""
    if getattr(install_gate, "done", False):
        return
    install_gate.done = True
    inner = agent_mod.call_llm

    def gated_call_llm(messages, **kw):
        if len(messages) > 1 and FOLLOWUP_MARK in messages[0].get("content", ""):
            return gate_followup(inner, messages, **kw)
        return inner(messages, **kw)

    agent_mod.call_llm = gated_call_llm


def export_dbos_table(db_path: Path):
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT workflow_uuid, function_id, function_name, error IS NOT NULL, child_workflow_id, "
                       "started_at_epoch_ms, completed_at_epoch_ms FROM operation_outputs ORDER BY workflow_uuid, function_id").fetchall()
    wfs = con.execute("SELECT workflow_uuid, name, status, created_at, updated_at FROM workflow_status").fetchall()
    con.close()
    return {"workflow_status": [dict(zip(["workflow_uuid", "name", "status", "created_at", "updated_at"], r)) for r in wfs],
            "operation_outputs": [dict(zip(["workflow_uuid", "function_id", "function_name", "errored", "child_workflow_id",
                                             "started_at_epoch_ms", "completed_at_epoch_ms"], r)) for r in rows]}


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    topic = sys.argv[1]
    max_iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set")

    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = HERE / "runs" / f"{stamp}_{slug}{'_gated' if GATE_ON else ''}"
    run_dir.mkdir(parents=True)
    os.chdir(run_dir)

    patch_example()
    if GATE_ON:
        install_gate()
    DBOS(config={"name": "hacker-news-agent", "system_database_url": "sqlite:///dbos_system.sqlite"})
    DBOS.launch()
    t0 = time.time()
    handle = DBOS.start_workflow(wf.agentic_research_workflow, topic, max_iterations)
    run_error = None
    try:
        handle.get_result()
    except Exception as e:  # noqa: BLE001
        run_error = f"{type(e).__name__}: {e}"
    status = DBOS.get_event(handle.workflow_id, AGENT_STATUS)
    elapsed = time.time() - t0
    dbos_table = export_dbos_table(run_dir / "dbos_system.sqlite")
    DBOS.destroy()

    stream = {"workflow_id": handle.workflow_id, "topic": topic, "max_iterations": max_iterations,
              "model": os.environ.get("FATHOM_MODEL") or "gpt-4o-mini", "elapsed_s": round(elapsed, 1),
              "error": run_error, "gate": GATE_ON, "steps": STEPS}
    (run_dir / "dbos_steps.json").write_text(json.dumps(stream, indent=1))
    if GATE_ON:
        (run_dir / "gate.json").write_text(json.dumps(GATE, indent=1))
    if run_error:
        (run_dir / "dbos_operation_outputs.json").write_text(json.dumps(dbos_table, indent=1))
        raise SystemExit(f"workflow failed after {len(STEPS)} steps: {run_error}\n(step stream saved to {run_dir})")
    (run_dir / "dbos_operation_outputs.json").write_text(json.dumps(dbos_table, indent=1))
    report = (status.report if status else None) or ""
    (run_dir / "report.md").write_text(report)

    ops = ops_from_steps(STEPS)
    (run_dir / "ops.json").write_text(json.dumps(ops, indent=1))

    from fathom_read.client import read, ReadError
    from fathom_read.cli import render
    from fathom_read.ops import Op
    try:
        verdict = read([Op.from_dict(o) for o in ops])
        (run_dir / "read.json").write_text(json.dumps(verdict.as_dict(), indent=1))
        print("\n" + render(verdict, title=f"fathom read: {topic}"))
    except ReadError as e:
        print(f"\nfathom read failed: {e}\n(ops.json is saved; run `fathom read ops.json` later)")

    n_llm = sum(1 for s in STEPS if s["step_name"] in ("evaluate_results_step", "should_continue_step",
                                                        "generate_follow_ups_step", "synthesize_findings_step"))
    print(f"\nrun folder: {run_dir}\nsteps recorded: {len(STEPS)}  (LLM steps: {n_llm}; DBOS rows: {len(dbos_table['operation_outputs'])})")
    if GATE_ON:
        from collections import Counter
        print("gate:", dict(Counter(g["outcome"] for g in GATE)), f"({sum(g['regrounds'] for g in GATE)} re-ground calls)")


if __name__ == "__main__":
    main()
