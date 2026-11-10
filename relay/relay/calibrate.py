"""Turn results/probe_raw.jsonl into calibration/standin.json and calibration/limits.json.

- TTFT median and lognormal sigma per model, from successful latency probes (RAG-size prompts)
- output speed (tokens/s) per model = tokens_out / (total - ttft)
- TPM and RPD from the x-ratelimit-limit-* headers Groq returns on every response
- RPM and TPD are not in headers; they stay at the documented values unless a 429 body names them
- slow-call and failure rates from what we saw
"""
from __future__ import annotations

import json
import math
import re
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "results" / "probe_raw.jsonl"


def main():
    recs = [json.loads(l) for l in RAW.read_text().splitlines() if l.strip()]
    sc = json.loads((ROOT / "calibration" / "standin.json").read_text())
    lim = json.loads((ROOT / "calibration" / "limits.json").read_text())
    report = {}
    for model in sorted({r.get("model") for r in recs if r.get("model")}):
        ok = [r for r in recs if r.get("model") == model and r.get("http") == 200 and r.get("ttft_ms")]
        rag = [r for r in ok if r["kind"] == "latency-rag"] or ok
        info = {"n_ok": len(ok)}
        if rag:
            tt = [r["ttft_ms"] for r in rag]
            logs = [math.log(x) for x in tt]
            med = st.median(tt)
            sig = st.pstdev(logs) if len(logs) > 1 else 0.4
            speeds = [r["tokens_out"] / max(0.001, (r["total_ms"] - r["ttft_ms"]) / 1000) for r in rag if r["tokens_out"] > 5]
            info.update(ttft_median_ms=round(med), ttft_sigma=round(sig, 3),
                        tok_per_s=round(st.median(speeds)) if speeds else None,
                        slow_rate=round(sum(x > 5 * med for x in tt) / len(tt), 3))
        hdr = [r["headers"] for r in recs if r.get("model") == model and r.get("headers")]
        for h in hdr:
            if "x-ratelimit-limit-tokens" in h:
                info["tpm_header"] = int(float(h["x-ratelimit-limit-tokens"]))
            if "x-ratelimit-limit-requests" in h:
                info["rpd_header"] = int(float(h["x-ratelimit-limit-requests"]))
        bodies = [r["body"] for r in recs if r.get("model") == model and r.get("http") == 429]
        for b in bodies:
            m = re.search(r"on (requests|tokens) per (minute|day) \((\w+)\): Limit (\d+)", b)
            if m:
                info[f"{m.group(3).lower()}_429"] = int(m.group(4))
        report[model] = info
        if model in sc["models"]:
            mc = sc["models"][model]
            for k in ("ttft_median_ms", "ttft_sigma", "tok_per_s"):
                if info.get(k):
                    mc[k] = info[k]
            for src, dst in (("tpm_header", "tpm"), ("rpd_header", "rpd"), ("rpm_429", "rpm"), ("tpm_429", "tpm"), ("rpd_429", "rpd"), ("tpd_429", "tpd")):
                if info.get(src):
                    mc[dst] = info[src]
                    if model in lim:
                        lim[model][dst] = info[src]
    fails = [r for r in recs if r.get("kind", "").startswith("latency") and r.get("http") not in (200, 429)]
    lat = [r for r in recs if r.get("kind", "").startswith("latency")]
    if lat:
        sc["fail_rate"] = max(0.005, round(len(fails) / len(lat), 3))
    slows = [v.get("slow_rate", 0) for v in report.values() if "slow_rate" in v]
    if slows:
        sc["slow_rate"] = max(0.01, round(max(slows), 3))
    (ROOT / "calibration" / "standin.json").write_text(json.dumps(sc, indent=1))
    (ROOT / "calibration" / "limits.json").write_text(json.dumps(lim, indent=1))
    (ROOT / "results" / "calibration_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
