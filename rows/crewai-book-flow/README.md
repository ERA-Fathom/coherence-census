# CrewAI, the "write a book with flows" example

The read on CrewAI's own published book-writing flow (crewAIInc/crewAI-examples, `flows/write_a_book_with_flows`, commit da94a91e; the repository has since been archived), nine runs across three models, each run captured two ways from the same process.

The flow runs an outline crew (a researcher with a search tool, then an outliner) and one chapter crew per outlined chapter (a researcher with the same search tool, then a writer), then joins the chapters into a book. The book's committed state is the outline the crew produced and the searches it ran; the read asks whether the chapters stayed consistent with that.

## Files

- `book_flow_ops.py` is the whole mapping from the crew's event stream to committed-state ops. A search is an `add` to one crew-wide query collection. The outline commits a `chapter_title` and a `chapter_no` per chapter and then a `commit` on the outline. Each chapter is a `set` under the title the writer returned, citing that title and every "Chapter k" the text mentions. The flow's join step is an `answer` for the finished book citing every outlined chapter.
- `runner.py` runs the example once, with `fathom_read.capture.crewai.FathomListener` on the crew event bus and Arize's `openinference-instrumentation-crewai` exporting spans to a file, and writes one row per run to `runs/book_map.db` (kept with the runs on the machine that ran them).
- `reread.py` re-maps and re-reads banked runs after a change to the mapping, at no spend.
- `selftest_offline.py` runs the whole path with a scripted model server and a scripted search API and asserts the planted findings. `serper_map.json` is the one-line map that lets the public openinference adapter read the search tool's spans.
- `runs/<timestamp>_<title>/` holds each run's event stream, the flow's own state (outline and chapters), the ops, the verdict, the TOOL spans the instrumentor recorded, and the verdict over those spans alone.

```
fathom read runs/20260915T205744Z_the-state-of-ai-agents-in-enterprise-sof/ops.json
python book_flow_ops.py runs/<run>/crewai_events.json runs/<run>/flow_state.json > ops.json   # regenerate the ops
```

## Runs

Three titles per model. Models through OpenRouter, with the example's hardcoded `gpt-4o` swapped by class attribute at run time. Serper live. crewai 1.15.21.

| Model | Chapters | Searches (distinct) | Event-bus read | Span read |
|---|---|---|---|---|
| gpt-4o-mini | 4, 7, 8 (outliner's choice) | 18 (18), 27 (27), 28 (28) | coherent, coherent, coherent | coherent x3 |
| llama-3.3-70b-instruct | 14, 14, 14 | 73 (45), 25 (25), 36 (15) | `duplicate_commit` x28; `stale_reference` x2; `duplicate_commit` x21 | x28; coherent; x21 |
| mistral-small-3.2-24b | 14, 14, 14 | 16 (16), 17 (15), 15 (15) | coherent; `duplicate_commit` x2; coherent | coherent; x2; coherent |

What the fired runs show. In the llama enterprise-agents book, one chapter crew's researcher issued the identical search 25 times in about two minutes, and three chapter searches re-ran queries the outline crew had already committed. In the llama agent-memory book, one crew looped 22 times while every other crew searched once. In the llama durable-execution book, one chapter crew failed and the flow saved a 14-chapter book as finished with an eight-word placeholder titled "Agent Completed Execution" where the outlined AWS Lambda chapter belonged; the read names the chapter delivered under a title the outline never committed and the outlined chapter the finished book never received. In the mistral durable-execution book, two chapter crews re-ran the outline crew's opening query.

The span read, over the TOOL spans Arize's instrumentor recorded for the same process, matched the event-bus read on every repeat count and stayed silent on the missing chapter, which lives in task outputs rather than tool spans.

## Notes

The example calls `crew().kickoff()` synchronously inside the async `write_chapters` method, which crewai 1.x rejects (`RuntimeError: Agent execution was invoked synchronously from within a running event loop`); it last ran on 0.203.2. The runs here use the vendored example with that one line changed to `await crew().kickoff_async(...)`, so the chapter crews run concurrently as the example's `asyncio.gather` intended. Every other vendored file was byte-identical to upstream.

Arize's CrewAI instrumentor stores the tool's argument schema in `tool.parameters` and the call's arguments in `input.value`. fathom-read 0.3.0's openinference adapter reads only `tool.parameters`, so `runner.py` copies `input.value` over for TOOL spans before the span read; the adapter fallback ships in the next fathom-read release, after which `fathom read runs/<run>/phoenix_spans_tool.json --format openinference --map serper_map.json` reproduces the span read directly.

To run it yourself, vendor the example beside `runner.py` as `write_a_book_with_flows/` (the archived repository still serves the files), apply the one-line change above, store `OPENROUTER_API_KEY` and `SERPER_API_KEY` (macOS Keychain is what `run_mac.sh` reads), and run `bash run_mac.sh`. `FATHOM_MODEL` picks the model and `FATHOM_CHAPTERS` lengthens the outline.
