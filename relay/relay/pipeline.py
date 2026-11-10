"""The Relay pipeline.

  1 understand  (small model, JSON)        -- skipped when tokens are scarce
  2 retrieve    (tool: BM25 over pieces)   -- has its own timeout + fallback
  3 decide      (branch)                   -- refuse early / pick model + passages
  4 draft       (big model, streamed)      -- cites passages as [n]
  5 verify      (per sentence, deterministic) -- gates what the person sees
     loop: if the first sentence fails verification, redraft once with feedback

The person only ever sees sentences that passed verification, so "first token" in this
system means "first verified sentence", not the model's first token. That costs time
to first token (we hold ~1 sentence) and buys the guarantee in the brief: no unchecked
answer is shown just to look fast.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field

from .llm import CallResult, Governor, ModelClient, Shed, est_tokens, now
from .retrieval import Hit, Retriever, tokens as qtokens

REFUSAL = "The papers in this library do not say"
CITE_ALT = re.compile(r"【\s*(\d+)[^】]*】")

SYS_DRAFT = """You answer questions using ONLY the numbered passages from three papers:
AIAYN = "Attention Is All You Need" (arXiv 1706.03762v7), MOSHI = "Moshi" (arXiv 2410.00037v1),
HNET = "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling" (arXiv 2507.07955v2).
Rules:
- Every sentence that states a fact must end with the passage number(s) it came from, like [2] or [1][3].
- Copy numbers exactly as written in the passages. Never compute or recall numbers that are not in the passages.
- If the passages do not contain the answer, reply exactly: "The papers in this library do not say." and then one short sentence on what they do cover, with a citation.
- Be brief: at most 4 sentences. No preamble."""

SYS_UNDERSTAND = """Classify a question about three papers: AIAYN (the Transformer), MOSHI (speech-text
dialogue model, Mimi codec), HNET (dynamic chunking, H-Net, byte-level hierarchical models).
Return JSON only: {"in_scope": true|false, "paper": "AIAYN"|"MOSHI"|"HNET"|"ANY", "search": "<keywords to search for>"}
in_scope=false only if the question is clearly not about these papers' content."""


@dataclass
class Models:
    big: str = "openai/gpt-oss-120b"
    small: str = "openai/gpt-oss-20b"


@dataclass
class Policy:
    retry: str = "budgeted"          # budgeted | naive | none
    ttft_slo_s: float = 20.0          # we stop trying to answer after this (system deadline)
    model_ttft_timeout_s: float = 8.0  # a call with no first token by now is treated as stuck
    model_total_timeout_s: float = 25.0
    tool_timeout_s: float = 1.5
    use_understand: bool = True
    use_cache: bool = True
    min_score: float = 4.0            # BM25 score below which we refuse without drafting
    max_tokens: int = 700  # includes gpt-oss reasoning tokens
    # "sentence": every sentence must carry its own citation (v1).
    # "group": uncited sentences are held until the next cited sentence and verified together
    #          against that citation (v2, after v1 refused correct H-Net answers that cited once per paragraph)
    verify: str = "group"


@dataclass
class Trace:
    request_id: str
    question: str
    status: str = "error"
    answer: str = ""
    first_token_ms: float | None = None
    total_ms: float = 0.0
    retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    wait_ms: float = 0.0      # time queued in governor / backoff sleeps
    work_ms: float = 0.0      # time in successful model + tool work
    retry_ms: float = 0.0     # time burned in attempts that failed
    degrade: int = 0
    cache_hit: bool = False
    model: str = ""
    shed_reason: str = ""
    cited: list = field(default_factory=list)
    events: list = field(default_factory=list)


