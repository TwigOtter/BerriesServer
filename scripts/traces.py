"""
scripts/traces.py

Inspect Berries interaction traces (logs/traces/YYYY-MM-DD.jsonl, written by
shared/trace.py). The terminal answer to "what did Berries just do?".

Usage:
    python scripts/traces.py                     # today's traces, one line each
    python scripts/traces.py --date 2026-07-14   # a specific day
    python scripts/traces.py --last 5            # only the most recent N
    python scripts/traces.py 3f9c2a1b            # full detail for one trace (id prefix)
    python scripts/traces.py 3f9c2a1b --prompts  # also print the full system prompt
    python scripts/traces.py --follow            # live-tail new traces as they land
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import LOCAL_TZ, TRACES_DIR  # noqa: E402


def _load_day(date_str: str) -> list[dict]:
    path = TRACES_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _local_time(record: dict) -> str:
    try:
        dt = datetime.fromisoformat(record["started_at"])
        return dt.astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    except (KeyError, ValueError):
        return "??:??:??"


def _preview(text, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary_line(r: dict) -> str:
    status = "ok " if r.get("ok") else "ERR"
    user = r.get("meta", {}).get("username", "")
    query = r.get("meta", {}).get("query") or r.get("data", {}).get("user_message", "")
    llm = r.get("llm_calls", [])
    return (
        f"{_local_time(r)}  {r['trace_id']}  {status}  {r.get('duration_ms', 0) / 1000:6.2f}s  "
        f"{r.get('pipeline', '?'):16s}  llm={len(llm)}  {user:<16.16s}  {_preview(query)}"
    )


def _print_list(records: list[dict], last: int | None) -> None:
    if last:
        records = records[-last:]
    if not records:
        print("No traces found.")
        return
    for r in records:
        print(_summary_line(r))
    print(f"\n{len(records)} trace(s). Detail: python scripts/traces.py <id-prefix>")


def _print_detail(r: dict, show_prompts: bool) -> None:
    status = "ok" if r.get("ok") else f"ERROR: {r.get('error')}"
    print(f"trace {r['trace_id']} — {r.get('pipeline')} — {status}")
    print(f"started:  {r.get('started_at')} ({_local_time(r)} local)")
    print(f"duration: {r.get('duration_ms', 0) / 1000:.2f}s")
    if r.get("meta"):
        print("meta:")
        for k, v in r["meta"].items():
            print(f"  {k}: {_preview(str(v), 200)}")

    if r.get("steps"):
        print("\nsteps (completion order; dots = nesting):")
        for s in r["steps"]:
            name = s["name"]
            indent = "  " * name.count(".")
            extras = {k: v for k, v in s.items() if k not in ("name", "ms")}
            extra_str = f"  {extras}" if extras else ""
            print(f"  {indent}{name.rsplit('.', 1)[-1]:<28s} {s['ms'] / 1000:7.2f}s{extra_str}")

    if r.get("llm_calls"):
        print("\nllm calls:")
        for c in r["llm_calls"]:
            err = f"  ERROR: {c['error']}" if c.get("error") else ""
            print(
                f"  {c.get('purpose', '?'):<24s} {c.get('model', '?'):<28s} "
                f"{c.get('ms', 0) / 1000:6.2f}s  in={c.get('input_tokens')} out={c.get('output_tokens')}{err}"
            )

    if r.get("tool_calls"):
        print("\ntool calls:")
        for c in r["tool_calls"]:
            ok = "ok" if c.get("ok") else "FAILED"
            print(f"  {c.get('name', '?'):<20s} {c.get('ms', 0) / 1000:6.2f}s  {ok}  input={c.get('input')}")
            if c.get("output_preview"):
                print(f"      → {_preview(c['output_preview'], 200)}")

    data = r.get("data", {})
    retrieval = data.get("retrieval")
    if retrieval:
        print("\nretrieval:")
        for q in retrieval.get("queries", []):
            print(f"  query: {q}")
        print(f"  candidates: {retrieval.get('n_candidates')}, injected: {len(retrieval.get('injected', []))}")
        for chunk in retrieval.get("injected", []):
            print(f"  [{chunk.get('source')}] {_preview(chunk.get('text', ''), 160)}")

    if data.get("user_message"):
        print(f"\nuser message:\n  {data['user_message']}")
    if data.get("response") is not None:
        print(f"\nresponse:\n  {data['response']}")
    if data.get("gif_query"):
        print(f"\ngif query: {data['gif_query']}")

    if show_prompts and data.get("system_prompt"):
        print("\nsystem prompt:")
        print(data["system_prompt"])
    elif data.get("system_prompt"):
        print(f"\nsystem prompt: {len(data['system_prompt'])} chars (use --prompts to print it)")


def _find_by_prefix(prefix: str) -> dict | None:
    """Search trace files newest-first for a trace id starting with prefix."""
    for path in sorted(TRACES_DIR.glob("*.jsonl"), reverse=True):
        for r in reversed(_load_day(path.stem)):
            if r.get("trace_id", "").startswith(prefix):
                return r
    return None


def _follow(date_str: str) -> None:
    print(f"Following {TRACES_DIR / (date_str + '.jsonl')} (Ctrl-C to stop)...")
    seen = len(_load_day(date_str))
    try:
        while True:
            records = _load_day(date_str)
            for r in records[seen:]:
                print(_summary_line(r))
            seen = len(records)
            # Roll over at local midnight
            today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
            if today != date_str:
                date_str, seen = today, 0
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Berries interaction traces.")
    parser.add_argument("trace_id", nargs="?", help="trace id (or prefix) to show in full detail")
    parser.add_argument("--date", help="day to list (YYYY-MM-DD, local time; default today)")
    parser.add_argument("--last", type=int, help="only the most recent N traces")
    parser.add_argument("--prompts", action="store_true", help="print the full system prompt in detail view")
    parser.add_argument("--follow", action="store_true", help="live-tail new traces")
    args = parser.parse_args()

    date_str = args.date or datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    if args.trace_id:
        record = _find_by_prefix(args.trace_id)
        if record is None:
            print(f"No trace found with id prefix {args.trace_id!r}.")
            sys.exit(1)
        _print_detail(record, show_prompts=args.prompts)
    elif args.follow:
        _follow(date_str)
    else:
        _print_list(_load_day(date_str), args.last)


if __name__ == "__main__":
    main()
