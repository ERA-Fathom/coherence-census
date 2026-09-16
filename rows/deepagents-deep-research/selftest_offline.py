"""
Offline self-test of the runner: no network, no model spend, no search spend.

Stands in a scripted OpenAI-compatible model server on localhost and a scripted Tavily, runs the vendored
deep_research agent through the runner exactly as a live run would, and checks the trace, the ops, and
(when a read is available) the findings.

The scripted model plants three contradictions the read should name:
  - the orchestrator delegates "Research topic A" twice                       -> duplicate_commit on delegations
  - sub-agent B, and the second sub-agent A, repeat sub-agent A's search        -> duplicate_commit on queries (x2)
  - the final report's Sources list cites https://example.org/never, a URL no
    search in the run returned, and https://example.org/topic-b, which no sub-agent
    fetched because every sub-agent ran A's search                              -> stale_reference on each
and a specificity check: the report's citation of the page the run did hold, and the other files, produce no finding.

Run: python selftest_offline.py
The read comes from the hosted service when reachable, else from the private core on the dev machine, else
the findings check is skipped and only the trace and ops are asserted.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["TAVILY_API_KEY"] = "offline"
os.environ["OPENROUTER_API_KEY"] = "offline"
os.environ["FATHOM_MODEL"] = "scripted-research-model"
os.environ["FATHOM_RECURSION"] = "80"
MAP_DB = Path(os.environ.get("FATHOM_MAP_DB") or HERE / "runs" / "research_map.db")
os.environ["FATHOM_MAP_DB"] = str(MAP_DB)

CALLS = {"n": 0, "orch": 0, "sub": 0}
URL_A, URL_B, URL_NEVER = "https://example.org/topic-a", "https://example.org/topic-b", "https://example.org/never"
SEARCH_RESULTS = {"topic a query": [(URL_A, "Topic A page")], "topic b query": [(URL_B, "Topic B page")]}
REPORT = f"""# Findings

Topic A matters [1]. Topic B matters too [2]. A further claim [3].

