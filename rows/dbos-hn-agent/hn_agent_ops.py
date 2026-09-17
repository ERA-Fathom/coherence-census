"""
Map the DBOS Hacker News research agent's step stream to committed-state ops.

The step stream is what the runner records: one entry per DBOS step, with the step's
name, its arguments, its result, and whether it succeeded. This file is the whole
mapping for this agent. Every line says what the agent commits and where it cites.

    search_hackernews_step   -> add    research/queries        <query>          (a search the agent chose)
                                set    story/<objectID>        <title>          (a discussion the agent now holds)
    get_comments_step        -> add    research/comments_read  <story_id>       (a thread the agent read)
    evaluate_results_step    -> set    finding/<query>         <summary>        cites story/<id> for each top story
    generate_follow_ups_step -> set    research/current_topic  <next query>
    should_continue_step     -> commit research/research                        (only when the agent decides to stop)
    synthesize_findings_step -> answer research/report         <report>         cites story/<id> for every HN link in the report

What the read can then say:
    stale_reference on the report  -> the report links to a discussion the run never retrieved
    duplicate_commit on queries    -> the agent repeated a search it had already committed
    duplicate_commit on comments   -> the agent re-read a thread it already held
    superseded_value on a finding  -> the agent rewrote a finding for a query it had already evaluated

Usage:
    python hn_agent_ops.py dbos_steps.json > ops.json
    fathom read ops.json
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List

HN_ITEM = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")


def _norm_query(q: Any) -> str:
    return " ".join(str(q or "").lower().split())


def ops_from_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []

    def emit(op: str, kind: str, key: str, value=None, ok=True, refs=None, source=""):
        ops.append({"op": op, "kind": kind, "key": str(key), "value": None if value is None else str(value),
                    "ok": bool(ok), "refs": [[k, str(v)] for k, v in (refs or [])], "step": len(ops),
                    "source": source})

    for s in steps:
        name = s.get("step_name", "")
        args = s.get("args") or {}
        res = s.get("result")
        ok = bool(s.get("ok", True))

        if name == "search_hackernews_step":
            emit("add", "research", "queries", _norm_query(args.get("query")), ok, source=name)
            for hit in (res or []) if ok else []:
                oid = hit.get("objectID")
                if oid:
                    emit("set", "story", oid, hit.get("title") or "", ok, source=name)

        elif name == "get_comments_step":
            emit("add", "research", "comments_read", str(args.get("story_id")), ok, source=name)

        elif name == "evaluate_results_step":
            q = _norm_query(args.get("query"))
            summary = (res or {}).get("summary") if isinstance(res, dict) else None
            refs = [("story", st.get("objectID")) for st in ((res or {}).get("top_stories") or []) if st.get("objectID")] if isinstance(res, dict) else []
            emit("set", "finding", q, summary, ok, refs=refs, source=name)

        elif name == "generate_follow_ups_step":
            if ok and res:
                emit("set", "research", "current_topic", _norm_query(res), ok, source=name)

        elif name == "should_continue_step":
            if ok and res is False:
                emit("commit", "research", "research", ok=ok, source=name)

        elif name == "synthesize_findings_step":
            report = (res or {}).get("report", "") if isinstance(res, dict) else ""
            cited = []
            for m in HN_ITEM.finditer(report or ""):
                if m.group(1) not in cited:
                    cited.append(m.group(1))
            emit("answer", "research", "report", report, ok, refs=[("story", c) for c in cited], source=name)

    return ops


if __name__ == "__main__":
    doc = json.load(open(sys.argv[1]))
    steps = doc.get("steps", doc) if isinstance(doc, dict) else doc
    json.dump(ops_from_steps(steps), sys.stdout, indent=1)
