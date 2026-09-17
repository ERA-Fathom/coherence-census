#!/usr/bin/env bash
# Run DBOS's published Hacker News research agent on a few topics and read each run.
#
#   cd Fathom_DBOS_HN_MacRun
#   export OPENROUTER_API_KEY=sk-or-...      # or OPENAI_API_KEY=sk-... to use OpenAI directly
#   bash run_mac.sh
#
# Each run lands in runs/<timestamp>_<topic>/ with the step stream, DBOS's own step table,
# the report, the ops, and the read's verdict. Nothing is written outside this folder.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${OPENAI_API_KEY:-}" && -n "${OPENROUTER_API_KEY:-}" ]]; then
  export OPENAI_API_KEY="$OPENROUTER_API_KEY"
  export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
  export FATHOM_MODEL="${FATHOM_MODEL:-openai/gpt-4o-mini}"
fi
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "set OPENAI_API_KEY (or OPENROUTER_API_KEY) in THIS terminal first, e.g.  export OPENROUTER_API_KEY=sk-or-..."; exit 1
fi

# Preflight: one authenticated request before any run, so a bad or empty key stops here.
PRE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}/models"
PRE_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OPENAI_API_KEY" "$PRE_URL" || echo 000)
if [[ "$PRE_CODE" != "200" ]]; then
  echo "key check failed ($PRE_CODE from $PRE_URL). The key in this terminal is empty or wrong; re-export it and rerun."; exit 1
fi
echo "key ok (${#OPENAI_API_KEY} chars), model=${FATHOM_MODEL:-gpt-4o-mini}, base=${OPENAI_BASE_URL:-api.openai.com}"

command -v uv >/dev/null || { echo "install uv first: brew install uv"; exit 1; }
if [[ ! -d .venv ]]; then
  uv venv -q --python 3.12 .venv
  uv pip install -q --python .venv/bin/python "dbos==2.31.0" "httpx>=0.25" "openai>=1.0" rich python-dotenv pydantic fathom-read
fi

TOPICS=("${@:-}")
if [[ -z "${TOPICS[0]}" ]]; then
  TOPICS=("postgres performance" "rust async" "kubernetes cost" "vector databases" "webassembly")
fi
ITERS="${FATHOM_ITERATIONS:-3}"

for t in "${TOPICS[@]}"; do
  echo; echo "=================================================================="
  echo "topic: $t   (max_iterations=$ITERS, model=${FATHOM_MODEL:-gpt-4o-mini})"
  echo "=================================================================="
  if ! .venv/bin/python runner.py "$t" "$ITERS"; then
    echo "RUN FAILED for topic '$t' (see the error above). Stopping."; exit 1
  fi
done

echo; echo "done. verdicts:"
for d in runs/*/; do
  if [[ -f "$d/read.json" ]]; then
    .venv/bin/python - "$d" <<'EOF'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1]); v = json.load(open(d / "read.json"))
kinds = {}
for f in v["findings"]: kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
print(f"  {d.name}: {'coherent' if v['coherent'] else ', '.join(f'{k} x{n}' for k, n in kinds.items())}")
EOF
  fi
done
