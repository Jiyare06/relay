"""Model client + client-side rate governor.

The governor is the heart of the system. It keeps its own sliding-window account of
requests and tokens per minute and per day, syncs it from the provider's
x-ratelimit-* headers, and answers one question before any call is made:
"if this request joins the queue now, when will it get to run?"  If the answer is later
than the person will wait, we say so immediately instead of letting them time out.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp


def now() -> float:
    return time.monotonic()


def parse_dur(s: str | None) -> float | None:
    """Groq reset headers look like '7.66s', '2m59.56s', '1h2m3s', '120ms'."""
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    tot = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s):
        tot += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return tot


class Shed(Exception):
    def __init__(self, reason: str, wait_s: float = 0.0):
        super().__init__(reason)
        self.reason, self.wait_s = reason, wait_s


@dataclass
class Limits:
    rpm: int | None = None
    tpm: int | None = None
    rpd: int | None = None
    tpd: int | None = None
    max_inflight: int = 16


@dataclass(order=True)
class _Waiter:
    prio: int
    seq: int
    tokens: int = field(compare=False)
    deadline: float = field(compare=False)
    fut: asyncio.Future = field(compare=False)


class _Bucket:
    """Continuously refilling bucket. Groq behaves like this, not like a fixed window: after a
    per-minute 429 it said "try again in 2s" and a request 2.7 s later succeeded; the
    reset-requests header for the DAILY limit grows by ~86.4 s per request (= 86400/1000)."""

    def __init__(self, limit: int | None, period_s: float, headroom: float):
        self.inf = limit is None
        self.cap = float("inf") if self.inf else limit * headroom
        self.rate = 0.0 if self.inf else limit / period_s
        self.level = self.cap
        self.t = now()

    def refresh(self, t: float) -> float:
        if not self.inf:
            self.level = min(self.cap, self.level + (t - self.t) * self.rate)
        self.t = t
        return self.level

    def wait(self, amount: float, t: float) -> float:
        if self.inf:
            return 0.0
        deficit = amount - self.refresh(t)
        if deficit <= 0:
            return 0.0
        if amount > self.cap:  # can never fit in one go; wait for a full bucket
            deficit = self.cap - self.level
        return deficit / self.rate

    def take(self, amount: float, t: float) -> None:
        self.refresh(t)
        if not self.inf:
            self.level -= amount

    def give(self, amount: float, t: float) -> None:
        self.refresh(t)
        if not self.inf:
            self.level = min(self.cap, self.level + amount)

    def clamp(self, remaining: float, t: float) -> None:
        """Provider says only `remaining` is left: believe it if it's lower than our view."""
        self.refresh(t)
        if not self.inf:
            self.level = min(self.level, remaining)


