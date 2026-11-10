"""Stand-in model service: OpenAI/Groq-compatible /chat/completions with SSE streaming.

It can be slow, fail, refuse at RPM/TPM/RPD/TPD limits (Groq-style 429 body + headers),
get slower as in-flight requests grow, and streams its reply a piece at a time.
All behaviour comes from a calibration JSON (see calibration/standin.json), filled
from measurements of the real service (probe.py). Limits can be changed at runtime via
POST /admin/config to simulate "limits change without notice".

Run:  python -m relay.standin --port 8787 --config calibration/standin.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import time
from collections import deque

from aiohttp import web

DEFAULT = {
    "models": {
        "openai/gpt-oss-120b": {"rpm": 30, "tpm": 12000, "rpd": 1000, "tpd": 100000,
                                    "ttft_median_ms": 300, "ttft_sigma": 0.5, "tok_per_s": 280},
        "openai/gpt-oss-20b": {"rpm": 30, "tpm": 6000, "rpd": 14400, "tpd": 500000,
                                 "ttft_median_ms": 200, "ttft_sigma": 0.5, "tok_per_s": 700},
    },
    "load_knee": 8,          # in-flight requests before it starts slowing
    "load_alpha": 0.6,       # ttft multiplier slope past the knee
    "fail_rate": 0.01,       # 5xx before any token
    "midstream_fail_rate": 0.003,
    "slow_rate": 0.03,       # "succeeds slowly": ttft x slow_mult
    "slow_mult": 12.0,
    "count_max_tokens": True,  # TPM admission counts prompt + max_tokens (refund later)
    "seed": 7,
}


class _B:
    def __init__(self, limit, period):
        self.limit, self.rate = limit, (limit / period if limit else 0)
        self.level, self.t = float(limit or 0), time.monotonic()

    def refresh(self, t):
        if self.limit:
            self.level = min(self.limit, self.level + (t - self.t) * self.rate)
        self.t = t
        return self.level

    def wait(self, amount, t):
        if not self.limit:
            return 0.0
        d = amount - self.refresh(t)
        return 0.0 if d <= 0 else d / self.rate

    def reset_s(self, t):
        return 0.0 if not self.limit else (self.limit - self.refresh(t)) / self.rate


class Bucket:
    """Refilling buckets per limit, matching what we measured on Groq: after a per-minute
    429 capacity comes back in seconds, and the daily request budget refills ~1 per 86.4 s."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.b = {"rpm": _B(cfg.get("rpm"), 60), "tpm": _B(cfg.get("tpm"), 60),
                  "rpd": _B(cfg.get("rpd"), 86400), "tpd": _B(cfg.get("tpd"), 86400)}

    @property
    def day_req(self):
        return int(self.cfg.get("rpd", 0) - self.b["rpd"].refresh(time.monotonic()))

    def set_day_used(self, req, tok):
        t = time.monotonic()
        if self.b["rpd"].limit:
            self.b["rpd"].refresh(t); self.b["rpd"].level = max(0, self.b["rpd"].limit - req)
        if self.b["tpd"].limit:
            self.b["tpd"].refresh(t); self.b["tpd"].level = max(0, self.b["tpd"].limit - tok)

    def check(self, t, tokens) -> tuple[str | None, float]:
        for key, amt, label in (("rpd", 1, "requests per day (RPD)"), ("tpd", tokens, "tokens per day (TPD)"),
                                ("rpm", 1, "requests per minute (RPM)"), ("tpm", tokens, "tokens per minute (TPM)")):
            w = self.b[key].wait(amt, t)
            if w > 0:
                return label, w
        return None, 0.0

    def take(self, t, tokens):
        for key, amt in (("rpm", 1), ("rpd", 1), ("tpm", tokens), ("tpd", tokens)):
            if self.b[key].limit:
                self.b[key].refresh(t); self.b[key].level -= amt

    def refund(self, tokens):
        t = time.monotonic()
        for key in ("tpm", "tpd"):
            if self.b[key].limit:
                self.b[key].refresh(t); self.b[key].level = min(self.b[key].limit, self.b[key].level + tokens)

    def headers(self, t) -> dict:
        h = {}
        if self.b["rpd"].limit:
            h["x-ratelimit-limit-requests"] = str(self.cfg["rpd"])
            h["x-ratelimit-remaining-requests"] = str(int(max(0, self.b["rpd"].refresh(t))))
            h["x-ratelimit-reset-requests"] = f"{self.b['rpd'].reset_s(t):.2f}s"
        if self.b["tpm"].limit:
            h["x-ratelimit-limit-tokens"] = str(self.cfg["tpm"])
            h["x-ratelimit-remaining-tokens"] = str(int(max(0, self.b["tpm"].refresh(t))))
            h["x-ratelimit-reset-tokens"] = f"{self.b['tpm'].reset_s(t):.2f}s"
        return h


