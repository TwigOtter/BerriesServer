"""
shared/trace.py

Per-interaction tracing — the answer to "what did Berries actually do, and
how long did each part take?".

Every response pipeline (Twitch mention, Discord mention, going-live, ...)
opens a trace, and each stage on the request path — nickname lookup, query
rewriting, vector search, reranking, prompt assembly, LLM calls, agent tool
calls — records its duration and key details into it. Each trace produces
two outputs:

  1. One INFO summary line (journald-friendly), e.g.:
       trace discord_mention ok 7.42s [3f9c2a1b4d5e] context_chroma=2.31s
       context_user_profile=0.01s llm_response=4.90s | llm: 3 call(s) 6.80s
  2. One JSON line in logs/traces/YYYY-MM-DD.jsonl with full detail — the
     exact system prompt, user message, response, rewritten search queries,
     injected chunks, per-step timings, LLM token usage, and tool calls.
     Inspect with: python scripts/traces.py

The active trace lives in a contextvar, so code anywhere on the request path
can record into it without parameter plumbing, and it follows the request
through awaits and asyncio.to_thread. Every helper is a no-op when no trace
is active (or TRACE_ENABLED=false), so scripts, tests, and the eval harness
are unaffected.

Step names encode nesting with dots: a step opened inside another step is
recorded as "outer.inner". The summary line shows only top-level steps; the
JSONL record keeps everything.
"""

import contextvars
import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from shared.config import LOCAL_TZ, TRACE_ENABLED, TRACES_DIR

log = logging.getLogger("berries.trace")

# Prompts and responses are stored in full; this cap only guards against a
# runaway field (e.g. a malformed context block) bloating the trace file.
_MAX_FIELD_CHARS = 50_000

_current: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "berries_trace", default=None
)


def _clip(value):
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + f"... [{len(value) - _MAX_FIELD_CHARS} chars clipped]"
    return value


class Trace:
    """One traced interaction. Created via the trace() context manager."""

    def __init__(self, pipeline: str, **meta):
        self.trace_id = uuid.uuid4().hex[:12]
        self.pipeline = pipeline
        self.meta = {k: _clip(v) for k, v in meta.items()}
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.perf_counter()
        self.steps: list[dict] = []       # completion order; dotted names encode nesting
        self.llm_calls: list[dict] = []
        self.tool_calls: list[dict] = []
        self.data: dict = {}              # prompts, responses, retrieval details, ...
        self.error: str | None = None
        self._stack: list[str] = []

    def add(self, **kv) -> None:
        for k, v in kv.items():
            self.data[k] = _clip(v)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "pipeline": self.pipeline,
            "started_at": self.started_at.isoformat(),
            "duration_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            "ok": self.error is None,
            "error": self.error,
            "meta": self.meta,
            "steps": self.steps,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "data": self.data,
        }


def current() -> Trace | None:
    return _current.get()


@contextmanager
def trace(pipeline: str, **meta):
    """
    Open a trace for one interaction. On exit (success or exception) the
    trace is written to logs/traces/ and summarized at INFO. Exceptions are
    recorded and re-raised. Yields None when tracing is disabled.
    """
    if not TRACE_ENABLED:
        yield None
        return

    t = Trace(pipeline, **meta)
    token = _current.set(t)
    try:
        yield t
    except Exception as e:
        t.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        _current.reset(token)
        _finish(t)


@contextmanager
def step(name: str, **meta):
    """
    Time one stage of the active trace. Yields a dict the caller may add
    detail to (e.g. s["queries"] = [...]); it is merged into the step record.
    No-op (but still yields a dict) when no trace is active.
    """
    info: dict = dict(meta)
    t = current()
    if t is None:
        yield info
        return

    t._stack.append(name)
    full_name = ".".join(t._stack)
    t0 = time.perf_counter()
    try:
        yield info
    finally:
        t._stack.pop()
        entry = {"name": full_name, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for k, v in info.items():
            entry[k] = _clip(v)
        t.steps.append(entry)


def add(**kv) -> None:
    """Attach data (prompts, responses, retrieval details) to the active trace."""
    t = current()
    if t is not None:
        t.add(**kv)


def record_llm_call(
    purpose: str,
    model: str,
    backend: str,
    ms: float,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    max_tokens: int | None = None,
    error: str | None = None,
) -> None:
    t = current()
    if t is not None:
        t.llm_calls.append({
            "purpose": purpose,
            "model": model,
            "backend": backend,
            "ms": round(ms, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "max_tokens": max_tokens,
            "error": error,
        })


def record_tool_call(
    name: str,
    ms: float,
    *,
    input: dict | None = None,
    output_preview: str | None = None,
    ok: bool = True,
) -> None:
    t = current()
    if t is not None:
        t.tool_calls.append({
            "name": name,
            "ms": round(ms, 1),
            "input": input,
            "output_preview": _clip(output_preview),
            "ok": ok,
        })


def _finish(t: Trace) -> None:
    payload = t.to_dict()

    try:
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        # Keyed by local calendar day, matching the other daily logs.
        path = TRACES_DIR / f"{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        log.exception("Failed to write trace %s", t.trace_id)

    top_level = [s for s in t.steps if "." not in s["name"]]
    steps_str = " ".join(f"{s['name']}={s['ms'] / 1000:.2f}s" for s in top_level)
    llm_ms = sum(c["ms"] for c in t.llm_calls)
    status = "ok" if t.error is None else f"ERROR({t.error})"
    log.info(
        "trace %s %s %.2fs [%s] %s | llm: %d call(s) %.2fs%s",
        t.pipeline,
        status,
        payload["duration_ms"] / 1000,
        t.trace_id,
        steps_str,
        len(t.llm_calls),
        llm_ms / 1000,
        f" | tools: {len(t.tool_calls)} call(s)" if t.tool_calls else "",
    )
