#!/usr/bin/env python3
"""Print a stream transcript line by line, deduplicating across chunks."""

import json
import sys

def print_transcript(path):
    seen = set()
    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            chunk = json.loads(raw)
            for line in chunk["text"].split("\n"):
                if line and line not in seen:
                    seen.add(line)
                    print(line)

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "../data/transcripts/stream_chat_2026-04-16.jsonl"
    print_transcript(path)
