"""
Regenerate the census table from census.json by running every row's command.

    python scripts/build.py            # runs each row, rewrites the table in README.md
    python scripts/build.py --check    # runs each row, exits 1 if any row's verdict is not what the row claims

Each row's command sends the trace's ops to the hosted read (the bundled demo key is enough).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START, END = "<!-- census:start -->", "<!-- census:end -->"


def run_row(row: dict) -> dict:
    cmd = row["cmd"].split() + ["--json"]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = p.stdout.strip()
    if not out:
        return {"error": (p.stderr.strip() or f"exit {p.returncode}")[:200]}
    v = json.loads(out)
    kinds = Counter(f["kind"] for f in v["findings"])
    return {"coherent": v["coherent"], "kinds": dict(kinds), "ops_read": v["ops_read"], "ops_rejected": v["ops_rejected"]}


def verdict_text(r: dict) -> str:
    if "error" in r:
        return f"read unavailable: {r['error']}"
    if r["coherent"]:
        return "coherent"
    return ", ".join(f"`{k}` x{n}" for k, n in sorted(r["kinds"].items()))


def table(rows: list, results: list) -> str:
    head = "| Agent | Trace the read consumes | What the read caught in the study | The bundled trace returns | Reproduce | Study |\n|---|---|---|---|---|---|\n"
    body = ""
    for row, r in zip(rows, results):
        body += f"| {row['agent']} | {row['trace']} | {row['caught']} | {verdict_text(r)} | `{row['cmd']}` | [study]({row['study']}) |\n"
    return head + body


def main():
    rows = json.load(open(ROOT / "census.json"))
    results = [run_row(r) for r in rows]
    for row, r in zip(rows, results):
        print(f"{row['id']:20s} {verdict_text(r)}")
    if "--check" in sys.argv:
        bad = [row["id"] for row, r in zip(rows, results) if "error" in r or r["coherent"]]
        if bad:
            print("rows whose trace did not reproduce a finding:", bad)
            sys.exit(1)
        return
    readme = (ROOT / "README.md").read_text()
    new = re.sub(f"{START}.*?{END}", f"{START}\n{table(rows, results)}{END}", readme, flags=re.S)
    (ROOT / "README.md").write_text(new)


if __name__ == "__main__":
    main()
