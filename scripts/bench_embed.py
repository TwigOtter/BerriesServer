"""
scripts/bench_embed.py

One-off A/B benchmark: encode the same batches on CPU vs GPU and print
the speedup. Loads the model twice (sequentially, not concurrently) using
the same cache as embed_api so no re-download.

Run:
    source /opt/berries/venv/bin/activate
    python -m scripts.bench_embed

Note: this loads the model into the local process, not via the running
embed_api service. It will briefly allocate VRAM alongside the service —
nomic-embed is ~550MB so both fit comfortably on an 8GB card.
"""

import gc
import time

import torch
from sentence_transformers import SentenceTransformer

from shared.config import DATA_DIR, EMBEDDING_MODEL

# ~480-token chunk of plausible chat-transcript prose. Padded to roughly
# match the CHUNK_TOKEN_LIMIT the ingest pipeline produces.
SAMPLE = (
    "search_document: "
    + ("the spooky forest demon shuffled through the underbrush, muttering "
       "about disc golf and missed putts. twig laughed in chat. someone "
       "redeemed a sound alert. ") * 40
)

BATCH_SIZES = [1, 8, 32]
WARMUP_RUNS = 2
TIMED_RUNS = 5


def bench(device: str) -> dict[int, float]:
    print(f"\n=== loading model on {device} ===")
    model = SentenceTransformer(
        EMBEDDING_MODEL,
        trust_remote_code=True,
        cache_folder=str(DATA_DIR / "huggingface"),
        device=device,
    )

    results = {}
    for batch_size in BATCH_SIZES:
        batch = [SAMPLE] * batch_size

        for _ in range(WARMUP_RUNS):
            model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        if device == "cuda":
            torch.cuda.synchronize()

        t0 = time.perf_counter()
        for _ in range(TIMED_RUNS):
            model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        if device == "cuda":
            torch.cuda.synchronize()
        elapsed_ms = (time.perf_counter() - t0) * 1000 / TIMED_RUNS

        results[batch_size] = elapsed_ms
        print(f"  batch={batch_size:>3}  {elapsed_ms:>7.1f} ms/batch  "
              f"({elapsed_ms / batch_size:.1f} ms/text)")

    del model
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    return results


def main() -> None:
    if not torch.cuda.is_available():
        print("CUDA not available — nothing to compare. Aborting.")
        return

    print(f"GPU: {torch.cuda.get_device_name(0)}")
    cpu = bench("cpu")
    gpu = bench("cuda")

    print("\n=== summary ===")
    print(f"{'batch':>6}  {'cpu (ms)':>10}  {'gpu (ms)':>10}  {'speedup':>8}")
    for bs in BATCH_SIZES:
        print(f"{bs:>6}  {cpu[bs]:>10.1f}  {gpu[bs]:>10.1f}  {cpu[bs] / gpu[bs]:>7.1f}x")


if __name__ == "__main__":
    main()
