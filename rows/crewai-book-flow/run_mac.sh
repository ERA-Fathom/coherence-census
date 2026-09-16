#!/usr/bin/env bash
# Run CrewAI's published "write a book with flows" example on a few book titles and read each run.
#
#   cd Fathom_CrewAI_Book_MacRun
#   bash run_mac.sh                                   # the default three titles
#   bash run_mac.sh "Title one|topic one" "Title two"  # your own (title|topic, topic optional)
#
# Keys. Store each once in the macOS Keychain and this script loads them every time:
#   security add-generic-password -a "$USER" -s OPENROUTER_API_KEY -w     # prompts for the value
#   security add-generic-password -a "$USER" -s SERPER_API_KEY -w
# An exported OPENROUTER_API_KEY / SERPER_API_KEY in this terminal wins over the Keychain.
#
# Model. The example hardcodes gpt-4o; FATHOM_MODEL overrides it through OpenRouter without touching
# the vendored code (default below: openai/gpt-4o-mini). FATHOM_MODEL=openrouter/openai/gpt-4o for the
# example's own model.
set -euo pipefail
cd "$(dirname "$0")"

keychain() { security find-generic-password -a "$USER" -s "$1" -w 2>/dev/null || true; }
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-$(keychain OPENROUTER_API_KEY)}"
export SERPER_API_KEY="${SERPER_API_KEY:-$(keychain SERPER_API_KEY)}"
if [[ -z "$OPENROUTER_API_KEY" ]]; then
  echo "no OpenRouter key. Store it once:  security add-generic-password -a \"\$USER\" -s OPENROUTER_API_KEY -w"; exit 1
fi
if [[ -z "$SERPER_API_KEY" ]]; then
  echo "no Serper key (the example's search tool). Get one at serper.dev, then store it once:"
  echo "  security add-generic-password -a \"\$USER\" -s SERPER_API_KEY -w"; exit 1
fi
export FATHOM_MODEL="${FATHOM_MODEL:-openrouter/openai/gpt-4o-mini}"
export FATHOM_CHAPTERS="${FATHOM_CHAPTERS:-}"   # e.g. 14, to lengthen the outline (pressure lever)
export CREWAI_DISABLE_TELEMETRY=true CREWAI_TRACING_ENABLED=false CREWAI_TESTING=true  # the last one only silences the first-run trace prompt

# Preflight: one authenticated request per key before any run, so a bad key stops here.
code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/models || echo 000)
[[ "$code" == "200" ]] || { echo "OpenRouter key check failed ($code). Re-store the key and rerun."; exit 1; }
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "X-API-KEY: $SERPER_API_KEY" -H "content-type: application/json" -d '{"q":"ping","num":1}' https://google.serper.dev/search || echo 000)
[[ "$code" == "200" ]] || { echo "Serper key check failed ($code). Re-store the key and rerun."; exit 1; }
echo "keys ok, model=$FATHOM_MODEL"

command -v uv >/dev/null || { echo "install uv first: brew install uv"; exit 1; }
if [[ ! -d .venv ]]; then
  # crewai pinned to the release the self-test ran on; the vendored example carries the one-line
  # kickoff_async fix it needs on 1.x (see README_MAC.md, "Version note"). Arize's own CrewAI
  # instrumentor records the OpenInference spans the same run leaves for the second read.
  uv venv -q --python 3.12 .venv
  uv pip install -q --python .venv/bin/python "crewai[tools]==1.15.21" "fathom-read>=0.3.0" \
      "openinference-instrumentation-crewai>=1.1.18" "opentelemetry-sdk>=1.20.0"
fi

TITLES=("$@")
if [[ ${#TITLES[@]} -eq 0 ]]; then
  TITLES=("The State of AI Agents in Enterprise Software, 2026|how enterprises deploy, govern and pay for AI agents in 2026"
          "Durable Execution for AI Workflows|durable execution engines and how AI agent workflows use them"
          "Agent Memory and Long-Horizon Coherence|how AI agents keep state over long tasks and where that fails")
fi

for spec in "${TITLES[@]}"; do
  title="${spec%%|*}"; topic="${spec#*|}"; [[ "$topic" == "$spec" ]] && topic="$title"
  echo; echo "=================================================================="
  echo "book: $title   (model=$FATHOM_MODEL, chapters=${FATHOM_CHAPTERS:-outliner decides})"
  echo "=================================================================="
  if ! .venv/bin/python runner.py "$title" "$topic"; then
    echo "RUN FAILED for '$title' (see the error above). Stopping."; exit 1
  fi
done

echo; echo "done. verdicts:"
for d in runs/*/; do
  [[ -f "$d/read.json" ]] || continue
  .venv/bin/python - "$d" <<'EOF'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1]); v = json.load(open(d / "read.json"))
kinds = {}
for f in v["findings"]: kinds[f["kind"] + "/" + f["key"]] = kinds.get(f["kind"] + "/" + f["key"], 0) + 1
print(f"  {d.name}: {'coherent' if v['coherent'] else ', '.join(f'{k} x{n}' for k, n in kinds.items())}")
EOF
done
echo; echo "map: runs/book_map.db (table book_runs, one row per run)"
