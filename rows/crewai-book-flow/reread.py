"""
Re-map and re-read banked runs at no spend, after a change to book_flow_ops.py.

    python reread.py                 # every run under runs/
    python reread.py runs/<one run>  # one run

Rewrites ops.json, read.json and read_spans.json in each run folder from the events and spans already on
disk, and updates the run's row in the map (runs/book_map.db, or FATHOM_MAP_DB). The previous read.json
is kept as read.json.prev_<timestamp> the first time it changes. The read comes from the hosted service,
or from the private core on the dev machine when the service is unreachable from this shell.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from book_flow_ops import ops_from_events, summary  # noqa: E402
from runner import write_map_row, arguments_from_input_value  # noqa: E402


def get_read():
    import fathom_read.client as client
    core_py = HERE.parent / "_publish" / "fathom-core" / "src" / "fathom_read" / "core.py"

    def read(ops):
        try:
            return client.read(ops, timeout=8)
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

    return read


def reread(run_dir: Path, read):
    from fathom_read.ops import Op
    from fathom_read.adapters import openinference as oi_adapter
    events = json.load(open(run_dir / "crewai_events.json"))["events"]
    state = json.load(open(run_dir / "flow_state.json"))
    ops = ops_from_events(events, state)
    (run_dir / "ops.json").write_text(json.dumps(ops, indent=1))
    verdict = read([Op.from_dict(o) for o in ops])
    new = json.dumps(verdict.as_dict(), indent=1)
    old_p = run_dir / "read.json"
    if old_p.exists() and old_p.read_text() != new and not list(run_dir.glob("read.json.prev_*")):
        old_p.rename(run_dir / f"read.json.prev_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    old_p.write_text(new)
    n_spans, spans_findings = None, None
    sp = run_dir / "phoenix_spans.json"
    if sp.exists():
        spans = json.load(open(sp))["spans"]
        n_spans = len(spans)
        sv = read(oi_adapter.load({"spans": arguments_from_input_value(spans)}, mapping_path=str(HERE / "serper_map.json")))
        (run_dir / "read_spans.json").write_text(json.dumps(sv.as_dict(), indent=1))
        spans_findings = len(sv.findings)
    s = summary(ops)
    by_kind = {}
    for f in verdict.findings:
        by_kind[f.kind + "/" + f.key if f.kind == "stale_reference" else f.kind] = by_kind.get(f.kind + "/" + f.key if f.kind == "stale_reference" else f.kind, 0) + 1
    write_map_row(Path(os.environ.get("FATHOM_MAP_DB") or HERE / "runs" / "book_map.db"), {
        "run": run_dir.name, "stamp": run_dir.name.split("_")[0], "title": state.get("title"), "topic": state.get("topic"),
        "model": state.get("model"), "elapsed_s": state.get("elapsed_s"), "error": state.get("error"), "events": len(events),
        **s, "ops": len(ops), "coherent": int(verdict.coherent), "findings": len(verdict.findings),
        "findings_by_kind": json.dumps(by_kind), "spans": n_spans, "spans_findings": spans_findings})
    print(f"{run_dir.name}: ops {len(ops)}, findings {len(verdict.findings)} {by_kind}, span findings {spans_findings}")


if __name__ == "__main__":
    targets = [Path(a) for a in sys.argv[1:]] or sorted(p for p in (HERE / "runs").iterdir() if p.is_dir() and (p / "crewai_events.json").exists())
    rd = get_read()
    for t in targets:
        reread(t, rd)
