"""Entry point for quality evals and load scenarios.

  python -m relay.run eval  --source real    --setting mid-k4-rr
  python -m relay.run load  --source standin --scenario burst
"""
from __future__ import annotations

import argparse
import os
import asyncio
import json
import re
import unicodedata
import zlib
import time
from pathlib import Path

import aiohttp

from .llm import Governor, Limits, ModelClient, load_key
from .loadgen import run_scenario
from .pipeline import Models, Policy, Relay, Trace
from .retrieval import SETTINGS, Retriever

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
CAL = ROOT / "calibration"
GROQ = os.environ.get("RELAY_BASE", "https://api.groq.com/openai/v1")
STANDIN = "http://127.0.0.1:8787/openai/v1"

# name: (profile [(seconds, req/min)], patience median s, zipf skew, stand-in admin tweak)
SCENARIOS = {
    "steady":   ([(240, 6)], 12, 1.0, None),
    "burst":    ([(30, 4), (30, 120), (180, 4)], 12, 1.4, None),
    "overload": ([(300, 30)], 12, 1.0, None),
    "halved":   ([(240, 6)], 12, 1.0, "halve"),
    "daily":    ([(120, 6)], 12, 1.0, "daily"),
    "halved-static": ([(240, 6)], 12, 1.0, "halve"),
    "halved-static-naive": ([(240, 6)], 12, 1.0, "halve"),
    "burst-nocache": ([(30, 4), (30, 120), (180, 4)], 12, 1.4, None),
    "overload-naive": ([(300, 30)], 12, 1.0, None),
    "steady-real": ([(300, 3)], 12, 1.0, None),
}


def norm(s: str) -> str:
    """gpt-oss writes narrow no-break spaces and non-breaking hyphens ("3.5\u202fdays", "H\u2011Net");
    compare on plain ASCII spacing, and also with digit-unit spaces closed ("30 B" == "30B")."""
    s = unicodedata.normalize("NFKC", s)
    for ch in "\u2010\u2011\u2012\u2013\u2014":
        s = s.replace(ch, "-")
    for ch in "\u00a0\u202f\u2009\u2007":
        s = s.replace(ch, " ")
    s = s.lower().replace(",", "")
    return s + " || " + re.sub(r"(\d) (?=[a-z])", r"\1", s)


def grade(q: dict, tr: Trace) -> str:
    ans = norm(tr.answer)
    refused = "do not say" in ans or "not in the papers" in ans
    if not q.get("answerable", True):
        return "refused_correctly" if refused else "wrong"
    if refused:
        return "refused_wrongly"
    def ok(groups):
        return all(any(norm(alt).split(" || ")[0] in ans for alt in grp) for grp in groups)
    facts_ok = any(ok(g) for g in q["expect_any"]) if "expect_any" in q else ok(q.get("expect", []))
    refs = {int(x) for x in re.findall(r"\[(\d+)\]", tr.answer)}
    cited_papers = {tr.cited[r - 1].split()[0] for r in refs if 0 < r <= len(tr.cited)}
    cited_ok = (not q.get("paper")) or q["paper"] in cited_papers or (tr.cache_hit and refs)
    return "correct" if facts_ok and cited_ok else "wrong"


def limits_from(path: Path) -> dict[str, Limits]:
    d = json.loads(path.read_text())
    return {m: Limits(**v) for m, v in d.items()}


def build(source: str, setting: str, session, policy: Policy, models: Models, tool_fault=None) -> Relay:
    lims = limits_from(CAL / "limits.json")
    govs = {m: Governor(m, lims[m]) for m in (models.big, models.small)}
    base, key = (GROQ, load_key()) if source == "real" else (STANDIN, None)
    pw, k, rr = SETTINGS[setting]
    r = Retriever(pw, k, rr)
    rs = Retriever(*SETTINGS["mid-k2-rr"]) if setting != "mid-k2-rr" else r
    return Relay(ModelClient(base, key, session), govs, r, models, policy, tool_fault=tool_fault, small_retriever=rs)


def tool_fault_factory(seed=3):
    import random
    rng = random.Random(seed)

    async def fault():
        x = rng.random()
        if x < 0.02:
            await asyncio.sleep(5)  # hang -> tool timeout -> fallback
        else:
            await asyncio.sleep(rng.lognormvariate(-4.0, 0.8))  # ~20 ms typical
    return fault


async def admin(cfg: dict):
    async with aiohttp.ClientSession() as s:
        async with s.post("http://127.0.0.1:8787/admin/config", json=cfg) as r:
            return await r.json()


