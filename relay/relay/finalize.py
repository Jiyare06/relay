"""Assemble the submission's results/results.csv from the raw files, reproducibly.

Inputs (untouched originals kept in incoming/):
  incoming/results.csv, incoming/results_traces.jsonl   real-service rows, run on my laptop
  results/standin.csv, results/standin_traces.jsonl      stand-in rows, run on a separate Linux machine
Steps:
  1. Name every quality run by its verifier version (v1 'sentence' runs predate the flag).
  2. Label rows that failed because my laptop lost network (HTTP status 0, never reached
     Groq) as '*-netfail'. They stay in the file; analyze.py leaves them out of the tables.
  3. Re-grade every real answer with the current deterministic grader (Unicode-safe);
     every change is written to results/regrade_log.csv.
  4. Concatenate real + stand-in rows.
"""
import csv, json
from pathlib import Path
from .run import grade, ROOT as _R
QS_PATH = _R / "questions.json"
from .pipeline import Trace

ROOT = Path(__file__).resolve().parent.parent
INC, RES = ROOT / "incoming", ROOT / "results"
V1 = {"quality-small-k8-rr-20b": "quality-small-k8-rr-sentence-20b",
      "quality-mid-k4-rr-20b": "quality-mid-k4-rr-sentence-20b",
      "quality-mid-k4-norr-20b": "quality-mid-k4-norr-sentence-20b"}


def new_scenario(scen, status, trace):
    if scen in V1:
        return V1[scen]
    if scen == "quality-large-k2-norr-20b":
        return scen + "-netfail"   # 20/25 rows never reached Groq (laptop asleep); rerun as ...-sentence-20b
    if scen == "quality-mid-k4-rr-120b":
        neterr = trace and all(e.get("http") == 0 for e in trace["events"] if "model" in e)
        return scen + "-netfail" if status == "error" and neterr else "quality-mid-k4-rr-sentence-120b"
    return scen


def main():
    qs = {q["id"]: q for q in json.loads(QS_PATH.read_text())}
    traces = {}
    for line in (INC / "results_traces.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        traces[(d["request_id"], d["scenario"])] = d    # later lines win (one stale duplicate id)
    rows = list(csv.DictReader(open(INC / "results.csv", encoding="utf-8")))
    cols = list(rows[0].keys())
    out_rows, out_tr, log = [], [], []
    for r in rows:
        d = traces.get((r["request_id"], r["scenario"]))
        ns = new_scenario(r["scenario"], r["status"], d)
        rid = r["request_id"].replace(r["scenario"], ns, 1)
        if d and d.get("qid") and r["status"] == "ok":
            g = grade(qs[d["qid"]], Trace(rid, d["question"], answer=d["answer"], cited=d["cited"], cache_hit=d["cache_hit"]))
            if g != r["answer_grade"]:
                log.append({"request_id": rid, "qid": d["qid"], "old": r["answer_grade"], "new": g, "answer": d["answer"][:220]})
                r["answer_grade"] = g
        r["scenario"], r["request_id"] = ns, rid
        out_rows.append(r)
        if d:
            d = dict(d, scenario=ns, request_id=rid, grade=r["answer_grade"])
            out_tr.append(d)
    standin = list(csv.DictReader(open(RES / "standin.csv", encoding="utf-8")))
    allrows = out_rows + standin
    ids = [r["request_id"] for r in allrows]
    assert len(ids) == len(set(ids)), "duplicate request ids"
    with open(RES / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(allrows)
    with open(RES / "traces.jsonl", "w", encoding="utf-8") as f:
        for d in out_tr:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        f.write((RES / "standin_traces.jsonl").read_text(encoding="utf-8"))
    with open(RES / "regrade_log.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["request_id", "qid", "old", "new", "answer"]); w.writeheader(); w.writerows(log)
    print(len(out_rows), "real rows +", len(standin), "stand-in rows;", len(log), "grades changed")
    for l in log:
        print(" ", l["request_id"], l["old"], "->", l["new"])


if __name__ == "__main__":
    main()