class StandIn:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rng = random.Random(cfg.get("seed", 7))
        self.buckets = {m: Bucket(c) for m, c in cfg["models"].items()}
        self.inflight = 0
        self.log: list[dict] = []

    def ntok(self, s: str) -> int:
        return max(1, int(len(s) / self.cfg.get("chars_per_token", 4.0)))

    def make_reply(self, msgs: list[dict], max_tokens: int) -> str:
        sys = msgs[0]["content"] if msgs else ""
        user = msgs[-1]["content"]
        if "Return JSON only" in sys:
            ql = user.lower()
            paper = "MOSHI" if ("moshi" in ql or "mimi" in ql) else "HNET" if ("chunk" in ql or "h-net" in ql) else "AIAYN" if "transformer" in ql else "ANY"
            return json.dumps({"in_scope": True, "paper": paper, "search": user[:80]})
        # draft: echo the first sentence of passage [1] with citation, like a terse grounded answer
        m = re.search(r"\[1\] \([^)]*\) (.+?[.!?])\s", user, re.S)
        if not m:
            return "The papers in this library do not say."
        s1 = re.sub(r"\s+", " ", m.group(1))[:300]
        m2 = re.search(r"\[2\] \([^)]*\) (.+?[.!?])\s", user, re.S)
        out = f"According to the passages, {s1} [1]"
        if m2:
            out += f" Also, {re.sub(r'\s+', ' ', m2.group(1))[:200]} [2]"
        words = out.split(" ")
        return " ".join(words[: max_tokens * 3 // 4])

    async def handle(self, req: web.Request) -> web.StreamResponse:
        body = await req.json()
        model = body.get("model")
        if model not in self.cfg["models"]:
            return web.json_response({"error": {"message": f"The model `{model}` does not exist", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
        mc = self.cfg["models"][model]
        msgs = body.get("messages", [])
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 256)
        prompt_toks = sum(self.ntok(m.get("content", "")) for m in msgs) + 4 * len(msgs)
        admit = prompt_toks + (max_tokens if self.cfg.get("count_max_tokens") else 0)
        b = self.buckets[model]
        t = time.monotonic()
        why, wait = b.check(t, admit)
        rec = {"t": time.time(), "model": model, "inflight": self.inflight, "prompt": prompt_toks}
        if why:
            rec["status"] = 429
            self.log.append(rec)
            lim = mc.get({"requests per minute (RPM)": "rpm", "tokens per minute (TPM)": "tpm", "requests per day (RPD)": "rpd", "tokens per day (TPD)": "tpd"}[why])
            msg = (f"Rate limit reached for model `{model}` in organization `org_standin` service tier `on_demand` on {why}: "
                   f"Limit {lim}, Requested {admit}. Please try again in {wait:.3f}s.")
            h = b.headers(t)
            h["retry-after"] = str(max(1, math.ceil(wait)))
            return web.json_response({"error": {"message": msg, "type": "tokens" if "token" in why else "requests", "code": "rate_limit_exceeded"}}, status=429, headers=h)
        b.take(t, admit)
        self.inflight += 1
        try:
            if self.rng.random() < self.cfg["fail_rate"]:
                await asyncio.sleep(self.rng.uniform(0.05, 0.6))
                rec["status"] = 503
                self.log.append(rec)
                return web.json_response({"error": {"message": "Service Unavailable", "type": "internal_server_error"}}, status=503)
            load = 1 + self.cfg["load_alpha"] * max(0, self.inflight - self.cfg["load_knee"]) / self.cfg["load_knee"]
            ttft = mc["ttft_median_ms"] / 1000 * math.exp(self.rng.gauss(0, mc["ttft_sigma"])) * load
            slow = self.rng.random() < self.cfg["slow_rate"]
            if slow:
                ttft *= self.cfg["slow_mult"]
            text = self.make_reply(msgs, max_tokens)
            pieces = re.findall(r"\S+\s*", text)
            # gpt-oss "thinks" first: hidden reasoning tokens delay the first visible token and are billed
            rmean = mc.get("reasoning_tokens_mean", 0)
            reasoning = int(self.rng.expovariate(1 / rmean)) if rmean else 0
            ttft += reasoning / mc["tok_per_s"] * load
            out_toks = self.ntok(text) + reasoning
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", **b.headers(time.monotonic())})
            await resp.prepare(req)
            rid = f"chatcmpl-standin-{self.rng.getrandbits(48):x}"

            async def send(obj):
                await resp.write(f"data: {json.dumps(obj)}\n\n".encode())
            await send({"id": rid, "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]})
            await asyncio.sleep(ttft)
            tps = mc["tok_per_s"] / load
            for i, p in enumerate(pieces):
                if i == len(pieces) // 2 and self.rng.random() < self.cfg["midstream_fail_rate"]:
                    await send({"error": {"message": "stream interrupted", "type": "internal_server_error"}})
                    rec["status"] = "midstream_fail"
                    self.log.append(rec)
                    return resp
                await send({"id": rid, "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"content": p}}]})
                await asyncio.sleep(max(0.0, self.ntok(p) / tps))
            usage = {"prompt_tokens": prompt_toks, "completion_tokens": out_toks, "total_tokens": prompt_toks + out_toks}
            await send({"id": rid, "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "x_groq": {"usage": usage}})
            await resp.write(b"data: [DONE]\n\n")
            # refund unused max_tokens, like a provider that settles on actual usage
            b.refund(admit - (prompt_toks + out_toks))
            rec.update(status=200, ttft=ttft, slow=slow, out=out_toks, load=load)
            self.log.append(rec)
            return resp
        finally:
            self.inflight -= 1

    async def admin(self, req: web.Request) -> web.Response:
        upd = await req.json()
        for k, v in upd.items():
            if k == "models":
                for m, mc in v.items():
                    self.cfg["models"][m].update(mc)
                    self.buckets[m] = Bucket(self.cfg["models"][m])  # limits changed without notice
            elif k == "day_used":  # {"model": {"req": n, "tok": n}}
                for m, u in v.items():
                    self.buckets[m].set_day_used(u.get("req", 0), u.get("tok", 0))
            elif k == "reset":
                self.buckets = {m: Bucket(c) for m, c in self.cfg["models"].items()}
            else:
                self.cfg[k] = v
        return web.json_response({"ok": True, "cfg": self.cfg})

    async def stats(self, req):
        return web.json_response({"inflight": self.inflight, "n": len(self.log),
                                  "day_req_used": {m: b.day_req for m, b in self.buckets.items()}})


def make_app(cfg: dict) -> web.Application:
    s = StandIn(cfg)
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_post("/openai/v1/chat/completions", s.handle)
    app.router.add_get("/openai/v1/models", lambda r: web.json_response({"data": [{"id": m} for m in cfg["models"]]}))
    app.router.add_post("/admin/config", s.admin)
    app.router.add_get("/admin/stats", s.stats)
    app["standin"] = s
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = json.loads(json.dumps(DEFAULT))
    if a.config:
        user = json.load(open(a.config))
        for k, v in user.items():
            if k == "models":
                cfg["models"] = v
            else:
                cfg[k] = v
    web.run_app(make_app(cfg), port=a.port, access_log=None)
