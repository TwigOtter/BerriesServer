# Observability

How to see what Berries is actually doing — which pipeline ran, what was
retrieved, what the system prompt said, which tools were called, and how long
every step took.

## Three layers

### 1. Service logs (journald)

All three services configure logging through `shared/logging_setup.py`:

```bash
journalctl -u berries-ingest -u berries-discord -u berries-embed -f
```

What changed from the old setup:

- **Every line carries its logger name** (`shared.retrieval`,
  `discord_bot.mention`, `llm_client`, `berries.trace`, ...), so a line always
  says what is being invoked.
- **`shared/*` logs actually reach journald from the Discord bot.** The old
  setup only attached handlers to the `discord_bot` logger, so INFO logs from
  `shared/ask_berries.py`, `shared/retrieval.py`, etc. were silently dropped.
- **Third-party noise is capped at WARNING** — no more `httpx`/
  `huggingface_hub` "HTTP Request: HEAD https://huggingface.co/..." spam.
- Each service also writes `logs/<service>.log` at DEBUG (rotating, 5 MB × 3)
  for when INFO wasn't enough.

Per interaction you'll now see lines like:

```
[INFO] llm_client: LLM call — purpose=rewrite_queries model=claude-haiku-4-5 0.74s in=412 out=38
[INFO] shared.retrieval: retrieval — 3 query/queries → 12 candidate(s) → 4 chunk(s) injected
[INFO] llm_client: LLM call — purpose=rerank model=claude-haiku-4-5 1.02s in=2103 out=44
[INFO] llm_client: LLM call — purpose=chat_response model=claude-sonnet-4-6 4.31s in=5820 out=71
[INFO] berries.trace: trace discord_mention ok 7.42s [3f9c2a1b4d5e] nickname_lookup=0.01s
       context_chroma=2.31s context_user_profile=0.02s context_channel_history=0.00s
       llm_response=4.31s log_interaction=0.01s | llm: 3 call(s) 6.07s
```

The final `berries.trace` line is the per-interaction performance summary:
total time, per-step breakdown, and LLM call count/time.

### 2. Interaction traces (`logs/traces/`)

Every response pipeline (`shared/ask_berries.py`) opens a trace
(`shared/trace.py`); every stage on the request path records into it. One JSON
line per interaction is appended to `logs/traces/YYYY-MM-DD.jsonl` (local
calendar day, like the other daily logs), containing:

- `pipeline` (`twitch_mention`, `discord_mention`, `going_live`,
  `discord_oneoff`), user, raw query
- `steps` — every stage with duration in ms; dotted names encode nesting
  (`context_chroma.rerank` ran inside the chroma provider)
- `llm_calls` — purpose, model, backend, duration, input/output token usage
- `tool_calls` — agent-loop tool invocations with inputs and output previews
- `data` — the **full system prompt**, the exact user message sent to the
  model, the response, and retrieval detail (rewritten queries, candidate
  count, injected chunks with sources)

Tracing is on by default; set `TRACE_ENABLED=false` in `.env` to turn it off.
The trace contextvar follows the request through `await` and
`asyncio.to_thread`, and every helper is a no-op outside a trace, so scripts
and tests are unaffected.

### 3. The trace inspector (`scripts/traces.py`)

```bash
source /opt/berries/venv/bin/activate

python scripts/traces.py                     # today's traces, one line each
python scripts/traces.py --date 2026-07-14   # a specific day
python scripts/traces.py --last 5            # only the most recent N
python scripts/traces.py 3f9c2a1b            # full detail for one trace (id prefix)
python scripts/traces.py 3f9c2a1b --prompts  # also print the full system prompt
python scripts/traces.py --follow            # live-tail new traces as they land
```

The detail view shows the step timing tree, every LLM call with token usage,
tool calls, the rewritten search queries and injected chunks, the user
message, and the response — the complete answer to "why did Berries say
that, and why did it take 7 seconds?".

## Adding instrumentation to new code

```python
from shared import trace

# Inside an existing pipeline — time a stage and attach detail:
with trace.step("my_stage") as s:
    result = await do_work()
    s["items"] = len(result)

# Attach data to the whole interaction:
trace.add(my_field=some_value)

# A brand-new response pipeline gets its own trace:
with trace.trace("my_pipeline", username=user):
    ...
```

LLM calls made through `shared/llm_client.get_completion()` are logged and
traced automatically — pass `purpose="my_task"` so the label is meaningful.

## A frontend?

The JSONL trace files are the stable interface: if a localhost dashboard ever
feels worth building, it only needs to read `logs/traces/*.jsonl` — no
service changes required. Until then, `scripts/traces.py --follow` during a
stream plus the detail view afterwards covers the same ground with far less
to maintain.
