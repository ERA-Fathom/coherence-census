"""
Run CrewAI's published "write a book with flows" example once, record its event stream, and read it.

    python runner.py "<book title>" "<topic>"

What it does, in order:
  1. Imports the vendored flow unchanged (write_a_book_with_flows/, see VENDORED_FROM.txt).
  2. Attaches fathom_read's public CrewAI capture listener to the crew event bus, and Arize's own
     OpenInference CrewAI instrumentor with a file exporter, so the same run also leaves the spans
     Phoenix would store (phoenix_spans.json).
  3. Overrides the example's model object by class attribute when FATHOM_MODEL is set (the vendored
     files hardcode gpt-4o); the substitution is recorded in flow_state.json.
  4. Kicks the flow off, then writes the run folder: crewai_events.json (the event stream as captured),
     flow_state.json (the flow's own outline and chapters), book.md (the joined book), ops.json (the
     committed-state ops book_flow_ops.py maps from the events), read.json (the read's verdict),
     phoenix_spans.json and read_spans.json (the same run read off the OpenInference TOOL spans alone,
     through the public openinference adapter and serper_map.json).
  5. Appends one row per run to runs/book_map.db (SQLite), the map this folder accumulates.

Environment:
  OPENROUTER_API_KEY   the model key (run_mac.sh loads it from the macOS Keychain once you store it)
  SERPER_API_KEY       the search key the example's SerperDevTool needs
  FATHOM_MODEL         e.g. openrouter/openai/gpt-4o-mini (default: leave the example's gpt-4o alone)
  FATHOM_API_KEY       key for the read (default: the rate-limited demo key)
  FATHOM_MAP_DB        where the SQLite map lives (default runs/book_map.db)
  FATHOM_CHAPTERS      ask the outliner for about this many chapters (pressure lever; default: the outliner decides)
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

from book_flow_ops import ops_from_events, summary  # noqa: E402


def override_model():
    """Swap the example's LLM object for the one FATHOM_MODEL names, without editing the vendored files."""
    model = os.environ.get("FATHOM_MODEL")
    if not model:
        return None
    from crewai import LLM
    from write_a_book_with_flows.crews.outline_book_crew import outline_crew as oc
    from write_a_book_with_flows.crews.write_book_chapter_crew import write_book_chapter_crew as wc
    oc.OutlineCrew.llm = LLM(model=model)
    wc.WriteBookChapterCrew.llm = LLM(model=model)
    return model


class FileSpanExporter:
    """Collect OpenTelemetry spans in memory and write them in the shape fathom's openinference adapter reads."""

    def __init__(self):
        self.spans = []

    def export(self, spans):
        for sp in spans:
            code = getattr(getattr(sp, "status", None), "status_code", None)
            self.spans.append({
                "name": sp.name, "span_id": format(sp.context.span_id, "016x"),
                "parent_id": format(sp.parent.span_id, "016x") if sp.parent else None,
                "start_time": sp.start_time, "end_time": sp.end_time,
                "status_code": getattr(code, "name", str(code or "UNSET")),
                "attributes": {k: (v if isinstance(v, (str, int, float, bool)) or v is None else json.dumps(jsonable(v)))
                               for k, v in dict(sp.attributes or {}).items()},
            })
        from opentelemetry.sdk.trace.export import SpanExportResult
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def arguments_from_input_value(spans):
    """Arize's CrewAI instrumentor puts the tool's argument SCHEMA in tool.parameters and the call's actual
    arguments in input.value. fathom-read 0.3.0's openinference adapter reads tool.parameters only, so a
    TOOL span whose tool.parameters looks like a schema gets input.value copied over it here. fathom-read
    0.3.1 carries this fallback in the adapter itself; this shim keeps 0.3.0 runs honest."""
    out = []
    for sp in spans:
        a = dict(sp.get("attributes", {}))
        if str(a.get("openinference.span.kind", "")).upper() == "TOOL":
            try:
                params = json.loads(a.get("tool.parameters") or "null")
            except ValueError:
                params = None
            if (not isinstance(params, dict) or "properties" in params or params.get("type") == "object") and a.get("input.value"):
                a["tool.parameters"] = a["input.value"]
        out.append({**sp, "attributes": a})
    return out


def instrument_openinference():
    """Arize's own CrewAI instrumentor, exporting to a file instead of a Phoenix server. Returns the exporter."""
    try:
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from openinference.instrumentation.crewai import CrewAIInstrumentor
    except ImportError as e:
        print(f"openinference capture off ({e}); the run still records the CrewAI event stream")
        return None
    exporter = FileSpanExporter()
    provider = trace_sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    CrewAIInstrumentor().instrument(tracer_provider=provider)
    return exporter


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


