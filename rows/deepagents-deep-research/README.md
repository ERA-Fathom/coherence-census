# deepagents, the "deep_research" example

The read on deepagents' own published research agent (langchain-ai/deepagents, `examples/deep_research`, commit e031960, vendored unchanged; see `deep_research/VENDORED_FROM.txt`), six runs across two models, each run captured two ways from the same process.

The example plans with `write_todos`, saves the request to a file, delegates topics to research sub-agents through `task()`, each sub-agent searches (Tavily, with the top page fetched into its context) and reflects, and the orchestrator writes `/final_report.md` with a numbered Sources list. The run's committed state holds the delegations, every search any agent ran, every page a search returned, and the files. The read asks whether the sub-agents and the report stayed consistent with that.

## Files

- `deep_research_ops.py` holds the whole mapping from the tool trace to committed-state ops. A `task()` call adds its description to one run-wide delegation collection. A `tavily_search` adds its query to one run-wide query collection and sets a `source` per page it returned. `write_file` sets the file under its path, and the report cites a `source` for every URL it lists. When a run ends without `/final_report.md`, the orchestrator's final message stands in as the report, with the same citation rule.
- `runner.py` runs the example once with a LangChain callback handler that records every tool call in the graph, the orchestrator's and each sub-agent's, attributing each search to the `task()` it ran under, and appends the published `langchain-fathom` middleware to the same agent. It writes one row per run to `runs/research_map.db` (kept with the runs on the machine that ran them).
- `deepresearch_map.json` names `task` and `tavily_search` for the middleware, which sees the orchestrator's own tool calls only.
- `reread.py` re-maps and re-reads banked runs after a change to the mapping, at no spend. `selftest_offline.py` runs the whole path with a scripted model server and a scripted Tavily and asserts the planted findings.
- `runs/<timestamp>_<question>/` holds each run's tool trace, the orchestrator's message history, the files it wrote, the middleware's verdict, the ops, the verdict, and the run's settings.

```
fathom read runs/20260916T183702Z_compare-letta-mem0-zep-langmem-and-cogne/ops.json
python deep_research_ops.py runs/<run>/tool_trace.json runs/<run>/messages.json > ops.json   # regenerate the ops
```

## Runs

Three comparison questions per model, models through OpenRouter with the example's hardcoded `init_chat_model` call routed to the named model at run time. Tavily live. deepagents 0.7.14, langchain-fathom 0.1.0.

| Model | Question | Tool calls | Sub-agents | Searches (distinct) | Sources held | Report | Citations | Read |
|---|---|---|---|---|---|---|---|---|
| gpt-4o-mini | Temporal, DBOS, Inngest on durable execution | 20 | 3 | 8 (8) | 6 | final message | 5 | `duplicate_commit` x1 |
| gpt-4o-mini | LangGraph, CrewAI, AutoGen on shared state | 18 | 3 | 8 (8) | 8 | final message | 8 | coherent |
| gpt-4o-mini | Letta, Mem0, Zep on long-term memory | 10 | 1 | 5 (5) | 4 | `/final_report.md` | 3 | coherent |
| llama-3.3-70b | Temporal, DBOS, Inngest, Restate, Hatchet | 18 | 2 | 8 (5) | 3 | final message | 1 | `duplicate_commit` x9 |
| llama-3.3-70b | LangGraph, CrewAI, AutoGen, OpenAI Agents SDK, Google ADK | 3 | 1 | 1 (1) | 1 | none delivered | 0 | coherent |
| llama-3.3-70b | Letta, Mem0, Zep, LangMem, Cognee | 6 | 1 | 1 (1) | 1 | `/final_report.md` | 1 | `stale_reference` x1 |

What the fired runs show. In the llama memory run, the one search returned `theaiengineer.substack.com/p/cognee-vs-zep-vs-mem0-vs-letta`, and the report's only citation reads `cognee-vs-mem0-vs-zep-vs-letta`, the slug rewritten to match the article title's word order. The report cites a URL no search returned, one transposition from the real one, and the read names it as a reference to a source the committed state never held. In the llama durable-execution run, the orchestrator delegated the whole question twice with the identical description, the second sub-agent opened with the first sub-agent's exact query, the one article every search surfaced (zylos.ai) failed to fetch with a redirect error on all five holds, and the orchestrator then ran its own query three times with word-for-word identical reflections while the second and third of those searches returned a fetchable deep dive on all five systems that neither the reflections nor the report acknowledged. The final message reports that nothing relevant was found and cites the unreachable article. In the gpt-4o-mini durable-execution run, one sub-agent's two searches returned the same YouTube page, held twice.

The published middleware, over the orchestrator's own tool calls alone, returned 3 of the 9 findings on the llama durable-execution run (the duplicate delegation and the orchestrator's two repeated queries); the callback trace across the sub-agents returned the rest. The gpt-4o-mini runs predate the runner change that keeps the middleware's verdict, so their `middleware.json` reads null.

## Notes

Under both models the example's own workflow ran short of its prompt. No run called `write_todos`. Four of six runs ended without `/final_report.md`, delivering the report with its Sources list in the final message instead, and one llama run ended with a final message describing the delegation ("This task is delegated to a research-agent subagent, which will perform the research and return a report") and no report at all. The verification step in the orchestrator's prompt never ran.

`langchain-fathom` 0.1.0's `on_finding="store"` writes the verdict to a state key the deepagents graph schema does not declare, so LangGraph drops it before the result reaches the caller. `runner.py` keeps a copy from inside the middleware. A declared state key is the package fix.

Three earlier llama runs (17:27 to 17:28 UTC) returned an empty completion on the first model call, no text and no tool call, through an OpenRouter provider that appears to have dropped the tool call; they carry an error in the map and no verdict, and the rerun above sets `provider.require_parameters` on every request. A read over zero ops says coherent, so the runner now refuses to post a run that committed nothing.

To run it yourself, vendor the example beside `runner.py` as `deep_research/` (the files at the commit above), store `OPENROUTER_API_KEY` and `TAVILY_API_KEY` (macOS Keychain is what `run_mac.sh` reads), and run `bash run_mac.sh "your question"`. `FATHOM_MODEL` picks the model.