class Relay:
    def __init__(self, client: ModelClient, govs: dict[str, Governor], retriever: Retriever,
                 models: Models = Models(), policy: Policy = Policy(), tool_fault=None,
                 small_retriever: Retriever | None = None):
        self.c, self.govs, self.r = client, govs, retriever
        self.r_small = small_retriever or retriever
        self.m, self.p = models, policy
        self.cache: dict[str, Trace] = {}
        self.live: dict[str, Trace] = {}
        self.tool_fault = tool_fault  # optional async fn simulating tool latency/hangs

    # ------------------------------------------------------------------
    def _key(self, q: str) -> str:
        return " ".join(sorted(set(qtokens(q))))

    def _level(self, tok_est: int) -> int:
        """0 full, 1 skip understand, 2 small model + fewer passages, 3 no model budget."""
        big, small = self.govs[self.m.big], self.govs[self.m.small]
        br, bt = big.day_left()
        sr, st = small.day_left()
        big_ok = br >= 1 and bt >= tok_est
        small_ok = sr >= 1 and st >= tok_est
        if not big_ok and not small_ok:
            return 3
        if not big_ok:
            return 2
        w = big.wait_for(tok_est, *big.queue_ahead(0))
        frac_left = min(br / (big.lim.rpd or 1e9), bt / (big.lim.tpd or 1e18))
        if w > self.p.ttft_slo_s * 0.5:
            return 2 if small_ok else 1
        if w > 1.0 or frac_left < 0.2:
            return 1
        return 0

    async def _call(self, tr: Trace, model: str, msgs: list[dict], max_tokens: int, prio: int,
                    deadline: float, on_token=None) -> CallResult:
        gov = self.govs[model]
        est = est_tokens(msgs) + max_tokens
        attempts = 0
        while True:
            t_q = now()
            lease = await gov.acquire(est, prio, deadline)  # may raise Shed
            tr.wait_ms += (now() - t_q) * 1000
            try:
                res = await self.c.chat(model, msgs, max_tokens,
                                        ttft_timeout=min(self.p.model_ttft_timeout_s, max(0.5, deadline - now())),
                                        total_timeout=self.p.model_total_timeout_s, on_token=on_token)
            except asyncio.CancelledError:
                # person gave up mid-call: the provider still bills the prompt
                lease.release(est)
                tr.tokens_in += est_tokens(msgs)
                tr.calls += 1
                raise
            tr.calls += 1
            gov.on_headers(res.headers)
            used = (res.tokens_in + res.tokens_out) if res.status == "ok" else (est if res.status == "timeout" else 0)
            lease.release(used)
            tr.events.append({"model": model, "status": res.status, "http": res.http, "ttft": res.ttft_ms,
                              "ms": round(res.total_ms), "ra": res.retry_after, "t": round(time.time(), 3),
                              **({"body": res.body[:240]} if res.status != "ok" else {})})
            if res.status == "ok":
                tr.work_ms += res.total_ms
                tr.tokens_in += res.tokens_in
                tr.tokens_out += res.tokens_out
                tr.model = model
                return res
            tr.retry_ms += res.total_ms
            if res.status == "rate_limited":
                gov.on_429(res.retry_after, res.body)
            # ---- retry decision ----
            attempts += 1
            if self.p.retry == "none":
                return res
            if self.p.retry == "naive":
                if attempts > 3:
                    return res
                tr.retries += 1
                await asyncio.sleep(0.2)
                tr.retry_ms += 200
                continue
            # budgeted: at most 2 retries, only if the wait fits in what's left of the deadline,
            # never retry a timeout that already produced tokens (we'd pay twice for the prefix)
            if attempts > 2 or (res.status == "timeout" and res.ttft_ms is not None):
                return res
            backoff = res.retry_after if res.status == "rate_limited" and res.retry_after else (0.5 * 2 ** (attempts - 1))
            backoff *= random.uniform(0.8, 1.3)
            if now() + backoff + 1.0 > deadline:
                return res
            tr.retries += 1
            await asyncio.sleep(backoff)
            tr.wait_ms += backoff * 1000
            tr.retry_ms += backoff * 1000

    # ------------------------------------------------------------------
    async def _understand(self, tr: Trace, q: str, deadline: float) -> dict:
        msgs = [{"role": "system", "content": SYS_UNDERSTAND}, {"role": "user", "content": q}]
        try:
            res = await self._call(tr, self.m.small, msgs, 300, prio=1, deadline=min(deadline, now() + 4))
        except Shed:
            return {}
        if res.status != "ok":
            return {}
        m = re.search(r"\{.*\}", res.text, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            return {}

    async def _retrieve(self, tr: Trace, retr: Retriever, query: str, paper: str | None) -> list[Hit]:
        t0 = now()

        async def run():
            if self.tool_fault:
                await self.tool_fault()
            return retr.search(query, paper=paper)
        try:
            hits = await asyncio.wait_for(run(), timeout=self.p.tool_timeout_s)
            tr.work_ms += (now() - t0) * 1000
            return hits
        except asyncio.TimeoutError:
            tr.retry_ms += (now() - t0) * 1000
            tr.retries += 1
            tr.events.append({"tool": "retrieve", "status": "timeout"})
            t1 = now()
            hits = retr.search(query, paper=paper)  # fallback: in-process, no remote index
            tr.work_ms += (now() - t1) * 1000
            return hits

    @staticmethod
    def _passages(hits: list[Hit]) -> str:
        return "\n\n".join(f"[{i+1}] ({h.piece.cite()}) {h.piece.text}" for i, h in enumerate(hits))

    @staticmethod
    def check_sentence(sent: str, hits: list[Hit]) -> tuple[bool, str]:
        s = sent.strip()
        if not s:
            return True, ""
        if s.startswith(REFUSAL) or "do not say" in s.lower():
            return True, ""
        refs = [int(x) for x in re.findall(r"\[(\d+)\]", s)]
        if not refs:
            if len(s) < 40:  # tiny fragments / list markers
                return True, ""
            return False, "uncited sentence"
        if any(r < 1 or r > len(hits) for r in refs):
            return False, "cites a passage that was not provided"
        cited_text = " ".join(hits[r - 1].piece.text for r in refs).replace(",", "")
        body = re.sub(r"\[\d+\]", "", s).replace(",", "")
        for num in re.findall(r"\d+(?:\.\d+)?", body):
            if len(num) == 1:
                continue
            if num not in cited_text:
                return False, f"number {num} not in cited passages"
        return True, ""

    # ------------------------------------------------------------------
    async def answer(self, rid: str, q: str, emit=None) -> Trace:
        """emit(text) is called for each verified sentence (what the person sees)."""
        t0 = now()
        tr = Trace(rid, q)
        self.live[rid] = tr  # lets the caller read partial usage if it cancels us
        deadline = t0 + self.p.ttft_slo_s
        sent_out: list[str] = []

        def show(s: str):
            if tr.first_token_ms is None:
                tr.first_token_ms = (now() - t0) * 1000
            sent_out.append(s)
            if emit:
                emit(s)

        try:
            key = self._key(q)
            if self.p.use_cache and key in self.cache:
                c = self.cache[key]
                tr.cache_hit, tr.cited = True, c.cited
                show(c.answer)
                tr.status = "ok"
                return tr

            level = self._level(3000)
            tr.degrade = level
            if level == 3:
                tr.status, tr.shed_reason = "rate_limited", "daily_budget"
                hits = self.r.search(q)  # retrieval costs no model budget
                g = self.govs[self.m.small]
                show(self.budget_message(hits, g.rpd.wait(1, now())))
                return tr

            # 1 understand
            info = {}
            if self.p.use_understand and level == 0:
                info = await self._understand(tr, q, deadline)
            if info.get("in_scope") is False:
                tr.status = "ok"
                show(f"{REFUSAL} anything about that; they cover the Transformer, Moshi, and H-Net dynamic chunking.")
                return tr
            paper = info.get("paper") if info.get("paper") in ("AIAYN", "MOSHI", "HNET") else None
            query = q + " " + (info.get("search") or "")

            # 2 retrieve (fewer passages when degraded)
            retr = self.r_small if level >= 2 else self.r
            hits = await self._retrieve(tr, retr, query, paper)
            tr.cited = [h.piece.cite() for h in hits]

            # 3 decide
            if not hits or hits[0].score < self.p.min_score:
                tr.status = "ok"
                show(f"{REFUSAL} anything I could find about this.")
                return tr
            model = self.m.small if level >= 2 else self.m.big

            # 4 draft + 5 verify (sentence-gated), with one redraft loop
            feedback = ""
            for attempt in range(2):
                user = f"Passages:\n{self._passages(hits)}\n\nQuestion: {q}{feedback}"
                msgs = [{"role": "system", "content": SYS_DRAFT}, {"role": "user", "content": user}]
                buf = {"text": "", "failed": "", "pos": 0, "pending": ""}

                def on_token(d: str, buf=buf):
                    if buf["failed"]:
                        return
                    buf["text"] += d
                    # gpt-oss-120b cites with lenticular brackets (【3】, sometimes 【3†L5】); normalise to [3].
                    # Found from the real v2 run's logged drafts: every H-Net "uncited sentence" was this.
                    if "】" in buf["text"]:
                        buf["text"] = CITE_ALT.sub(r"[\1]", buf["text"])
                    # release complete sentences (end at citation or period followed by space)
                    while True:
                        m = re.search(r"(.+?(?:\]|[.!?]))(?=\s+[A-Z(])", buf["text"][buf["pos"]:], re.S)
                        if not m or buf["pos"] + m.end() >= len(buf["text"]):
                            break
                        sent = m.group(1)
                        # extend through trailing citations
                        rest = buf["text"][buf["pos"] + m.end():]
                        ext = re.match(r"(\s*\[\d+\])+", rest)
                        if ext:
                            sent += ext.group(0)
                        buf["pos"] += m.end() + (len(ext.group(0)) if ext else 0)
                        has_ref = re.search(r"\[\d+\]", sent) is not None
                        if self.p.verify == "group" and not has_ref and "do not say" not in sent.lower():
                            buf["pending"] += sent  # hold until a cited sentence vouches for it
                            continue
                        group = (buf["pending"] + sent) if has_ref else sent
                        ok, why = self.check_sentence(group, hits)
                        if not ok:
                            buf["failed"] = why
                            return
                        buf["pending"] = ""
                        show(group.strip())

                prio = 0  # in-progress requests jump ahead of new ones' first call
                res = await self._call(tr, model, msgs, self.p.max_tokens, prio, deadline + 10, on_token=on_token)
                if res.status != "ok":
                    tr.status = res.status
                    break
                tr.events.append({"draft": buf["text"][:700]})
                if not buf["failed"]:
                    tail = (buf["pending"] + buf["text"][buf["pos"]:]).strip()
                    if tail:
                        ok, why = self.check_sentence(tail, hits)
                        if ok:
                            show(tail)
                        else:
                            buf["failed"] = why
                if not buf["failed"]:
                    tr.status = "ok"
                    break
                tr.events.append({"verify": "fail", "why": buf["failed"], "attempt": attempt})
                if sent_out:  # already showed verified sentences: stop, don't contradict
                    show("(I stopped here because I could not verify the next statement against the papers.)")
                    tr.status = "ok"
                    break
                if attempt == 0:
                    feedback = f"\n\nYour previous draft failed a check ({buf['failed']}). Use only facts and numbers present in the passages, cite each sentence."
                    tr.retries += 1
                else:
                    show(f"{REFUSAL} this in a way I could verify. The closest passages are: " + "; ".join(tr.cited[:3]) + ".")
                    tr.status = "ok"
        except Shed as e:
            tr.status, tr.shed_reason = "rate_limited", e.reason
            if not sent_out:
                show(self.busy_message(e) if e.reason != "daily_budget" else self.budget_message(self.r.search(q), e.wait_s))
        except asyncio.CancelledError:
            tr.status = "timeout"
            tr.total_ms = (now() - t0) * 1000
            raise
        finally:
            tr.total_ms = (now() - t0) * 1000
            tr.answer = " ".join(sent_out)
        if tr.status == "ok" and not tr.cache_hit and tr.tokens_out and self.p.use_cache:
            self.cache[self._key(q)] = tr
        return tr

    @staticmethod
    def busy_message(e: Shed) -> str:
        secs = max(5, int(e.wait_s)) if e.wait_s else 30
        return (f"Too many people are asking right now; a checked answer would take about {secs} s more than "
                "I think you'd wait. I won't send a rushed, unchecked answer. Please ask again in a minute.")

    @staticmethod
    def budget_message(hits: list[Hit] | None = None, wait_s: float | None = None) -> str:
        # Groq's daily request budget refills continuously (~1 request per 86 s on 1,000/day),
        # so "come back tomorrow" would be wrong: a few new answers become possible each few minutes.
        when = f"in about {max(1, round(wait_s / 60))} min" if wait_s else "in a few minutes"
        msg = ("Today's model budget is used up, so I can't write a new checked answer right now. "
               f"Capacity comes back gradually; try again {when}. Nobody has asked this exact question today, "
               "so there is no saved answer to show you.")
        if hits and hits[0].score >= 4.0:
            ex = " | ".join(f"{h.piece.cite()}: \"{h.piece.text[:160]}…\"" for h in hits[:3])
            msg += (" Meanwhile, these are the passages most likely to answer it. They are raw excerpts, not an "
                    "answer, and I have not checked them against your question: " + ex)
        else:
            msg += " I also couldn't find a passage in the three papers that looks relevant."
        return msg