def write_map_row(db: Path, row: dict):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS book_runs (
        run TEXT PRIMARY KEY, stamp TEXT, title TEXT, topic TEXT, model TEXT, elapsed_s REAL, error TEXT,
        events INTEGER, chapters INTEGER, searches INTEGER, distinct_queries INTEGER, repeated_searches INTEGER,
        outline_titles INTEGER, ops INTEGER, coherent INTEGER, findings INTEGER, findings_by_kind TEXT,
        spans INTEGER, spans_findings INTEGER)""")
    con.execute("INSERT OR REPLACE INTO book_runs VALUES (:run,:stamp,:title,:topic,:model,:elapsed_s,:error,:events,"
                ":chapters,:searches,:distinct_queries,:repeated_searches,:outline_titles,:ops,:coherent,:findings,"
                ":findings_by_kind,:spans,:spans_findings)", row)
    con.commit()
    con.close()


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    title = sys.argv[1]
    topic = sys.argv[2] if len(sys.argv) > 2 else title
    for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(k):
            break
    else:
        raise SystemExit("set OPENROUTER_API_KEY (or OPENAI_API_KEY) first")
    if not os.environ.get("SERPER_API_KEY"):
        raise SystemExit("set SERPER_API_KEY first (the example's search tool needs it)")

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = HERE / "runs" / f"{stamp}_{slug}"
    run_dir.mkdir(parents=True)
    os.chdir(run_dir)  # the flow saves its book as ./<title>.md

    model = override_model()
    from fathom_read.capture.crewai import FathomListener
    from write_a_book_with_flows.main import BookFlow, BookState

    listener = FathomListener(str(run_dir / "crewai_events.json"))  # keep the reference; the bus holds it weakly
    spans = instrument_openinference()
    flow = BookFlow()
    goal = (f"The goal of this book is to give a reader a complete, current, well-organized account of {topic}. "
            "Each chapter should stand on its own and fit the outline, with no chapter repeating another.")
    n_ch = os.environ.get("FATHOM_CHAPTERS")  # pressure lever: a longer outline means more crews over one record
    if n_ch:
        goal += f" The book should have about {int(n_ch)} chapters."
    t0 = time.time()
    run_error = None
    try:
        flow.kickoff(inputs={"title": title, "topic": topic, "goal": goal})
    except Exception as e:  # noqa: BLE001
        run_error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0
    listener.close()

    st: BookState = flow.state
    state = {"title": st.title, "topic": st.topic, "goal": st.goal, "model": model or "gpt-4o (the example's own)",
             "chapters_requested": int(n_ch) if n_ch else None,
             "elapsed_s": round(elapsed, 1), "error": run_error,
             "book_outline": jsonable(st.book_outline), "book": jsonable(st.book)}
    (run_dir / "flow_state.json").write_text(json.dumps(state, indent=1))
    joined = "".join(f"# {c['title']}\n\n{c['content']}\n\n" for c in state["book"])
    (run_dir / "book.md").write_text(joined)

    events = listener.events
    ops = ops_from_events(events, state)
    (run_dir / "ops.json").write_text(json.dumps(ops, indent=1))
    s = summary(ops)

    verdict = None
    from fathom_read.client import read, ReadError
    from fathom_read.cli import render
    from fathom_read.ops import Op
    try:
        verdict = read([Op.from_dict(o) for o in ops])
        (run_dir / "read.json").write_text(json.dumps(verdict.as_dict(), indent=1))
        print("\n" + render(verdict, title=f"fathom read: {title}"))
    except ReadError as e:
        print(f"\nfathom read failed: {e}\n(ops.json is saved; run `fathom read ops.json` later)")

    by_kind = {}
    if verdict is not None:
        for f in verdict.findings:
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1

    # The same run, read off the OpenInference spans alone (what Phoenix stores), through the public adapter.
    n_spans, spans_findings = None, None
    if spans is not None:
        (run_dir / "phoenix_spans.json").write_text(json.dumps({"spans": spans.spans}, indent=1))
        n_spans = len(spans.spans)
        from fathom_read.adapters import openinference as oi_adapter
        span_ops = oi_adapter.load({"spans": arguments_from_input_value(spans.spans)}, mapping_path=str(HERE / "serper_map.json"))
        try:
            sv = read(span_ops)
            (run_dir / "read_spans.json").write_text(json.dumps(sv.as_dict(), indent=1))
            spans_findings = len(sv.findings)
            print("\n" + render(sv, title=f"fathom read, off the OpenInference spans: {title}"))
        except ReadError as e:
            print(f"\nspan read failed: {e}\n(phoenix_spans.json is saved; run `fathom read phoenix_spans.json --format openinference --map serper_map.json` later)")
    write_map_row(Path(os.environ.get("FATHOM_MAP_DB") or HERE / "runs" / "book_map.db"), {
        "run": run_dir.name, "stamp": stamp, "title": title, "topic": topic, "model": state["model"],
        "elapsed_s": state["elapsed_s"], "error": run_error, "events": len(events), **s, "ops": len(ops),
        "coherent": None if verdict is None else int(verdict.coherent),
        "findings": None if verdict is None else len(verdict.findings), "findings_by_kind": json.dumps(by_kind),
        "spans": n_spans, "spans_findings": spans_findings})

    print(f"\nrun folder: {run_dir}\nevents: {len(events)}  chapters: {s['chapters']}  searches: {s['searches']} "
          f"({s['distinct_queries']} distinct, {s['repeated_searches']} repeated)  ops: {len(ops)}")
    if run_error:
        raise SystemExit(f"flow failed after {elapsed:.0f}s: {run_error}\n(events and state saved to {run_dir})")


if __name__ == "__main__":
    main()