class Governor:
    def __init__(self, name: str, limits: Limits, headroom: float = 0.9):
        self.name, self.lim, self.headroom = name, limits, headroom
        self.rpm = _Bucket(limits.rpm, 60, headroom)
        self.tpm = _Bucket(limits.tpm, 60, headroom)
        self.rpd = _Bucket(limits.rpd, 86400, headroom)
        self.tpd = _Bucket(limits.tpd, 86400, headroom)
        self.blocked_until = 0.0
        self.adaptive = True       # learn changed limits from headers / 429 bodies
        self.limit_changes: list = []
        self.inflight = 0
        self.heap: list[_Waiter] = []
        self.seq = itertools.count()
        self.peak_queue = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- accounting -------------------------------------------------------
    def day_left(self) -> tuple[float, float]:
        t = now()
        return self.rpd.refresh(t), self.tpd.refresh(t)

    def wait_for(self, tokens: int, ahead_tokens: int = 0, ahead_reqs: int = 0) -> float:
        """Seconds until a request of `tokens` behind the current queue could start."""
        t = now()
        return max(0.0, self.blocked_until - t,
                   self.tpm.wait(ahead_tokens + tokens, t), self.rpm.wait(ahead_reqs + 1, t),
                   self.tpd.wait(ahead_tokens + tokens, t), self.rpd.wait(ahead_reqs + 1, t))

    def queue_ahead(self, prio: int) -> tuple[int, int]:
        toks = sum(x.tokens for x in self.heap if x.prio <= prio)
        reqs = sum(1 for x in self.heap if x.prio <= prio)
        return toks, reqs

    # ---- admission --------------------------------------------------------
    async def acquire(self, tokens: int, prio: int, deadline: float) -> "Lease":
        at, ar = self.queue_ahead(prio)
        t = now()
        day_wait = max(self.rpd.wait(ar + 1, t), self.tpd.wait(at + tokens, t))
        if now() + day_wait > deadline:
            raise Shed("daily_budget", day_wait)
        est = self.wait_for(tokens, at, ar)
        if now() + est > deadline:
            raise Shed("would_miss_deadline", est)
        w = _Waiter(prio, next(self.seq), tokens, deadline, asyncio.get_running_loop().create_future())
        heapq.heappush(self.heap, w)
        self.peak_queue = max(self.peak_queue, len(self.heap))
        self._ensure_task()
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(w.fut), timeout=max(0.01, deadline - now()))
        except asyncio.TimeoutError:
            if not w.fut.done():
                w.fut.cancel()
            elif not w.fut.cancelled() and w.fut.exception() is None:
                self.release(tokens, 0)  # granted at the last instant: give it back
            raise Shed("deadline_in_queue")
        except asyncio.CancelledError:
            if not w.fut.done():
                w.fut.cancel()
            elif not w.fut.cancelled() and w.fut.exception() is None:
                self.release(tokens, 0)
            raise
        return Lease(self, tokens)

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._dispatch())

    async def _dispatch(self) -> None:
        while True:
            while self.heap and (self.heap[0].fut.done() or self.heap[0].deadline < now()):
                w = heapq.heappop(self.heap)
                if not w.fut.done():
                    w.fut.set_exception(Shed("deadline_in_queue"))
            if not self.heap:
                self._wake.clear()
                await self._wake.wait()
                continue
            head = self.heap[0]
            wait = self.wait_for(head.tokens)
            if wait <= 0 and self.inflight < self.lim.max_inflight:
                heapq.heappop(self.heap)
                self._record(head.tokens)
                self.inflight += 1
                head.fut.set_result(True)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(max(wait, 0.02), 1.0))
            except asyncio.TimeoutError:
                pass

    def _record(self, tokens: int) -> None:
        t = now()
        self.rpm.take(1, t)
        self.rpd.take(1, t)
        self.tpm.take(tokens, t)
        self.tpd.take(tokens, t)

    def release(self, est: int, actual: int | None) -> None:
        self.inflight -= 1
        if actual is not None and actual != est:
            t = now()
            # settle on real usage (we reserved prompt + max_tokens; most answers are shorter)
            if actual < est:
                self.tpm.give(est - actual, t)
                self.tpd.give(est - actual, t)
            else:
                self.tpm.take(actual - est, t)
                self.tpd.take(actual - est, t)
        self._wake.set()

    # ---- provider feedback ------------------------------------------------
    def _resize(self, which: str, limit: int) -> None:
        """Limits change without notice: adopt the provider's number, keep the same fill fraction."""
        b = getattr(self, which)
        if b.inf or abs(b.cap - limit * self.headroom) < 1:
            return
        period = 60 if which in ("rpm", "tpm") else 86400
        frac = b.refresh(now()) / b.cap if b.cap else 0
        nb = _Bucket(limit, period, self.headroom)
        nb.level = frac * nb.cap
        setattr(self, which, nb)
        setattr(self.lim, which, limit)
        self.limit_changes.append((round(time.time(), 1), which, limit))

    def on_headers(self, h) -> None:
        t = now()
        if self.adaptive:
            if h.get("x-ratelimit-limit-tokens"):
                self._resize("tpm", int(float(h["x-ratelimit-limit-tokens"])))
            if h.get("x-ratelimit-limit-requests"):
                self._resize("rpd", int(float(h["x-ratelimit-limit-requests"])))
        rem_tok = h.get("x-ratelimit-remaining-tokens")   # Groq: tokens per MINUTE
        if rem_tok is not None:
            self.tpm.clamp(float(rem_tok) * self.headroom, t)
        rem_req = h.get("x-ratelimit-remaining-requests")  # Groq: requests per DAY
        if rem_req is not None:
            self.rpd.clamp(float(rem_req) * self.headroom, t)

    def on_429(self, retry_after: float | None, body: str = "") -> None:
        ra = retry_after if retry_after is not None else 2.0
        t = now()
        self.blocked_until = max(self.blocked_until, t + ra)
        b = body.lower()
        m = re.search(r"\((rpm|tpm|rpd|tpd)\): limit (\d+)", b)
        if m and self.adaptive:
            self._resize(m.group(1), int(m.group(2)))
        if "(tpd)" in b or "tokens per day" in b:
            self.tpd.clamp(0, t)
        elif "(rpd)" in b or "requests per day" in b:
            self.rpd.clamp(0, t)
        elif "(tpm)" in b:
            self.tpm.clamp(0, t)
        elif "(rpm)" in b:
            self.rpm.clamp(0, t)


