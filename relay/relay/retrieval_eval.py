"""Token-free retrieval check: for each answerable question, do the retrieved passages
contain every expected fact (and come from the right paper)? Also the prompt size each
setting implies. Lets us shortlist settings before spending model budget."""
import json, sys
from pathlib import Path
from .retrieval import Retriever, SETTINGS

ROOT = Path(__file__).resolve().parent.parent
qs = json.loads((ROOT / "questions.json").read_text())

def run(route=True):
    out = {}
    for name, (pw, k, rr) in SETTINGS.items():
        r = Retriever(pw, k, rr, route=route)
        hit = n = 0; ptoks = 0; misses = []; top_unans = []
        for q in qs:
            hits = r.search(q["q"])
            ptoks += sum(len(h.piece.text) for h in hits) // 4
            if not q.get("answerable", True):
                top_unans.append(round(hits[0].score, 1) if hits else 0); continue
            n += 1
            text = " ".join(h.piece.text for h in hits).replace(",", "")
            ok = all(any(a.replace(",", "").lower() in text.lower() for a in g) for g in q["expect"]) and any(h.piece.paper == q["paper"] for h in hits)
            hit += ok
            if not ok: misses.append(q["id"])
        out[name] = dict(recall=round(100 * hit / n, 1), ctx_tokens=ptoks // len(qs), misses=misses, unans_top=top_unans,
                         ans_top_min=None)
        print(name, "route" if route else "noroute", out[name], flush=True)
    return out

if __name__ == "__main__":
    res = {"route": run(True), "noroute": run(False)}
    (ROOT / "results" / "retrieval_recall.json").write_text(json.dumps(res, indent=1))
