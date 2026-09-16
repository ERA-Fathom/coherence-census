"""
Map the deepagents "deep_research" example's own tool-call trace to committed-state ops.

The trace is what the runner's callback handler records: every tool call the orchestrator and its
research sub-agents make, in order, with the tool's name, its arguments, whether it succeeded, the
URLs its result carried, and which task() delegation it ran under. This file is the whole mapping.
Every line says what the agent commits and where it cites.

    write_todos(todos)               -> set     plan/todos              <todos>          the plan the orchestrator committed to
    task(description, subagent_type) -> add     research/delegations    <description>    a topic handed to a sub-agent
    tavily_search(query)             -> add     research/queries        <query>          a search any agent chose (one collection
                                                                                          for the whole run, since sub-agents cannot
                                                                                          see each other's searches)
                                        set     source/<url>            <title>          a page the run now holds, per result
    write_file(path, content)        -> set     file/<path>             <content>        a file written; /final_report.md also cites
                                                                                          source/<url> for every URL the report lists
    final message (no report file)   -> answer  research/report         <message>        the report as delivered, citing its URLs
    edit_file(path, old, new)        -> set     file/<path>             <folded content> the file after the edit
    read_file, ls, glob, grep, think_tool -> nothing (reads)

What the read can then say:
    duplicate_commit on queries      -> a sub-agent repeated a search the run had already made (its own, or a sibling's)
    duplicate_commit on delegations  -> the orchestrator delegated a topic it had already delegated
    stale_reference on the report    -> the final report cites a source no search in the run ever returned
    duplicate_commit on a file       -> the same file written twice with the same content (reads from 0.4.0 on)

Usage:
    python deep_research_ops.py tool_trace.json > ops.json
    fathom read ops.json
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List

URL = re.compile(r"https?://[^\s\)\]>\"'`]+")
READ_ONLY = {"read_file", "ls", "glob", "grep", "think_tool"}


def norm(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def norm_url(u: str) -> str:
    return u.strip().rstrip(".,;:").rstrip("/").lower()


def ops_from_trace(calls: List[Dict[str, Any]], final_message: str = "") -> List[Dict[str, Any]]:
    """final_message: the orchestrator's last message. When the run never wrote /final_report.md (the example's
    workflow asks for the file, and the model sometimes answers in the message instead), the message with its
    Sources list is the report the run delivered, and it is read the same way."""
    ops: List[Dict[str, Any]] = []
    files: Dict[str, str] = {}

    def emit(op: str, kind: str, key: str, value=None, ok=True, refs=None, source=""):
        ops.append({"op": op, "kind": kind, "key": str(key), "value": None if value is None else str(value),
                    "ok": bool(ok), "refs": [[k, str(v)] for k, v in (refs or [])], "step": len(ops),
                    "source": source})

    for c in calls:
        name = c.get("tool", "")
        args = c.get("args") or {}
        ok = bool(c.get("ok", True))
        who = c.get("agent") or "orchestrator"
        src = f"{who}.{name}"
        if name in READ_ONLY:
            continue
        if name == "write_todos":
            emit("set", "plan", "todos", json.dumps(args.get("todos"), sort_keys=True), ok, source=src)
        elif name == "task":
            emit("add", "research", "delegations", norm(args.get("description")), ok, source=src)
        elif name == "tavily_search":
            emit("add", "research", "queries", norm(args.get("query")), ok, source=src)
            for u, title in (c.get("results") or []):
                emit("set", "source", norm_url(u), title or "", ok, source=src)
        elif name == "write_file":
            path = str(args.get("file_path") or args.get("path") or "")
            content = str(args.get("content") or "")
            if ok:
                files[path] = content
            refs = []
            if path.endswith("final_report.md"):
                seen = set()
                for m in URL.finditer(content):
                    u = norm_url(m.group(0))
                    if u not in seen:
                        seen.add(u)
                        refs.append(("source", u))
            emit("set", "file", path, content, ok, refs=refs, source=src)
        elif name == "edit_file":
            path = str(args.get("file_path") or args.get("path") or "")
            old, new = str(args.get("old_string") or ""), str(args.get("new_string") or "")
            cur = files.get(path, "")
            folded = cur.replace(old, new) if args.get("replace_all") else cur.replace(old, new, 1)
            if ok:
                files[path] = folded
            emit("set", "file", path, folded, ok, source=src)

    if not any(k.endswith("final_report.md") for k in files) and final_message:
        seen, refs = set(), []
        for m in URL.finditer(final_message):
            u = norm_url(m.group(0))
            if u not in seen:
                seen.add(u)
                refs.append(("source", u))
        emit("answer", "research", "report", final_message, True, refs=refs, source="orchestrator.final_message")
    return ops


def summary(ops: List[Dict[str, Any]]) -> Dict[str, Any]:
    qs = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "queries" and o["ok"]]
    ds = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "delegations" and o["ok"]]
    report = [o for o in ops if (o["op"] == "set" and o["kind"] == "file" and o["key"].endswith("final_report.md"))
              or (o["op"] == "answer" and o["key"] == "report")]
    return {"searches": len(qs), "distinct_queries": len(set(qs)), "repeated_searches": len(qs) - len(set(qs)),
            "delegations": len(ds), "distinct_delegations": len(set(ds)),
            "sources_held": len({o["key"] for o in ops if o["kind"] == "source"}),
            "report_citations": len(report[-1]["refs"]) if report else 0,
            "files_written": len({o["key"] for o in ops if o["kind"] == "file"}),
            # "file": /final_report.md written; "message": the final message carried a report (cites a source
            # or runs long enough to be one); "none": the run ended without delivering a report at all
            "report": ("file" if report and report[-1]["op"] == "set" else
                       "message" if report and (report[-1]["refs"] or len(str(report[-1]["value"])) >= 600) else "none")}


if __name__ == "__main__":
    doc = json.load(open(sys.argv[1]))
    calls = doc.get("calls", doc) if isinstance(doc, dict) else doc
    final = ""
    if len(sys.argv) > 2:  # messages.json, for the final message
        msgs = json.load(open(sys.argv[2]))
        ai = [m for m in msgs if m.get("type") == "ai"]
        if ai:
            c = ai[-1]["data"].get("content")
            final = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    json.dump(ops_from_trace(calls, final), sys.stdout, indent=1)
