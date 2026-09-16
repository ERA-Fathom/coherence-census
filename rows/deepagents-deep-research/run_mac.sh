#!/usr/bin/env bash
# Run deepagents' published deep_research example on a few questions and read each run.
#
#   cd Fathom_DeepAgents_Research_MacRun
#   bash run_mac.sh                                  # the default three questions
#   bash run_mac.sh "your question" "another one"    # your own
#
# Keys. Store each once in the macOS Keychain and this script loads them every time:
#   security add-generic-password -a "$USER" -s OPENROUTER_API_KEY -w     # prompts for the value
#   security add-generic-password -a "$USER" -s TAVILY_API_KEY -w
# An exported OPENROUTER_API_KEY / TAVILY_API_KEY in this terminal wins over the Keychain.
#
# Model. The example hardcodes claude-sonnet-4-5 through init_chat_model; FATHOM_MODEL routes the same
# call through OpenRouter without touching the vendored code (default below: openai/gpt-4o-mini).
# The example's own model: FATHOM_MODEL=anthropic/claude-sonnet-4.5 bash run_mac.sh (also via OpenRouter).
set -euo pipefail
cd "$(dirname "$0")"

keychain() { security find-generic-password -a "$USER" -s "$1" -w 2>/dev/null || true; }
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-$(keychain OPENROUTER_API_KEY)}"
export TAVILY_API_KEY="${TAVILY_API_KEY:-$(keychain TAVILY_API_KEY)}"
if [[ -z "$OPENROUTER_API_KEY" ]]; then
  echo "no OpenRouter key. Store it once:  security add-generic-password -a \"\$USER\" -s OPENROUTER_API_KEY -w"; exit 1
fi
if [[ -z "$TAVILY_API_KEY" ]]; then
  echo "no Tavily key (the example's search tool). Get one at tavily.com, then store it once:"
  echo "  security add-generic-password -a \"\$USER\" -s TAVILY_API_KEY -w"; exit 1
fi
export FATHOM_MODEL="${FATHOM_MODEL:-openai/gpt-4o-mini}"
export FATHOM_RECURSION="${FATHOM_RECURSION:-200}"

# Preflight: one authenticated request per key before any run, so a bad key stops here.
code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/models || echo 000)
[[ "$code" == "200" ]] || { echo "OpenRouter key check failed ($code). Re-store the key and rerun."; exit 1; }
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
       -d "{\"api_key\":\"$TAVILY_API_KEY\",\"query\":\"ping\",\"max_results\":1}" https://api.tavily.com/search || echo 000)
[[ "$code" == "200" ]] || { echo "Tavily key check failed ($code). Re-store the key and rerun."; exit 1; }
echo "keys ok, model=$FATHOM_MODEL"

command -v uv >/dev/null || { echo "install uv first: brew install uv"; exit 1; }
if [[ ! -d .venv ]]; then
  # deepagents pinned to the release the self-test ran on; langchain-fathom is the published middleware.
  uv venv -q --python 3.12 .venv
  uv pip install -q --python .venv/bin/python "deepagents==0.7.14" "langchain-openai>=1.3" "langchain-fathom>=0.1.0" \
      "fathom-read>=0.4.1" "tavily-python>=0.7" "httpx>=0.28" "markdownify>=1.2"
fi

QUESTIONS=("$@")
if [[ ${#QUESTIONS[@]} -eq 0 ]]; then
  QUESTIONS=("Compare how Temporal, DBOS and Inngest approach durable execution for AI agent workflows, with their tradeoffs"
             "Compare LangGraph, CrewAI and AutoGen on how they persist and share state between agents in a long-running task"
             "What are the main approaches to giving AI agents long-term memory, and how do Letta, Mem0 and Zep differ")
fi

for q in "${QUESTIONS[@]}"; do
  echo; echo "=================================================================="
  echo "question: $q   (model=$FATHOM_MODEL)"
  echo "=================================================================="
  if ! .venv/bin/python runner.py "$q"; then
    echo "RUN FAILED for that question (see the error above). Stopping."; exit 1
  fi
done

echo; echo "done. verdicts:"
for d in runs/*/; do
  [[ -f "$d/read.json" || -f "$d/run.json" ]] || continue
  .venv/bin/python - "$d" <<'EOF'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
err = json.load(open(d / "run.json")).get("error") if (d / "run.json").exists() else None
if err:
    print(f"  {d.name}: NO RUN ({err[:90]})"); raise SystemExit
if not (d / "read.json").exists():
    raise SystemExit
v = json.load(open(d / "read.json"))
kinds = {}
for f in v["findings"]: kinds[f["kind"] + "/" + f["key"][:40]] = kinds.get(f["kind"] + "/" + f["key"][:40], 0) + 1
print(f"  {d.name}: {'coherent' if v['coherent'] else ', '.join(f'{k} x{n}' for k, n in kinds.items())}")
EOF
done
echo; echo "map: runs/research_map.db (table research_runs, one row per run)"
