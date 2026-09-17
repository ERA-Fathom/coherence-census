# DBOS, the Hacker News research agent, with the repair in front of one step

The read on DBOS's own published Hacker News research agent (dbos-inc/dbos-demo-apps, `python/hacker-news-agent`, commit 826c40a, vendored unchanged; see `VENDORED_FROM.txt`), five topics at ten iterations, run twice. The first pass runs the agent as published. The second pass puts the repair in front of the follow-up step, where the agent proposes its next queries, and changes nothing else. Same model, same topics, same iteration count, same day.

The agent searches Hacker News for a topic, reads the comments on the top stories, evaluates what it found, decides whether to continue, proposes follow-up queries, picks one, and repeats, then writes a report with links. DBOS checkpoints every step. The run's committed state holds every query the agent chose, every story a search returned, every thread it read, and every finding it wrote. The read asks whether later steps stayed consistent with that.

## Files

- `hn_agent_ops.py` holds the whole mapping from the DBOS step stream to committed-state ops. A search adds its query to one run-wide query collection and sets a `story` per hit. Reading a thread adds its id to one run-wide collection of threads read. An evaluation sets a `finding` under its query and cites the stories it ranked. The report cites a `story` for every Hacker News link it carries.
- `runner.py` runs the example once, as published, under DBOS with a SQLite system database, records every DBOS step with its arguments and result, exports DBOS's own step table beside it, maps the stream to ops, and sends the ops to the read. With `FATHOM_GATE=1` it puts the repair in front of the follow-up step through the Fathom service (`fathom_read.client.reground`, fathom-read 0.5.0 or later), applies the decision that comes back, and writes every decision to `gate.json` at outcome level.
- `selftest_offline.py` runs the pipeline with a scripted model and a scripted Hacker News API, no network and no spend, and asserts the step stream and the ops.
- `runs/<timestamp>_<topic>/` holds each run's ops, the verdict, the report, DBOS's step table, and the step stream gzipped (`dbos_steps.json.gz`, the input to the mapping). Gated runs carry `gate.json` as well.

```
fathom read runs/20260902T155919Z_kubernetes-cost/ops.json
gunzip -k runs/<run>/dbos_steps.json.gz && python hn_agent_ops.py runs/<run>/dbos_steps.json > ops.json   # regenerate the ops
```

## Runs

gpt-4o-mini through OpenRouter, 2 September 2026, five topics, ten iterations each. Elapsed 145 to 182 seconds per run as published and 135 to 178 seconds with the repair.

| Topic | Pass | Searches (repeated) | First repeat at iteration | Threads read (re-read) | Distinct threads | Stories re-held | Read |
|---|---|---|---|---|---|---|---|
| postgres performance | as published | 10 (3) | 7 | 138 (52) | 86 | 90 | `duplicate_commit` x145, `superseded_value` x1 |
| rust async | as published | 10 (1) | 9 | 130 (32) | 98 | 52 | `duplicate_commit` x85, `superseded_value` x1 |
| kubernetes cost | as published | 10 (5) | 4 | 152 (108) | 44 | 173 | `duplicate_commit` x286, `superseded_value` x2 |
| vector databases | as published | 10 (3) | 6 | 194 (88) | 106 | 129 | `duplicate_commit` x220, `superseded_value` x2 |
| webassembly | as published | 10 (2) | 7 | 133 (37) | 96 | 72 | `duplicate_commit` x111, `superseded_value` x2 |
| postgres performance | repair on the follow-up step | 10 (0) | none | 97 (8) | 89 | 15 | `duplicate_commit` x23 |
| rust async | repair on the follow-up step | 10 (0) | none | 136 (15) | 121 | 17 | `duplicate_commit` x32 |
| kubernetes cost | repair on the follow-up step | 10 (0) | none | 97 (12) | 85 | 27 | `duplicate_commit` x39 |
| vector databases | repair on the follow-up step | 10 (0) | none | 170 (33) | 137 | 43 | `duplicate_commit` x76 |
| webassembly | repair on the follow-up step | 10 (0) | none | 156 (9) | 147 | 16 | `duplicate_commit` x25 |

As published, the agent repeated 14 of its 50 searches, every repeat at iteration 4 or later, re-read 317 of the 747 threads it fetched (42 percent), held the same story again with the same title 516 times, and went back to a next query it had already chosen and moved past 8 times. Across the five runs the read returned 855 findings. With the repair in front of the follow-up step, the agent repeated 0 of 50 searches, re-read 77 of 656 threads (12 percent), re-held 118 stories, covered 579 distinct threads against 430 (35 percent more) at the same iteration count and model, and the read returned 195 findings. The remaining duplicates are threads that two distinct searches both returned and the agent read twice, which the follow-up step never touches.

What the repair saw. Across the 45 follow-up steps in the five gated runs, the agent's first proposal was already in its committed query list on 21, none at iterations 1 and 2 and all five at iteration 6. On 23 steps every proposal was new and the agent proceeded. On 21 the repair kept the proposals that did not contradict the committed list and the agent took the first of those. On 1 (postgres performance, iteration 8) every proposal contradicted the list, the committed queries went back in front of the agent, and it proposed a new one on the first re-ask. One re-ask across the five runs.

## Notes

The follow-up step is the only place the repair touches, and it removes the query repeats entirely on these runs. The thread re-reads fall by roughly three quarters as a consequence, since a repeated search re-fetches the same threads, and the remainder is the agent reading a thread that two distinct searches returned.

The `superseded_value` findings on `current_topic` show the agent choosing, as its next query, one it had already chosen and moved on from earlier in the run (in the postgres run, step 356 returns to 'postgres performance tools', replaced at step 312). The gated runs show none, since a query already in the committed list never comes back as a proposal the agent can pick.

The verdicts in `runs/*/read.json` come from fathom-read 0.4.1, which also counts a story re-held with the title it already carries. The 2 September study read these runs under 0.1 and reported 331 and 77 duplicate commits, which counted repeated searches and re-read threads only; those two columns are unchanged here.

DBOS records step outputs in its system database and this runner adds the arguments, so the read sees what each step acted on. The exported `dbos_operation_outputs.json` is DBOS's own table for provenance. On this agent nearly every step commits something, so the informative unit for the expiry read is the iteration rather than the step, and no DBOS calibration exists yet; an expiry read over these runs reports under the pooled default.

To run it yourself, vendor the example beside `runner.py` as `hacker-news-agent/` (the files at the commit above), export `OPENROUTER_API_KEY` in the terminal, and run `bash run_mac.sh` for three iterations or `FATHOM_ITERATIONS=10 bash run_mac.sh` to match these runs. `FATHOM_GATE=1` adds the repair and needs a free service key.
