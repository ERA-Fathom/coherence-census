"""
Re-map and re-read banked runs at no spend, after a change to deep_research_ops.py.

    python reread.py                 # every run under runs/
    python reread.py runs/<one run>  # one run

Rewrites ops.json and read.json in each run folder from the trace and messages already on disk, and updates
the run's row in the map (runs/research_map.db, or FATHOM_MAP_DB). The previous read.json is kept as
read.json.prev_<timestamp> the first time it changes. The read comes from the hosted service, or from the
private core on the dev machine when the service is unreachable from this shell.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from deep_research_ops import ops_from_trace, summary  # noqa: E402
from runner import write_map_row  # noqa: E402


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
    calls = json.load(open(run_dir / "tool_trace.json"))["calls"]
    messages = json.load(open(run_dir / "messages.json"))
    run = json.load(open(run_dir / "run.json"))
    if run.get("error"):
        print(f"{run_dir.name}: skipped (run error: {run['error'][:70]})")
        return
    final = ""
    ai = [m for m in messages if m.get("type") == "ai"]
    if ai:
        c = ai[-1]["data"].get("content")
        final = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    ops = ops_from_trace(calls, final)
    (run_dir / "ops.json").write_text(json.dumps(ops, indent=1))
    verdict = read([Op.from_dict(o) for o in ops])
    new = json.dumps(verdict.as_dict(), indent=1)
    old_p = run_dir / "read.json"
    if old_p.exists() and old_p.read_text() != new and not list(run_dir.glob("read.json.prev_*")):
        old_p.rename(run_dir / f"read.json.prev_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    old_p.write_text(new)
    s = summary(ops)
    by_kind = {}
    for f in verdict.findings:
        k = f.kind + ("/" + f.key[:40] if f.kind == "stale_reference" else "")
        by_kind[k] = by_kind.get(k, 0) + 1
    mw_p = run_dir / "middleware.json"
    mw = json.load(open(mw_p)) if mw_p.exists() else None
    write_map_row(Path(os.environ.get("FATHOM_MAP_DB") or HERE / "runs" / "research_map.db"), {
        "run": run_dir.name, "stamp": run_dir.name.split("_")[0], "question": run.get("question"), "model": run.get("model"),
        "elapsed_s": run.get("elapsed_s"), "error": run.get("error"), "tool_calls": len(calls), **s, "ops": len(ops),
        "coherent": int(verdict.coherent), "findings": len(verdict.findings), "findings_by_kind": json.dumps(by_kind),
        "middleware_ops": mw.get("ops_read") if isinstance(mw, dict) else None,
        "middleware_findings": len(mw.get("findings", [])) if isinstance(mw, dict) else None})
    print(f"{run_dir.name}: ops {len(ops)}, citations {s['report_citations']}, findings {len(verdict.findings)} {by_kind}")


if __name__ == "__main__":
    targets = [Path(a) for a in sys.argv[1:]] or sorted(p for p in (HERE / "runs").iterdir() if p.is_dir() and (p / "tool_trace.json").exists())
    rd = get_read()
    for t in targets:
        reread(t, rd)
