"""
Offline self-test of the runner: no network, no model spend.

Stands in a scripted LLM and a scripted Hacker News API, runs the vendored workflow through DBOS
exactly as the runner does, and checks that the step stream, the ops, and the read behave.
The scripted LLM repeats a query and cites one discussion the run never fetched, so the read
should return a duplicate_commit and a stale_reference. Run: python selftest_offline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "offline")
os.environ["FATHOM_MODEL"] = "scripted"
os.environ["FATHOM_ENDPOINT"] = os.environ.get("FATHOM_ENDPOINT", "offline")

HERE = Path(__file__).resolve().parent
sys.argv = ["runner.py", "postgres performance", "3"]
import runner  # noqa: E402
import hacker_news_agent.agent as agent_mod  # noqa: E402
import hacker_news_agent.api as api_mod  # noqa: E402

HITS = {
    "database internals": [{"objectID": "1004", "title": "How Postgres stores rows", "url": "", "points": 50, "num_comments": 3, "author": "d"}],
    "postgres performance": [{"objectID": "1001", "title": "Postgres 17 is fast", "url": "", "points": 300, "num_comments": 40, "author": "a"},
                             {"objectID": "1002", "title": "Tuning shared_buffers", "url": "", "points": 120, "num_comments": 0, "author": "b"}],
    "database tools": [{"objectID": "1003", "title": "pgcli", "url": "", "points": 80, "num_comments": 12, "author": "c"},
                       {"objectID": "1001", "title": "Postgres 17 is fast", "url": "", "points": 300, "num_comments": 40, "author": "a"}],
}
CALLS = {"n": 0}


class FakeResp:
    def __init__(self, hits): self._h = hits
    def raise_for_status(self): pass
    def json(self): return {"hits": self._h}


class FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, url, params=None):
        tags = params.get("tags", "")
        if tags.startswith("comment,story_"):
            sid = tags.split("story_")[1]
            return FakeResp([{"author": "x", "comment_text": f"comment on {sid}"}])
        return FakeResp(HITS.get(params.get("query"), []))


def fake_llm(messages, **kw):
    CALLS["n"] += 1
    sys_msg = messages[0]["content"]
    user = messages[1]["content"]
    if "research evaluation agent" in sys_msg:
        q = user.split("Query used:")[1].split("\n")[0].strip()
        return json.dumps({"insights": [f"insight about {q}"], "relevance_score": 8,
                           "summary": f"summary for {q}", "key_points": ["p"]})
    if "Generate focused follow-up queries" in sys_msg:
        if "COMMITTED STATE CHECK" in user:
            return json.dumps(["database internals"])
        # iteration 1 -> a new query; iteration 2 -> repeats the first query (a self-contradiction)
        return json.dumps(["database tools"]) if "iteration 1 " in user else json.dumps(["postgres performance"])
    if "research decision agent" in sys_msg:
        return json.dumps({"should_continue": True})
    if "research analyst" in sys_msg:
        return json.dumps({"report": "Postgres is fast, see [this](https://news.ycombinator.com/item?id=1001) and "
                                     "[tools](https://news.ycombinator.com/item?id=1003), and a claim with a "
                                     "[link the run never fetched](https://news.ycombinator.com/item?id=9999)."})
    raise AssertionError("unexpected prompt")


api_mod.httpx.Client = FakeClient
agent_mod.call_llm = fake_llm

# Run the workflow through the runner's own path. The read itself is hosted; with FATHOM_ENDPOINT=offline the
# runner saves ops.json and skips the verdict, and the assertions below check the step stream and the ops.
runner.main()

run_dirs = sorted((HERE / "runs").glob("*postgres-performance"))
rd = run_dirs[-1]
steps = json.load(open(rd / "dbos_steps.json"))["steps"]
ops = json.load(open(rd / "ops.json"))
names = [s["step_name"] for s in steps]
assert names.count("search_hackernews_step") == 3, names
assert names.count("synthesize_findings_step") == 1, names
assert all(s["ok"] for s in steps)
kinds = [(o["op"], o["kind"], o["key"]) for o in ops]
assert ("add", "research", "queries") in kinds
assert ("answer", "research", "report") in kinds
report_op = [o for o in ops if o["op"] == "answer"][0]
assert ["story", "9999"] in report_op["refs"], report_op["refs"]
dup_q = [o for o in ops if o["op"] == "add" and o["key"] == "queries"]
assert [o["value"] for o in dup_q] == ["postgres performance", "database tools", "postgres performance"], dup_q
if (rd / "read.json").exists():
    v = json.load(open(rd / "read.json"))
    fk = sorted(f["kind"] for f in v["findings"])
    print("findings:", fk)
    assert "stale_reference" in fk and "duplicate_commit" in fk, fk
else:
    print("no read.json (offline); run `fathom read %s/ops.json` to see duplicate_commit x1 and stale_reference x1" % rd.name)
print(f"selftest OK: {len(steps)} steps, {len(ops)} ops, {CALLS['n']} scripted LLM calls, run folder {rd.name}")
