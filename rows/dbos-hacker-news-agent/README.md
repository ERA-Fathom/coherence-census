# DBOS Hacker News research agent

The row in progress. `hn_agent_ops.py` is the mapping from the agent's DBOS step stream (step name, args, result, ok) to committed-state ops: the searches the agent chose, the discussions it holds, the findings it wrote, and the report's citations. When the run lands, this folder gets the step stream, the report, and the read's verdict, and the row goes into the table.

```
python rows/dbos-hacker-news-agent/hn_agent_ops.py dbos_steps.json > ops.json
fathom read ops.json
```
