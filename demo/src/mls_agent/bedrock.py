"""Bedrock access. In-account, in-region, reached over a private endpoint in
deployment — which is what makes the residency claim hold (RESPONSE.md 3.8).

Two cost traps worth knowing, both found the hard way:

1.  `amazon.nova-2-lite-v1:0` cannot be invoked on demand at all. It requires an
    inference profile.
2.  The profiles are not priced alike. `us.` bills $0.33/$2.75 per million tokens
    against `global.` at $0.30/$2.50 — a silent 10% surcharge for picking the
    obvious-looking one.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import boto3
from botocore.config import Config

# Bedrock throttles on-demand invocation per account, and the default ceiling is
# lower than it looks: eight concurrent embed calls hit ThrottlingException within
# minutes. Four plus adaptive retry — which rate-limits client-side rather than
# just retrying into the same wall — sustains the backfill without failing.
EMBED_CONCURRENCY = 4

BOTO_CONFIG = Config(
    retries={"max_attempts": 10, "mode": "adaptive"},
    read_timeout=60,
)

REGION = os.environ.get("AWS_REGION", "us-east-1")

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
EMBED_DIMS = 256  # Matryoshka: Titan V2 supports 256/512/1024. Smaller index, same recall.
# ⚠ `global.` is a CROSS-REGION inference profile: it routes to whichever Region
# has capacity. Fine for this demo, and 10% cheaper than `us.`, but it would
# break a regional residency requirement silently — no error, no log line. A
# residency-constrained deployment must pin to a single Region, using provisioned
# throughput where on-demand is unavailable. See RESPONSE.md 4.1.
TEXT_MODEL = "global.amazon.nova-2-lite-v1:0"

# us-east-1, verified July 2026. Re-check before quoting these anywhere.
EMBED_USD_PER_1M_TOKENS = 0.02
TEXT_USD_PER_1M_INPUT = 0.30
TEXT_USD_PER_1M_OUTPUT = 2.50


@dataclass
class Usage:
    """Running tally. The whole argument is about how much work happens where."""

    embed_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_embed(self, tokens: int) -> None:
        with self._lock:  # embed_many runs these concurrently
            self.calls += 1
            self.embed_tokens += tokens

    @property
    def usd(self) -> float:
        return (
            self.embed_tokens * EMBED_USD_PER_1M_TOKENS
            + self.input_tokens * TEXT_USD_PER_1M_INPUT
            + self.output_tokens * TEXT_USD_PER_1M_OUTPUT
        ) / 1_000_000

    def __str__(self) -> str:
        return (
            f"{self.calls} calls · {self.embed_tokens:,} embed / "
            f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens · ${self.usd:.4f}"
        )


@dataclass
class Bedrock:
    region: str = REGION
    usage: Usage = field(default_factory=Usage)
    _rt: object = None

    def __post_init__(self):
        self._rt = boto3.client("bedrock-runtime", region_name=self.region, config=BOTO_CONFIG)

    def embed(self, text: str) -> list[float]:
        """One passage to one vector.

        Called at ingest for documents, and once per question at query time.
        Never for structured fields — see RESPONSE.md 3.3.
        """
        resp = self._rt.invoke_model(
            modelId=EMBED_MODEL,
            body=json.dumps({"inputText": text, "dimensions": EMBED_DIMS, "normalize": True}),
        )
        payload = json.loads(resp["body"].read())

        self.usage.record_embed(payload.get("inputTextTokenCount", 0))
        return payload["embedding"]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Titan has no batch endpoint, so throughput comes from concurrency.

        Serially this ran at ~950 ms per passage — a 300-document backfill took
        twenty minutes, which rather undercuts an argument about doing the work
        once and cheaply. The calls are independent and network-bound, so a small
        thread pool is the whole fix.
        """
        if not texts:
            return []
        if len(texts) == 1:
            return [self.embed(texts[0])]

        with ThreadPoolExecutor(max_workers=EMBED_CONCURRENCY) as pool:
            return list(pool.map(self.embed, texts))

    def generate(self, prompt: str, *, max_tokens: int = 1200, temperature: float = 0.8) -> str:
        resp = self._rt.converse(
            modelId=TEXT_MODEL,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        usage = resp.get("usage", {})
        self.usage.calls += 1
        self.usage.input_tokens += usage.get("inputTokens", 0)
        self.usage.output_tokens += usage.get("outputTokens", 0)

        return resp["output"]["message"]["content"][0]["text"]


def estimate_tokens(text: str) -> int:
    """Rough token count without a tokenizer round trip.

    Used to *price* the draft design's per-query re-embedding rather than
    actually spending it. Measure what is cheap; calculate what is not.
    """
    return max(1, len(text) // 4)