async def main(a):
    RES.mkdir(exist_ok=True)
    models = Models(big=a.big, small=a.small)
    qs = json.loads((ROOT / "questions.json").read_text())
    out_csv, out_jsonl = RES / a.out, RES / a.out.replace(".csv", "_traces.jsonl")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        if a.cmd == "eval":
            # quality runs measure answers, not overload: be patient and wait for the per-minute
            # window to clear before each question instead of shedding (that's the load tests' job)
            pol = Policy(use_cache=False, ttft_slo_s=120, verify=a.verify)
            relay = build(a.source, a.setting, s, pol, models)
            name = f"quality-{a.setting}-{a.verify}-{a.big.split('-')[2] if a.big.count('-') >= 2 else a.big}{a.tag}"
            # paced, one at a time: quality runs must not be distorted by our own rate limits
            qsel = [q for q in qs if not a.only or q["id"] in a.only.split(",")]
            for i, q in enumerate(qsel):
                while any(g.wait_for(3000) > 0 for g in relay.govs.values()):
                    await asyncio.sleep(1)
                rows = await _single(relay, name, a.source, q, out_csv, out_jsonl, i)
                print(q["id"], rows["status"], rows["answer_grade"], rows["first_token_ms"], rows["tokens_in"], rows["tokens_out"], flush=True)
                await asyncio.sleep(a.gap)
            return
        if a.cmd == "load":
            prof, pat, zipf, tweak = SCENARIOS[a.scenario]
            prof = [(d * a.scale, r) for d, r in prof]
            pol = Policy(use_cache=not a.scenario.endswith("nocache"),
                         retry="naive" if "naive" in a.scenario else "budgeted")
            if a.source == "standin":
                await admin({"reset": True})
                base = json.loads((CAL / "standin.json").read_text())["models"]
                if tweak == "halve":
                    await admin({"models": {m: {"rpm": v["rpm"] // 2, "tpm": v["tpm"] // 2} for m, v in base.items()}})
                elif tweak == "daily":
                    # 3pm, daily budget gone: big model exhausted, small model has 6 requests left
                    await admin({"day_used": {models.big: {"req": base[models.big]["rpd"], "tok": 0},
                                              models.small: {"req": base[models.small]["rpd"] - 6, "tok": 0}}})
                else:
                    await admin({"models": base})
            relay = build(a.source, a.setting, s, pol, models,
                          tool_fault=tool_fault_factory() if a.source == "standin" else None)
            if "static" in a.scenario:  # ablation: governor ignores the provider's changed limits
                for g in relay.govs.values():
                    g.adaptive = False
            loadqs = [q for q in qs if q.get("load", True)]
            await run_scenario(relay, a.scenario, a.source, prof, loadqs, out_csv, out_jsonl,
                               seed=zlib.crc32(a.scenario.encode()) % 1000, zipf_s=zipf, patience_median=pat,
                               grader=grade if a.source == "real" else None)
            peaks = {m: g.peak_queue for m, g in relay.govs.items()}
            with open(RES / "peaks.jsonl", "a") as f:
                f.write(json.dumps({"scenario": a.scenario, "source": a.source, "peak_queue": peaks, "t": time.time()}) + "\n")
            print("peak queue", peaks, "limit changes", {m: g.limit_changes for m, g in relay.govs.items()})
            if a.source == "standin" and tweak:
                await admin({"reset": True, "models": base})


async def _single(relay, name, source, q, out_csv, out_jsonl, i):
    import csv
    from dataclasses import asdict
    from .loadgen import CSV_COLS, extras
    t_wall = time.time()
    tr = await relay.answer(f"{name}-{i:04d}", q["q"])
    g = grade(q, tr) if tr.status == "ok" else ""
    row = {"request_id": f"{name}-{i:04d}", "scenario": name, "source": source,
           "start_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_wall)) + "Z",
           "first_token_ms": "" if tr.first_token_ms is None else round(tr.first_token_ms),
           "total_ms": round(tr.total_ms), "status": tr.status, "retries": tr.retries,
           "tokens_in": tr.tokens_in, "tokens_out": tr.tokens_out, "retrieval_setting": relay.r.name, "answer_grade": g, **extras(tr)}
    new = not out_csv.exists()
    with open(out_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if new:
            w.writeheader()
        w.writerow(row)
    d = asdict(tr)
    d.update(scenario=name, source=source, qid=q["id"], grade=g)
    with open(out_jsonl, "a") as f:
        f.write(json.dumps(d) + "\n")
    return row


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["eval", "load"])
    ap.add_argument("--source", default="standin", choices=["real", "standin"])
    ap.add_argument("--setting", default="mid-k4-rr", choices=list(SETTINGS))
    ap.add_argument("--scenario", default="steady", choices=list(SCENARIOS))
    ap.add_argument("--big", default="openai/gpt-oss-120b")
    ap.add_argument("--small", default="openai/gpt-oss-20b")
    ap.add_argument("--only", default="")
    ap.add_argument("--verify", default="group", choices=["sentence", "group"])
    ap.add_argument("--tag", default="", help="suffix for the scenario name, e.g. _citefix")
    ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--scale", type=float, default=1.0, help="shrink scenario durations (smoke tests only)")
    ap.add_argument("--out", default="results.csv")
    asyncio.run(main(ap.parse_args()))