class Lease:
    def __init__(self, gov: Governor, est: int):
        self.gov, self.est, self.done = gov, est, False

    def release(self, actual: int | None = None) -> None:
        if not self.done:
            self.done = True
            self.gov.release(self.est, actual)


# ---------------------------------------------------------------------------
@dataclass
class CallResult:
    status: str  # ok | rate_limited | timeout | error
    text: str = ""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    http: int = 0
    retry_after: float | None = None
    body: str = ""
    headers: dict = field(default_factory=dict)


class ModelClient:
    def __init__(self, base_url: str, api_key: str | None, session: aiohttp.ClientSession):
        self.base, self.key, self.s = base_url.rstrip("/"), api_key, session

    async def chat(self, model: str, messages: list[dict], max_tokens: int, *, ttft_timeout: float,
                   total_timeout: float, temperature: float = 0.0, on_token=None) -> CallResult:
        t0 = now()
        body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": True}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"   # reasoning tokens count against TPM and delay the first answer token
        hdr = {"Content-Type": "application/json"}
        if self.key:
            hdr["Authorization"] = f"Bearer {self.key}"
        res = CallResult("error")
        try:
            async with asyncio.timeout(total_timeout):
                req = self.s.post(f"{self.base}/chat/completions", json=body, headers=hdr)
                async with asyncio.timeout(ttft_timeout):
                    resp = await req.__aenter__()
                try:
                    res.http = resp.status
                    res.headers = {k.lower(): v for k, v in resp.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after", "x-groq", "x-request"))}
                    if resp.status != 200:
                        res.body = (await resp.text())[:600]
                        res.retry_after = parse_dur(resp.headers.get("retry-after"))
                        res.status = "rate_limited" if resp.status == 429 else "error"
                        return res
                    parts: list[str] = []
                    first_deadline = t0 + ttft_timeout
                    it = resp.content.__aiter__()
                    while True:
                        try:
                            if res.ttft_ms is None:
                                line = await asyncio.wait_for(it.__anext__(), timeout=max(0.01, first_deadline - now()))
                            else:
                                line = await it.__anext__()
                        except StopAsyncIteration:
                            break
                        line = line.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        ev = json.loads(data)
                        if "error" in ev:
                            res.body = json.dumps(ev)[:600]
                            res.status = "error"
                            return res
                        for ch in ev.get("choices", []):
                            d = ch.get("delta", {}).get("content")
                            if d:
                                if res.ttft_ms is None:
                                    res.ttft_ms = (now() - t0) * 1000
                                parts.append(d)
                                if on_token:
                                    on_token(d)
                        u = ev.get("usage") or (ev.get("x_groq") or {}).get("usage")
                        if u:
                            res.tokens_in = u.get("prompt_tokens", 0)
                            res.tokens_out = u.get("completion_tokens", 0)
                    res.text = "".join(parts)
                    res.status = "ok"
                finally:
                    await resp.__aexit__(None, None, None)
        except (asyncio.TimeoutError, TimeoutError):
            res.status = "timeout"
        except aiohttp.ClientError as e:
            res.status, res.body = "error", repr(e)[:300]
        finally:
            res.total_ms = (now() - t0) * 1000
        if res.status == "ok" and not res.tokens_in:
            res.tokens_in = est_tokens(messages)
            res.tokens_out = max(1, len(res.text) // 4)
        return res


def est_tokens(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages) // 4 + 8 * len(messages)


def load_key() -> str | None:
    if os.environ.get("GROQ_API_KEY"):
        return os.environ["GROQ_API_KEY"]
    p = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None
