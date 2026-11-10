"""Measure the real Groq service: latency spread, limits, what a 429 looks like, recovery.

Every raw response (status, timings, all x-ratelimit headers, 429 bodies) is appended to
results/probe_raw.jsonl so the limits table can be recomputed.

  python -m relay.probe models
  python -m relay.probe latency --model openai/gpt-oss-20b --n 30 --prompt small|rag
  python -m relay.probe rpm --model openai/gpt-oss-20b --n 40
  python -m relay.probe tpm --model openai/gpt-oss-20b
  python -m relay.probe concurrency --model openai/gpt-oss-20b --n 12
"""
from __future__ import annotations

import argparse
import os
import asyncio
import json
import time
from pathlib import Path

import aiohttp

from .llm import ModelClient, load_key

GROQ = os.environ.get("RELAY_BASE", "https://api.groq.com/openai/v1")
RAW = Path(__file__).resolve().parent.parent / "results" / "probe_raw.jsonl"
FILLER = ("The Transformer uses multi-head self-attention and position-wise feed-forward layers. " * 40)


def log(kind: str, model: str, res, extra=None):
    RAW.parent.mkdir(exist_ok=True)
    rec = {"t": time.time(), "kind": kind, "model": model, "status": res.status, "http": res.http,
           "ttft_ms": res.ttft_ms, "total_ms": round(res.total_ms, 1), "tokens_in": res.tokens_in,
           "tokens_out": res.tokens_out, "retry_after": res.retry_after, "headers": res.headers,
           "body": res.body[:600]}
    if extra:
        rec.update(extra)
    with open(RAW, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def msgs(kind: str, i: int):
    if kind == "rag":
        return [{"role": "system", "content": "Answer briefly from the passages."},
                {"role": "user", "content": f"Passages: {FILLER}\n\nQuestion {i}: What does the Transformer use? One sentence."}]
    return [{"role": "user", "content": f"Reply with the single word OK. ({i})"}]


async def main(a):
    key = load_key()
    assert key, "set GROQ_API_KEY or put it in relay/.env"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        c = ModelClient(GROQ, key, s)
        if a.cmd == "models":
            async with s.get(f"{GROQ}/models", headers={"Authorization": f"Bearer {key}"}) as r:
                data = await r.json()
            ids = sorted(m["id"] for m in data.get("data", []))
            print("\n".join(ids))
            with open(RAW, "a") as f:
                f.write(json.dumps({"t": time.time(), "kind": "models", "ids": ids}) + "\n")
            return
        if a.cmd == "latency":
            for i in range(a.n):
                r = await c.chat(a.model, msgs(a.prompt, i), a.max_tokens, ttft_timeout=30, total_timeout=60)
                rec = log(f"latency-{a.prompt}", a.model, r)
                print(i, r.status, r.http, round(r.ttft_ms or -1), round(r.total_ms), r.tokens_in, r.tokens_out,
                      r.headers.get("x-ratelimit-remaining-requests"), r.headers.get("x-ratelimit-remaining-tokens"))
                await asyncio.sleep(a.gap)
            return
        if a.cmd in ("rpm", "concurrency"):
            # fire n tiny requests at once; record who gets 429 and with what
            async def one(i):
                r = await c.chat(a.model, msgs("small", i), 32, ttft_timeout=30, total_timeout=60)
                return log(a.cmd, a.model, r, {"i": i})
            t0 = time.time()
            recs = await asyncio.gather(*(one(i) for i in range(a.n)))
            ok = sum(r["http"] == 200 for r in recs)
            print(f"{a.cmd}: sent {a.n} in {time.time()-t0:.1f}s ok={ok} 429={sum(r['http']==429 for r in recs)}")
            for r in sorted(recs, key=lambda r: r["http"] != 429):
                if r["http"] != 200:
                    print(json.dumps({k: r[k] for k in ("http", "retry_after", "body", "headers")})[:700])
                    break
            if any(r["http"] == 429 for r in recs):
                await recovery(c, a.model)
            return
        if a.cmd == "tpm":
            # large prompts back-to-back until the token limit answers
            for i in range(a.n):
                r = await c.chat(a.model, msgs("rag", i), 300, ttft_timeout=30, total_timeout=60)
                log("tpm", a.model, r, {"i": i})
                print(i, r.http, r.tokens_in, r.headers.get("x-ratelimit-remaining-tokens"), r.headers.get("x-ratelimit-reset-tokens"), r.body[:200])
                if r.http == 429:
                    await recovery(c, a.model)
                    break


async def recovery(c: ModelClient, model: str):
    """Poll with a tiny request every 2 s until it succeeds; report time to recover."""
    t0 = time.time()
    while time.time() - t0 < 180:
        await asyncio.sleep(2)
        r = await c.chat(model, msgs("small", 0), 32, ttft_timeout=30, total_timeout=60)
        log("recovery", model, r, {"since_429_s": round(time.time() - t0, 2)})
        if r.http == 200:
            print(f"recovered after {time.time()-t0:.1f}s")
            return
    print("not recovered in 180s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--model", default="openai/gpt-oss-20b")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--prompt", default="small")
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--gap", type=float, default=2.5)
    asyncio.run(main(ap.parse_args()))
