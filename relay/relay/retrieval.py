"""Lexical retrieval tool: BM25 over pieces, optional re-rank, optional paper routing.

No embeddings on purpose: the library is three papers (~100 pages), BM25 needs no model
call and no tokens, and the hard cases here are lexical (exact numbers, model names,
symbols like d_model) where dense retrievers are weakest.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .papers import Piece, chunk

STOP = set("""a an the of to in on for and or is are was were be been by with as at from that this
these those it its into than then which what who whom how why when where does do did can could
would should will shall may might paper papers say says said about use used using over under
between their there they them we our you your i me my not no yes if but also more most such""".split())

ALIASES = {
    "MOSHI": ["moshi", "mimi", "helium", "inner monologue", "speech", "audio", "dialogue", "codec", "rq-transformer", "full-duplex"],
    "HNET": ["h-net", "hnet", "dynamic chunking", "chunking", "hierarchical", "byte", "bytes", "tokenizer-free", "routing module", "smoothing module"],
    "AIAYN": ["transformer", "attention is all you need", "multi-head", "wmt", "bleu", "positional encoding", "scaled dot-product", "base model", "big model", "label smoothing", "vaswani"],
}


def tokens(s: str) -> list[str]:
    s = s.lower().replace("-", " ")
    return [t for t in re.findall(r"[a-z0-9_.]+", s) if t not in STOP and len(t) > 1 and t.strip(".")]


@dataclass
class Hit:
    piece: Piece
    score: float


class Retriever:
    def __init__(self, piece_words: int, k: int, rerank: bool, route: bool = True):
        self.piece_words, self.k, self.rerank, self.route = piece_words, k, rerank, route
        self.pieces = chunk(piece_words, max(10, piece_words // 5))
        self.docs = [tokens(p.text + " " + p.section) for p in self.pieces]
        self.tf = [Counter(d) for d in self.docs]
        self.df = Counter(t for d in self.docs for t in set(d))
        self.N = len(self.docs)
        self.avgdl = sum(map(len, self.docs)) / max(1, self.N)

    @property
    def name(self) -> str:
        return f"w{self.piece_words}-k{self.k}-{'rr' if self.rerank else 'norr'}"

    def _bm25(self, q: list[str], i: int, k1=1.4, b=0.75) -> float:
        tf, dl, s = self.tf[i], len(self.docs[i]), 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / self.avgdl))
        return s

    def paper_hint(self, question: str) -> str | None:
        ql = question.lower()
        scores = {p: sum(a in ql for a in al) for p, al in ALIASES.items()}
        best = max(scores, key=scores.get)
        ranked = sorted(scores.values(), reverse=True)
        return best if ranked[0] > 0 and ranked[0] > ranked[1] else None

    def search(self, question: str, k: int | None = None, paper: str | None = None) -> list[Hit]:
        k = k or self.k
        q = tokens(question)
        hint = paper or (self.paper_hint(question) if self.route else None)
        scored = []
        for i, p in enumerate(self.pieces):
            s = self._bm25(q, i)
            if s <= 0:
                continue
            if hint and p.paper == hint:
                s *= 1.35  # soft boost, not a filter: questions can span papers
            scored.append((s, i))
        scored.sort(reverse=True)
        cands = scored[: (k * 4 if self.rerank else k)]
        if self.rerank and cands:
            qset, qbig = set(q), set(zip(q, q[1:]))
            rs = []
            for s, i in cands:
                d = self.docs[i]
                cover = len(qset & set(d)) / max(1, len(qset))
                bigr = len(qbig & set(zip(d, d[1:])))
                nums = [t for t in q if any(c.isdigit() for c in t)]
                numhit = sum(t in set(d) for t in nums)
                rs.append((s * (0.4 + cover) + 1.5 * bigr + 2.0 * numhit, i))
            rs.sort(reverse=True)
            cands = rs
        return [Hit(self.pieces[i], s) for s, i in cands[:k]]


SETTINGS = {
    # name: (piece_words, k, rerank)
    "small-k8-rr": (120, 8, True),
    "mid-k4-rr": (250, 4, True),
    "mid-k4-norr": (250, 4, False),
    "large-k2-norr": (500, 2, False),
    "mid-k2-rr": (250, 2, True),  # degraded setting used near the token limit
}