### Sources
[1] {URL_A}
[2] {URL_B}
[3] {URL_NEVER}
"""


def _text(m) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def decide(body: dict):
    """Return (content, tool_calls) for one chat request, from the vendored prompts' own markers."""
    msgs = body.get("messages", [])
    system = " ".join(_text(m) for m in msgs if m.get("role") == "system")
    turn = sum(1 for m in msgs if m.get("role") == "assistant")
    if "Sub-Agent Research Coordination" in system:
        CALLS["orch"] += 1
        if turn == 0:
            return None, [("write_todos", {"todos": [{"content": "Research topic A", "status": "pending"},
                                                     {"content": "Research topic B", "status": "pending"}]})]
        if turn == 1:
            user = next((_text(m) for m in msgs if m.get("role") == "user"), "")
            return None, [("write_file", {"file_path": "/research_request.md", "content": user})]
        if turn == 2:
            return None, [("task", {"description": "Research topic A", "subagent_type": "research-agent"}),
                          ("task", {"description": "Research topic B", "subagent_type": "research-agent"})]
        if turn == 3:
            return None, [("task", {"description": "Research topic A", "subagent_type": "research-agent"})]
        if turn == 4:
            return None, [("write_file", {"file_path": "/final_report.md", "content": REPORT})]
        return "Report written to /final_report.md.", None
    if "research assistant conducting research" in system:
        CALLS["sub"] += 1
        user = next((_text(m) for m in msgs if m.get("role") == "user"), "").lower()
        q = "topic a query"  # sub-agent B repeats A's search on purpose
        if turn == 0:
            return None, [("tavily_search", {"query": q})]
        if turn == 1:
            return None, [("think_tool", {"reflection": f"Found enough on {user[:30]}."})]
        src = URL_A if "topic a" in user else URL_B
        return f"Findings on {user[:40]}: see {src}", None
    return "ok", None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "scripted-research-model", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        CALLS["n"] += 1
        content, tools = decide(body)
        if tools:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{CALLS['n']}_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(tools)]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": content}
            finish = "stop"
        self._send({"id": f"chatcmpl-{CALLS['n']}", "object": "chat.completion", "created": 0,
                    "model": body.get("model", "scripted"),
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

    def _send(self, doc):
        data = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def offline_read_if_available():
    import fathom_read.client as client
    orig = client.read
    core_py = HERE.parent / "_publish" / "fathom-core" / "src" / "fathom_read" / "core.py"

    def read(ops, **kw):
        try:
            return orig(ops, timeout=8, **kw)
        except client.ReadError:
            if not core_py.exists():
                raise
        import importlib.util
        spec = importlib.util.spec_from_file_location("fathom_core_private", core_py)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["fathom_core_private"] = mod
        spec.loader.exec_module(mod)
        core_ops = [mod.Op(**{k: (tuple(map(tuple, v)) if k == "refs" else v) for k, v in o.as_dict().items()
                             if k in mod.Op.__dataclass_fields__}) for o in ops]
        v = mod.read(core_ops)
        from fathom_read.ops import Verdict
        return Verdict.from_dict(v.as_dict() if hasattr(v, "as_dict") else v)

    client.read = read


def main():
    srv = start_server()
    os.environ["OPENROUTER_BASE_URL"] = f"http://127.0.0.1:{srv.server_port}/v1"
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    offline_read_if_available()

    sys.path.insert(0, str(HERE / "deep_research"))
    import research_agent.tools as tools_mod

    def fake_search(query, **kw):
        return {"results": [{"url": u, "title": t} for u, t in SEARCH_RESULTS.get(query, [])]}

    tools_mod.tavily_client.search = fake_search
    tools_mod.fetch_webpage_content = lambda url, timeout=10.0: f"Page content for {url}."

    sys.argv = ["runner.py", "Compare topic A and topic B (selftest)"]
    import runner
    try:
        runner.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            raise

    rd = sorted((HERE / "runs").glob("*selftest"))[-1]
    calls = json.load(open(rd / "tool_trace.json"))["calls"]
    ops = json.load(open(rd / "ops.json"))
    files = json.load(open(rd / "files.json"))
    names = [c["tool"] for c in calls]
    assert names.count("task") == 3, names
    assert names.count("tavily_search") == 3, names
    assert names.count("think_tool") == 3, names
    assert names.count("write_todos") == 1 and names.count("write_file") == 2, names
    subs = [c["agent"] for c in calls if c["tool"] == "tavily_search"]
    assert all(a.startswith("research-agent (task ") for a in subs), subs
    assert len(set(subs)) == 3, subs  # each search attributed to its own delegation
    assert all(c["ok"] for c in calls), [c for c in calls if not c["ok"]]
    assert any(k.endswith("final_report.md") for k in files), list(files)
    qs = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "queries"]
    assert qs == ["topic a query"] * 3, qs
    ds = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "delegations"]
    assert ds == ["research topic a", "research topic b", "research topic a"], ds
    report = [o for o in ops if o["kind"] == "file" and o["key"].endswith("final_report.md")][-1]
    assert report["refs"] == [["source", URL_A], ["source", URL_B], ["source", URL_NEVER]], report["refs"]
    srcs = sorted(o["key"] for o in ops if o["kind"] == "source")
    assert srcs == [URL_A, URL_A, URL_A], srcs  # the same page held three times, the sibling's copies included
    mw = json.load(open(rd / "middleware.json"))
    # the published middleware talks to the hosted read only; where that is unreachable it logs and stores nothing
    assert mw is None or (isinstance(mw, dict) and mw.get("ops_read", 0) >= 3), mw
    print(f"trace OK: {len(calls)} tool calls, {len(ops)} ops, {CALLS['n']} scripted model calls "
          f"({CALLS['orch']} orchestrator, {CALLS['sub']} sub-agent); middleware ops {mw.get('ops_read') if isinstance(mw, dict) else 'n/a (read unreachable)'}")

    if (rd / "read.json").exists():
        v = json.load(open(rd / "read.json"))
        got = sorted((f["kind"], f["key"]) for f in v["findings"])
        core_extra = ("duplicate_commit", URL_A)  # reads from 0.4.0 on also flag the same page set again with the same title
        got_core = [g for g in got if g != core_extra]
        # every sub-agent ran A's search, so the run never held URL_B either: the report's [2] is a stale reference too
        want = sorted([("duplicate_commit", "delegations"), ("duplicate_commit", "queries"), ("duplicate_commit", "queries"),
                       ("stale_reference", URL_NEVER), ("stale_reference", URL_B)])
        assert got_core == want, (got, want)
        assert not v["coherent"]
        print(f"read OK: {got}")
        import sqlite3
        con = sqlite3.connect(MAP_DB)
        row = con.execute("SELECT searches, repeated_searches, delegations, report_citations, findings FROM research_runs WHERE run=?", (rd.name,)).fetchone()
        con.close()
        assert row[:4] == (3, 2, 3, 3) and row[4] in (5, 7), row
        print(f"map row OK: {row}")
    else:
        print("read SKIPPED (no hosted read reachable and no private core here); trace and ops verified")
    print(f"selftest OK, run folder {rd.name}")


if __name__ == "__main__":
    main()
