"""
Offline self-test of the runner: no network, no model spend, no search spend.

Stands in a scripted OpenAI-compatible model server on localhost and a scripted Serper API, runs the
vendored flow through the runner exactly as a live run would, and checks the event stream, the ops,
and (when a read is available) the findings.

The scripted model plants four contradictions the read should name:
  - the outliner duplicates a chapter title it was told not to duplicate      -> duplicate_commit on titles
  - chapter 2's researcher repeats the outline researcher's search, and the duplicated
    chapter 3's researcher repeats chapter 1's                                  -> duplicate_commit on queries (x2)
  - chapter 2's writer delivers the chapter under a title the outline never held -> stale_reference on chapter_title
  - chapter 2's text cites "Chapter 7" in a three-chapter book                  -> stale_reference on chapter_no
  - the finished book therefore lacks the outlined "AI in Insurance" chapter      -> stale_reference on chapter
and a specificity check: chapter 1 and chapter 3 are clean and must produce no finding of their own.

Run: python selftest_offline.py
The read comes from FATHOM_ENDPOINT (the hosted read) when reachable, else from the private core on the
dev machine, else the findings check is skipped and only the events and ops are asserted.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"

os.environ["SERPER_API_KEY"] = "offline"
os.environ["OPENROUTER_API_KEY"] = "offline"
os.environ["FATHOM_MODEL"] = "openrouter/scripted-book-model"
os.environ["CREWAI_TESTING"] = "true"  # no first-run trace prompt
MAP_DB = Path(os.environ.get("FATHOM_MAP_DB") or HERE / "runs" / "book_map.db")
os.environ["FATHOM_MAP_DB"] = str(MAP_DB)
LOG = HERE / "runs" / "_selftest_requests.jsonl"

CALLS = {"n": 0, "tool_calls": 0}
OUTLINE = {"chapters": [
    {"title": "AI in Banking", "description": "How banks deploy AI for credit and operations."},
    {"title": "AI in Insurance", "description": "Underwriting and claims."},
    {"title": "AI in Banking", "description": "A duplicate the outliner should not have produced."},
]}
CHAPTERS = {
    "ai in banking": ("AI in Banking", "Banks use AI for credit decisions. See Chapter 2 for insurance."),
    "ai in insurance": ("AI in Insurance, Revisited", "Insurers use AI for claims. Chapter 1 covered banks; Chapter 7 covers the future."),
}
SEARCH = {"outline": "ai in finance 2026", "ai in banking": "ai credit models banks 2026",
          "ai in insurance": "ai in finance 2026"}  # chapter 2 repeats the outline's search


def _text(m) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _find_chapter(system: str, user: str):
    """Which chapter this crew is on, from the vendored prompts: the writer's task names the title,
    the chapter researcher's goal names it after the topic."""
    m = re.search(r"title of the chapter:\s*(.+)", user, re.IGNORECASE)
    if not m:
        m = re.search(r"information about .+? and (.+?) that will be used", system, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    t = " ".join(m.group(1).lower().split())
    for key in CHAPTERS:
        if t.startswith(key):
            return key
    return None


def decide(body: dict):
    """Return (content, tool_call_or_None) for one chat request."""
    msgs = body.get("messages", [])
    system = " ".join(_text(m) for m in msgs if m.get("role") == "system")
    users = [_text(m) for m in msgs if m.get("role") == "user"]
    user = users[0] if users else ""
    non_system = " ".join(_text(m) for m in msgs if m.get("role") != "system")
    has_tool_result = any(m.get("role") == "tool" for m in msgs) or "Observation:" in non_system
    tools_offered = bool(body.get("tools")) or "Action:" in system or "Action Input" in system
    native = bool(body.get("tools"))

    if "Book Outlining Agent" in system or ("book outline" in user.lower() and "chapters" in user.lower() and "Research Agent" not in system):
        return json.dumps(OUTLINE), None
    if "Chapter Writer" in system:
        key = _find_chapter(system, user) or "ai in banking"
        title, content = CHAPTERS[key]
        return json.dumps({"title": title, "content": content}), None
    if "Research Agent" in system:
        key = _find_chapter(system, user)
        q = SEARCH[key] if key else SEARCH["outline"]
        if tools_offered and not has_tool_result:
            if native:
                return None, {"name": body["tools"][0]["function"]["name"], "arguments": json.dumps({"search_query": q})}
            return f"Thought: I should search.\nAction: Search the internet with Serper\nAction Input: {json.dumps({'search_query': q})}", None
        note = f"Key points on {key or 'the topic'}: adoption is rising; sources found for {q}."
        return (note if native else f"Thought: I now know the final answer\nFinal Answer: {note}"), None
    # Anything else (a converter pass, a summary): echo a JSON shape if a schema is asked for.
    rf = body.get("response_format") or {}
    schema = json.dumps(rf)
    if "chapters" in schema:
        return json.dumps(OUTLINE), None
    if '"content"' in schema:
        return json.dumps({"title": "AI in Banking", "content": "x"}), None
    return "ok", None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "scripted-book-model", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        CALLS["n"] += 1
        content, tool = decide(body)
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps({"n": CALLS["n"], "tools": [t.get("function", {}).get("name") for t in body.get("tools") or []],
                                "response_format": bool(body.get("response_format")), "messages": body.get("messages"),
                                "reply": content, "tool_call": tool}) + "\n")
        msg = {"role": "assistant", "content": content}
        finish = "stop"
        if tool:
            CALLS["tool_calls"] += 1
            msg = {"role": "assistant", "content": None,
                   "tool_calls": [{"id": f"call_{CALLS['n']}", "type": "function", "function": tool}]}
            finish = "tool_calls"
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


