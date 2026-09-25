# Prime Agent, an orchestrator with rlm child agents

The read on [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) (commit 8969d24) running as a multi-round
orchestrator. Each round the orchestrator spawns child agents through `rlm`, each child sends back a value in an agent
message, and the orchestrator commits results that depend on those values before it opens the next round. Eleven runs
under deepseek/deepseek-chat-v3-0324, with no failure induced.

The trace is the orchestrator's own session, read by [fathom-prime-agent](https://github.com/ERA-Fathom/fathom-prime-agent).
A message a child delivers is committed as a report. An `[agent-message from <child>]` header that the orchestrator
writes in its own text is a claim on that report. When no delivery from that child carries the same message anywhere in
the session, the read names the claim as a reference to a report the committed state never held.

## What the read caught

The orchestrator wrote messages in the exact form Prime Agent gives a delivered child message, for children that had
not replied yet, and committed from them. In the run the bundled trace comes from, the orchestrator received two
children's messages, then wrote six more itself in one turn, each stating a value for a child still working, and
committed all eight results in the same cell. The real messages arrived afterwards with different values: the message it
wrote for one child said `e2 delta = 1`, and that child's real message said `e2 delta = 4`.

| Children per round | Values each result depends on | Runs | Seeds | Rounds reached (of 10) | Commits resting on reports not yet delivered |
|---|---|---|---|---|---|
| 4 | 3 | 3 | 5660 | 10, all three | 2 of 120 (1.7%) |
| 8 | 6 | 8 | 5660, 4041 | 2 to 6 | 37 of 209 (17.7%); one run carries 27 of the 37 |
| both | | 11 | 5660, 4041 | | 39 of 329 (11.9%), concentrated: one run at 8 children carries 27 of the 39 |

A commit rests on a report not yet delivered when the orchestrator committed a child's result before that child's message
reached it by any route: the message itself, the child's final answer in a listing, or a collect. In these runs the real
messages mostly arrived later, carrying other values.

## The bundled trace

`traces/prime_agent_fanin.json` is eight ops from the first round of that run, taken unchanged from the reader's output
with only the step numbers renewed: the two delivered messages, the orchestrator's claimed message for the child whose
result is `e2`, the commits of `e9`, `e1` and `e2` from the same cell, and two messages delivered afterwards, the second
of them the real `e2` message.

```
fathom read traces/prime_agent_fanin.json
```

returns `stale_reference` x1, at step 2, on the claimed report `r0t4: e2 delta = 1`.

## Caveats

- Two seeds and 11 runs. The rate is a coordinate, not an estimate for Prime Agent in general.
- The runs at 8 children per round all ended before round 10, so they cover fewer rounds than the runs at 4.
- The count is concentrated: one run holds 27 of the 39 commits, and 5 of the 11 runs have none.
- The task's wording changed between batches of runs, and the rate varied from run to run under the same wording.
- One model and one Prime Agent commit. Prime Agent's main at cd1f215 keeps the same session format and extension API,
  and fathom-prime-agent reads it the same way.

## Read your own sessions

```
prime-agent package install git:github.com/ERA-Fathom/fathom-prime-agent   # the read at every turn end
pip install git+https://github.com/ERA-Fathom/fathom-prime-agent
prime-fathom read ~/.prime/agent/sessions/<session>.jsonl                  # a saved session
```
