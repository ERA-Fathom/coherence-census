"""
Map the CrewAI "write a book with flows" example's own event stream to committed-state ops.

The flow runs an outline crew (a researcher with a search tool, then an outliner) and one chapter
crew per outlined chapter (a researcher with the same search tool, then a writer), and joins the
chapters into a book. The events come from CrewAI's event bus as fathom_read.capture.crewai records
them: tool_usage_finished / tool_usage_error (tool name and arguments) and task_completed (the task's
raw output). flow_state.json is the flow's own state after kickoff (the outline it committed and the
chapters it produced), written by the runner from the BookState object.

This file is the whole mapping. Every line says what the crew commits and where it cites.

    search tool call (any crew)        -> add     research/queries         <query>      one collection for the whole book
    generate_outline task              -> set     chapter_title/<title>    chapter <i>  the chapter titles the book committed to
                                          add     outline/titles           <title>      the outline's own no-duplicate rule
                                          set     chapter_no/<n>           <title>      chapter numbers the book holds
                                          commit  outline/outline                       the outline is final before writing begins
    write_chapter task                 -> set     chapter/<returned title> <content>    cites chapter_title/<returned title>
                                                                                        and chapter_no/<k> for every "Chapter k"
                                                                                        the text mentions
    join_and_save_chapter (flow state) -> answer  book/<title>             <headings>   cites chapter/<title> for every outlined
                                                                                        chapter, since the book is saved as finished

What the read can then say:
    duplicate_commit on queries        -> a chapter's researcher repeated a search the book already ran
                                          (its own, the outline's, or another chapter's)
    duplicate_commit on titles         -> the outliner duplicated a chapter it was told not to duplicate
    stale_reference on chapter_title   -> the writer delivered a chapter under a title the outline never committed
    stale_reference on chapter_no      -> the chapter cites a chapter number the book does not contain
    stale_reference on chapter         -> the book was saved as finished without a chapter the outline committed to
    post_commit_mutation on outline    -> something rewrote the outline after the book committed to it

Usage:
    python book_flow_ops.py crewai_events.json flow_state.json > ops.json
    fathom read ops.json
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

SEARCH_TOOLS = ("search the internet with serper", "search_the_internet_with_serper", "serperdevtool")
CHAPTER_REF = re.compile(r"\bchapter\s+(\d{1,2})\b", re.IGNORECASE)


def norm(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def _json_from(text: str) -> Optional[Any]:
    """Parse the JSON a pydantic-output task leaves in its raw output, tolerating fences and prose."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):] if "{" in t else t
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def _outline_from(events: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, str]]:
    """The outline as the crew emitted it (task output), with the flow state as the fallback parse."""
    for ev in events:
        if ev.get("type") == "task_completed" and "outline" in norm(ev.get("task_name")):
            doc = _json_from(ev.get("output") or "")
            if isinstance(doc, dict) and isinstance(doc.get("chapters"), list):
                return [{"title": str(c.get("title", "")), "description": str(c.get("description", ""))}
                        for c in doc["chapters"] if isinstance(c, dict)]
    return [{"title": str(c.get("title", "")), "description": str(c.get("description", ""))}
            for c in (state.get("book_outline") or [])]


def ops_from_events(events: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []

    def emit(op: str, kind: str, key: str, value=None, ok=True, refs=None, source=""):
        ops.append({"op": op, "kind": kind, "key": str(key), "value": None if value is None else str(value),
                    "ok": bool(ok), "refs": [[k, str(v)] for k, v in (refs or [])], "step": len(ops),
                    "source": source})

    outline_done = False
    chapter_idx = 0
    outline = _outline_from(events, state)
    outline_titles = {norm(c["title"]) for c in outline}

    for ev in events:
        t = ev.get("type")
        if t in ("tool_usage_finished", "tool_usage_error"):
            name = norm(ev.get("tool_name"))
            if name in SEARCH_TOOLS or "serper" in name:
                args = ev.get("tool_args") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"search_query": args}
                q = norm(args.get("search_query") or args.get("query") or args.get("q"))
                ok = t == "tool_usage_finished" and not ev.get("failure")
                phase = "chapter" if outline_done else "outline"
                emit("add", "research", "queries", q, ok, source=f"{phase}.search")

        elif t == "task_completed":
            task = norm(ev.get("task_name"))
            if "outline" in task and not outline_done:
                for i, ch in enumerate(outline, start=1):
                    # value carries a space on purpose: a bare index would read as a token the core tracks
                    emit("set", "chapter_title", norm(ch["title"]), f"chapter {i}", True, source="generate_outline")
                    emit("add", "outline", "titles", norm(ch["title"]), True, source="generate_outline")
                    emit("set", "chapter_no", i, ch["title"], True, source="generate_outline")
                emit("commit", "outline", "outline", ok=True, source="generate_outline")
                outline_done = True
            elif "write" in task and "chapter" in task:
                chapter_idx += 1
                raw = ev.get("output") or ""
                doc = _json_from(raw)
                title, content = "", raw
                if isinstance(doc, dict) and doc.get("title"):
                    title = str(doc.get("title", ""))
                    content = str(doc.get("content", ""))
                else:
                    # a non-JSON output (a failed crew's message, say): find the flow's own record of this
                    # chapter by content, since chapters can finish in any order under crewai 1.x
                    for ch in state.get("book") or []:
                        c = str(ch.get("content", ""))
                        if c and (c == raw.strip() or c in raw or raw.strip() in c):
                            title, content = str(ch.get("title", "")), c
                            break
                refs = [("chapter_title", norm(title))] if title else []
                for m in CHAPTER_REF.finditer(content):
                    k = int(m.group(1))
                    if ("chapter_no", str(k)) not in refs:
                        refs.append(("chapter_no", str(k)))
                key = norm(title) or f"chapter_{chapter_idx}"
                emit("set", "chapter", key, content, True, refs=refs, source="write_chapter")

    # The flow's join step assembles the book from the chapters it holds and saves it as finished. The
    # outline was the commitment, so the finished book cites every outlined chapter; one the writers never
    # delivered (or delivered under another title) reads as a stale reference.
    if outline_done and outline:
        book_title = norm(state.get("title")) or "book"
        emit("answer", "book", book_title, "\n".join(f"# {c.get('title','')}" for c in (state.get("book") or [])), True,
             refs=[("chapter", norm(c["title"])) for c in outline], source="join_and_save_chapter")

    return ops


def summary(ops: List[Dict[str, Any]]) -> Dict[str, Any]:
    qs = [o["value"] for o in ops if o["op"] == "add" and o["key"] == "queries" and o["ok"]]
    return {"chapters": sum(1 for o in ops if o["op"] == "set" and o["kind"] == "chapter"),
            "searches": len(qs), "distinct_queries": len(set(qs)), "repeated_searches": len(qs) - len(set(qs)),
            "outline_titles": sum(1 for o in ops if o["op"] == "set" and o["kind"] == "chapter_title")}


if __name__ == "__main__":
    ev_doc = json.load(open(sys.argv[1]))
    events = ev_doc.get("events", ev_doc) if isinstance(ev_doc, dict) else ev_doc
    state = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else {}
    json.dump(ops_from_events(events, state), sys.stdout, indent=1)
