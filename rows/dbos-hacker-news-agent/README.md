# DBOS Hacker News research agent

The read run on DBOS's published Hacker News research agent (dbos-inc/dbos-demo-apps, `python/hacker-news-agent`, vendored unchanged at commit 826c40a). Study: [Reading a durable workflow's committed state from its own step stream](https://embeddedriskanalytics.com/research-reading-a-durable-workflows-committed-state-from-its-own-step-stream.html).

- `hn_agent_ops.py` is the whole mapping from the agent's step stream (step name, args, result, ok) to committed-state ops: the searches the agent chose, the discussions it holds, the threads it read, the findings it wrote, and the report's citations.
- `runner.py` runs the example once through DBOS with a SQLite system database, records every step's arguments and result, exports DBOS's own step table beside it, and sends the ops to the read. `selftest_offline.py` runs the same path with a scripted model and a scripted Hacker News API and asserts the read returns the planted findings.
- `runs/` holds the five runs from the study: the step stream, DBOS's step table, the report, the ops, and the verdict.

```
fathom read runs/20260902T143638Z_vector-databases/ops.json
python hn_agent_ops.py runs/20260902T143638Z_vector-databases/dbos_steps.json > ops.json   # regenerate the ops
```

To run it yourself, vendor the example beside `runner.py` as `hacker-news-agent/`, set `OPENAI_API_KEY`, and run `python runner.py "<topic>" 3`.
