"""Recompute every table in the write-up from results/results.csv (and probe_raw.jsonl).

Percentiles use the nearest-rank method on status=ok rows only (rows where the person got
a verified answer). Rate-limited rows also get an instant honest message; their timing is
reported separately so it can't flatter the latency numbers.

Cost per 1,000 answers uses Groq's published on-demand prices (the free tier itself costs
$0), so it is "what this would cost on a paid plan". Prices are in PRICES below.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"

# $ per million tokens (input, output). Update from groq.com/pricing when re-running.
PRICES = {
    "openai/gpt-oss-120b": (0.15, 0.60),   # VERIFY against groq.com/pricing before submitting
    "openai/gpt-oss-20b": (0.075, 0.30),   # VERIFY
}


def pct(xs, p):
    xs = sorted(x for x in xs if x == x)
    if not xs:
        return None
    k = max(1, math.ceil(p / 100 * len(xs)))
    return xs[k - 1]


def fmt3(xs):
    v = [pct(xs, p) for p in (50, 95, 99)]
    return " / ".join("–" if x is None else f"{x:.0f}" for x in v)


def fmt2(xs):
    v = [pct(xs, p) for p in (50, 95)]
    return " / ".join("–" if x is None else f"{x:.0f}" for x in v)


def cost(row):
    pin, pout = PRICES.get(row["model"], (0, 0)) if isinstance(row.get("model"), str) else (0, 0)
    return (row["tokens_in"] * pin + row["tokens_out"] * pout) / 1e6


def load_tables(df: pd.DataFrame) -> tuple[str, str]:
    a = ["| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |",
         "|---|---|---|---|---|---|---|"]
    b = ["| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |",
         "|---|---|---|---|---|---|---|---|"]
    peaks = {}
    pf = RES / "peaks.jsonl"
    if pf.exists():
        for line in pf.read_text().splitlines():
            d = json.loads(line)
            peaks[(d["scenario"], d["source"])] = max(d["peak_queue"].values())
    for (scen, src), g in df[~df.scenario.str.startswith("quality")].groupby(["scenario", "source"], sort=False):
        ts = pd.to_datetime(g.start_time)
        dur = (ts.max() - ts.min()).total_seconds() + (g.total_ms.max() / 1000)
        span = max(1.0, (ts.max() - ts.min()).total_seconds())
        ok = g[g.status == "ok"]
        n = len(g)
        a.append(f"| {scen} | {src} | {n} | {dur:.0f} | {n / span * 60:.1f} | {fmt3(ok.first_token_ms)} | {fmt3(ok.total_ms)} |")
        tpa = (ok.tokens_in + ok.tokens_out).mean() if len(ok) else float("nan")
        c = ok.apply(cost, axis=1).mean() * 1000 if len(ok) else float("nan")
        b.append(f"| {scen} | {100 * (g.status == 'ok').mean():.1f} | {100 * (g.status == 'rate_limited').mean():.1f} | "
                 f"{100 * (g.status == 'timeout').mean():.1f} | {g.retries.mean():.2f} | {peaks.get((scen, src), 'not measured')} | "
                 f"{tpa:.0f} | ${c:.2f} |")
    return "\n".join(a), "\n".join(b)


def retrieval_table(df: pd.DataFrame) -> str:
    t = ["| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |",
         "|---|---|---|---|---|---|---|---|"]
    q = df[df.scenario.str.startswith("quality")]
    for (setting, src), g in q.groupby(["scenario", "source"], sort=False):
        n = len(g)
        ok = g[g.status == "ok"]
        gr = g.answer_grade.fillna("")
        t.append(f"| {setting} ({src}) | {n} | {100 * (gr == 'correct').mean():.1f} | {100 * (gr == 'wrong').mean():.1f} | "
                 f"{100 * (gr == 'refused_correctly').mean():.1f} | {(ok.tokens_in + ok.tokens_out).mean():.0f} | "
                 f"{fmt2(ok.first_token_ms)} | {fmt2(ok.total_ms)} |")
    return "\n".join(t)


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else RES / "results.csv"
    df = pd.read_csv(src)
    df["first_token_ms"] = pd.to_numeric(df.first_token_ms, errors="coerce")
    excluded = df[df.scenario.str.endswith("-netfail")]
    df = df[~df.scenario.str.endswith("-netfail")]
    a, b = load_tables(df)
    out = ["## Table 2a", a, "", "## Table 2b", b, "", "## Table 3", retrieval_table(df)]
    q = df[df.scenario.str.startswith("quality")]
    if len(q):
        out += ["", "Refused wrongly % per setting:"]
        for s, g in q.groupby("scenario"):
            out.append(f"- {s}: {100 * (g.answer_grade == 'refused_wrongly').mean():.1f}% (n={len(g)}), errors/non-ok: {(g.status != 'ok').sum()}")
    out += ["", f"Excluded from tables (laptop lost network; HTTP status 0, request never reached Groq): "
            + ", ".join(f"{s} ({len(g)} rows: {g.status.value_counts().to_dict()})" for s, g in excluded.groupby("scenario"))]
    text = "\n".join(out)
    (src.parent / "tables.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
