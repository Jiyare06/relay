"""Re-apply the (deterministic) grader to every stored answer and rewrite answer_grade in
results.csv. Grading only reads the answer text and its citations, so this changes no
measurement; it exists because the first grader missed gpt-oss's Unicode spacing
("3.5 days"). Every change is listed in results/regrade_log.csv."""
import csv, json
from pathlib import Path
from .run import grade
from .pipeline import Trace

RES = Path(__file__).resolve().parent.parent / "results"
QS = {q["id"]: q for q in json.loads((RES.parent / "questions.json").read_text())}

def main():
    traces = {}
    for f in RES.glob("*traces*.jsonl"):
        if "attempt1" in f.name:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            traces[d["request_id"]] = d
    rows = list(csv.DictReader(open(RES / "results.csv", encoding="utf-8")))
    log = []
    for r in rows:
        d = traces.get(r["request_id"])
        if not d or not d.get("qid") or r["status"] != "ok" or r["source"] != "real":
            continue
        tr = Trace(d["request_id"], d["question"], answer=d["answer"], cited=d["cited"], cache_hit=d["cache_hit"])
        g = grade(QS[d["qid"]], tr)
        if g != r["answer_grade"]:
            log.append({"request_id": r["request_id"], "qid": d["qid"], "old": r["answer_grade"], "new": g, "answer": d["answer"][:200]})
            r["answer_grade"] = g
    with open(RES / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    with open(RES / "regrade_log.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["request_id", "qid", "old", "new", "answer"]); w.writeheader(); w.writerows(log)
    print(f"{len(log)} grades changed")
    for l in log: print(l["qid"], l["old"], "->", l["new"])

if __name__ == "__main__":
    main()
