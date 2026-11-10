"""Open-loop load generator. Arrivals are Poisson at a rate that follows a profile, so a
slow system does not slow the arrivals down (closed-loop generators hide overload).

Each simulated person has a patience drawn from a lognormal distribution; if no verified
sentence has reached them by then they give up, and we cancel their request so it stops
consuming budget. That cancel matters: without it, given-up requests keep burning tokens.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import random
import time
from dataclasses import asdict
from pathlib import Path

from .pipeline import Relay, Trace

CSV_COLS = ["request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status",
            "retries", "tokens_in", "tokens_out", "retrieval_setting", "answer_grade",
            # extra columns (after the required ones): needed for cost, degradation and latency split
            "model", "cache_hit", "degrade", "shed_reason", "wait_ms", "work_ms", "retry_ms", "calls"]


def extras(tr) -> dict:
    return {"model": tr.model, "cache_hit": int(tr.cache_hit), "degrade": tr.degrade, "shed_reason": tr.shed_reason,
            "wait_ms": round(tr.wait_ms), "work_ms": round(tr.work_ms), "retry_ms": round(tr.retry_ms), "calls": tr.calls}


PREFIXES = ["Quick question:", "For my project,", "I'm debugging something -", "Sorry if obvious,",
            "Urgent:", "Can you check", "Team wants to know", "Help please,", "Remind me", "Before standup,"]


def patience(rng: random.Random, median_s: float = 12.0, sigma: float = 0.5) -> float:
    return min(60.0, max(3.0, median_s * math.exp(rng.gauss(0, sigma))))


async def run_scenario(relay: Relay, name: str, source: str, profile: list[tuple[float, float]],
                       questions: list[dict], out_csv: Path, out_jsonl: Path, seed: int = 1,
                       zipf_s: float = 1.1, patience_median: float = 12.0, grader=None,
                       on_tick=None, paraphrase_p: float = 0.6) -> list[dict]:
    """profile: [(duration_s, rate_per_min), ...]"""
    rng = random.Random(seed)
    weights = [1 / (i + 1) ** zipf_s for i in range(len(questions))]
    rows, tasks = [], []
    t_start = time.monotonic()
    n = 0

    async def one(i: int, q: dict, pat: float, wall: float):
        rid = f"{name}-{i:04d}"
        first = asyncio.Event()
        task = asyncio.create_task(relay.answer(rid, q["q"], emit=lambda s: first.set()))
        gave_up = False
        try:
            await asyncio.wait_for(asyncio.shield(first.wait()), timeout=pat)
        except asyncio.TimeoutError:
            if not task.done():
                gave_up = True
                task.cancel()
        tr: Trace | None = None
        try:
            tr = await asyncio.wait_for(task, timeout=90)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            tr = None
        except Exception as e:  # noqa: BLE001
            tr = Trace(rid, q["q"], status="error", answer=repr(e))
        live = relay.live.pop(rid, None)
        if tr is None:
            tr = live or Trace(rid, q["q"])
            tr.status = "timeout"
        if gave_up:
            tr.status = "timeout"
            tr.first_token_ms = None
            tr.total_ms = pat * 1000
        grade = ""
        if grader and tr.status == "ok":
            grade = grader(q, tr)
        row = {"request_id": rid, "scenario": name, "source": source,
               "start_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(wall)) + f".{int((wall % 1) * 1000):03d}Z",
               "first_token_ms": "" if tr.first_token_ms is None else round(tr.first_token_ms),
               "total_ms": round(tr.total_ms), "status": tr.status, "retries": tr.retries,
               "tokens_in": tr.tokens_in, "tokens_out": tr.tokens_out,
               "retrieval_setting": relay.r.name, "answer_grade": grade, **extras(tr)}
        rows.append(row)
        detail = asdict(tr)
        detail.update(scenario=name, source=source, patience_s=round(pat, 2), gave_up=gave_up, qid=q.get("id"), grade=grade)
        with open(out_jsonl, "a") as f:
            f.write(json.dumps(detail) + "\n")

    for dur, rate in profile:
        seg_end = time.monotonic() + dur
        while True:
            if rate <= 0:
                await asyncio.sleep(max(0, seg_end - time.monotonic()))
                break
            gap = rng.expovariate(rate / 60.0)
            if time.monotonic() + gap > seg_end:
                await asyncio.sleep(max(0, seg_end - time.monotonic()))
                break
            await asyncio.sleep(gap)
            q = dict(rng.choices(questions, weights)[0])
            if rng.random() < paraphrase_p:  # people word the same question differently -> cache miss
                q["q"] = rng.choice(PREFIXES) + " " + q["q"][0].lower() + q["q"][1:]
            tasks.append(asyncio.create_task(one(n, q, patience(rng, patience_median), time.time())))
            n += 1
    await asyncio.gather(*tasks)
    rows.sort(key=lambda r: r["request_id"])
    new = not out_csv.exists()
    with open(out_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"[{name}] {n} requests in {time.monotonic() - t_start:.0f}s", flush=True)
    return rows
