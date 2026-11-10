"""Fetch the three library papers at pinned versions and split them into citable pieces.

Each piece carries (paper, page, section) so every answer can cite a location a human
can check in the PDF. Pages come from pdftotext's form-feed page breaks; sections come
from numbered heading lines ("3.2 Attention", "4 Experiments").
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

PAPERS = {
    "AIAYN": ("1706.03762", 7, "Attention Is All You Need"),
    "MOSHI": ("2410.00037", 1, "Moshi: a speech-text foundation model for real-time dialogue"),
    "HNET": ("2507.07955", 2, "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling"),
}

HEADING = re.compile(r"^\s*((?:[A-H]|\d{1,2})(?:\.\d{1,2}){0,3})\.?\s+([A-Z][A-Za-z0-9 ,:;()'\-/&]{2,80})\s*$")
REFS = re.compile(r"^\s*(References|Bibliography)\s*$", re.I)


@dataclass
class Piece:
    pid: str
    paper: str
    page: int
    section: str
    text: str

    def cite(self) -> str:
        sec = f", §{self.section}" if self.section else ""
        return f"{self.paper} p.{self.page}{sec}"


def fetch(force: bool = False) -> dict[str, Path]:
    DATA.mkdir(parents=True, exist_ok=True)
    out = {}
    for key, (aid, ver, _) in PAPERS.items():
        pdf = DATA / f"{aid}v{ver}.pdf"
        if force or not pdf.exists():
            url = f"https://arxiv.org/pdf/{aid}v{ver}"
            req = urllib.request.Request(url, headers={"User-Agent": "relay-assignment/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                pdf.write_bytes(r.read())
        txt = pdf.with_suffix(".txt")
        if force or not txt.exists():
            subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), str(txt)], check=True)
        out[key] = txt
    return out


def _pages(txt: Path) -> list[str]:
    return txt.read_text(encoding="utf-8", errors="replace").split("\f")


def chunk(piece_words: int = 200, overlap: int = 40, drop_references: bool = True) -> list[Piece]:
    """Split each paper into ~piece_words windows, never crossing a page boundary
    (so the page citation is exact). Section is the last heading seen."""
    pieces: list[Piece] = []
    txts = {k: DATA / f"{aid}v{ver}.txt" for k, (aid, ver, _) in PAPERS.items()}
    for key, txt in txts.items():
        section = ""
        in_refs = False
        for pno, page in enumerate(_pages(txt), start=1):
            words: list[tuple[str, str]] = []  # (word, section at that word)
            for line in page.splitlines():
                if REFS.match(line):
                    in_refs = True
                    section = "References"
                m = HEADING.match(line)
                if m and len(line.split()) <= 10 and not in_refs:
                    section = f"{m.group(1)} {m.group(2).strip()}"
                for w in line.split():
                    words.append((w, section))
            if drop_references and in_refs and all(s == "References" for _, s in words):
                continue
            step = max(1, piece_words - overlap)
            for i in range(0, max(1, len(words)), step):
                win = words[i : i + piece_words]
                if len(win) < 25 and i > 0:
                    break
                if not win:
                    continue
                sec = win[0][1]
                if drop_references and sec == "References":
                    continue
                text = " ".join(w for w, _ in win)
                pieces.append(Piece(f"{key}-{pno}-{i}", key, pno, sec, text))
    return pieces


def save_pieces(pieces: list[Piece], path: Path) -> None:
    path.write_text("\n".join(json.dumps(asdict(p)) for p in pieces))


if __name__ == "__main__":
    fetch()
    for size in (120, 250, 500):
        ps = chunk(size, size // 5)
        print(size, len(ps), "pieces")