class FakeResp:
    status_code = 200
    content = b"{}"

    def __init__(self, q):
        self._q = q

    def raise_for_status(self):
        pass

    def json(self):
        return {"organic": [{"title": f"Result for {self._q}", "link": f"https://example.org/{abs(hash(self._q)) % 1000}",
                             "snippet": f"A snippet about {self._q}.", "position": 1}]}


def fake_post(url, headers=None, json=None, timeout=None, **kw):
    assert "serper" in url, url
    return FakeResp((json or {}).get("q", ""))


def offline_read_if_available():
    """Point fathom_read.client.read at the private core when the hosted read is unreachable."""
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
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    os.environ["OPENROUTER_BASE_URL"] = base   # crewai >= 1.x native provider
    os.environ["OPENROUTER_API_BASE"] = base   # crewai 0.x via litellm
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["CREWAI_TRACING_ENABLED"] = "false"
    import requests
    requests.post = fake_post
    import crewai_tools.tools.serper_dev_tool.serper_dev_tool as serper_mod
    serper_mod.requests.post = fake_post
    offline_read_if_available()

    sys.argv = ["runner.py", "AI in Finance (selftest)", "AI in finance"]
    import runner
    try:
        runner.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            raise

    rd = sorted((HERE / "runs").glob("*ai-in-finance-selftest"))[-1]
    events = json.load(open(rd / "crewai_events.json"))["events"]
    ops = json.load(open(rd / "ops.json"))
    state = json.load(open(rd / "flow_state.json"))
    kinds = [e["type"] for e in events]
    assert kinds.count("task_completed") == 8, kinds          # outline: 2 tasks; 3 chapters x 2 tasks
    assert kinds.count("tool_usage_finished") == 4, kinds     # 1 outline search + 3 chapter searches
    assert kinds.count("task_completed") == 8
    assert len(state["book_outline"]) == 3 and len(state["book"]) == 3, state
    qs = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "queries"]
    assert qs[0] == "ai in finance 2026" and sorted(qs) == sorted(["ai in finance 2026", "ai credit models banks 2026",
                                                                    "ai in finance 2026", "ai credit models banks 2026"]), qs
    titles = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "titles"]
    assert titles == ["ai in banking", "ai in insurance", "ai in banking"], titles
    answers = [o for o in ops if o["op"] == "set" and o["kind"] == "chapter"]
    assert len(answers) == 3, answers
    book = [o for o in ops if o["op"] == "answer" and o["kind"] == "book"]
    assert len(book) == 1 and book[0]["refs"] == [["chapter", "ai in banking"], ["chapter", "ai in insurance"], ["chapter", "ai in banking"]], book
    revisited = [a for a in answers if ["chapter_title", "ai in insurance, revisited"] in a["refs"]]
    assert len(revisited) == 1, [a["refs"] for a in answers]
    assert ["chapter_no", "7"] in revisited[0]["refs"] and ["chapter_no", "1"] in revisited[0]["refs"], revisited[0]["refs"]
    banking = [a for a in answers if ["chapter_title", "ai in banking"] in a["refs"]]
    assert len(banking) == 2 and all(["chapter_no", "2"] in a["refs"] for a in banking), [a["refs"] for a in banking]
    commit = [o for o in ops if o["op"] == "commit"]
    assert len(commit) == 1 and ops.index(commit[0]) < ops.index(answers[0]), "outline commits before any chapter"
    print(f"events OK: {len(events)} events, {len(ops)} ops, {CALLS['n']} scripted model calls ({CALLS['tool_calls']} tool calls)")

    if (rd / "read.json").exists():
        v = json.load(open(rd / "read.json"))
        got = sorted((f["kind"], f["key"]) for f in v["findings"])
        want = sorted([("duplicate_commit", "titles"), ("duplicate_commit", "queries"), ("duplicate_commit", "queries"),
                       ("stale_reference", "ai in insurance, revisited"), ("stale_reference", "7"), ("stale_reference", "ai in insurance")])
        # cores from 0.4.0 on also flag the duplicated chapter written twice with the same content (rule 4b,
        # "the same write made twice"); older reads do not, and both are accepted here
        optional = ("duplicate_commit", "ai in banking")
        got_core = [g for g in got if g != optional]
        assert got_core == want, (got, want)
        assert not v["coherent"]
        print(f"read OK: {got}")
        import sqlite3
        con = sqlite3.connect(MAP_DB)
        row = con.execute("SELECT chapters, searches, repeated_searches, findings FROM book_runs WHERE run=?", (rd.name,)).fetchone()
        con.close()
        assert row in ((3, 4, 2, 6), (3, 4, 2, 7)), row
        print(f"map row OK: {row}")
    if (rd / "read_spans.json").exists():
        sv = json.load(open(rd / "read_spans.json"))
        got = sorted((f["kind"], f["key"]) for f in sv["findings"])
        assert got == [("duplicate_commit", "queries"), ("duplicate_commit", "queries")], got
        spans = json.load(open(rd / "phoenix_spans.json"))["spans"]
        tool_spans = [x for x in spans if str(x["attributes"].get("openinference.span.kind", "")).upper() == "TOOL"]
        assert len(tool_spans) == 4, [x["name"] for x in tool_spans]
        print(f"span read OK: {len(spans)} spans ({len(tool_spans)} TOOL), {got}")
    else:
        print("read SKIPPED (no hosted read reachable and no private core here); events and ops verified")
    print(f"selftest OK, run folder {rd.name}")


if __name__ == "__main__":
    main()
