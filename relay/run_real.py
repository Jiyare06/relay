"""Run every step that needs the real Groq API, in order, on YOUR computer.

  python run_real.py            # everything (about 60-90 min, mostly waiting on rate limits)
  python run_real.py probe      # only the measurements
  python run_real.py eval       # only the quality / retrieval runs
  python run_real.py steady     # only the steady real load run

Needs GROQ_API_KEY in the environment or in a .env file next to this script.
Request budget (approx): probe ~170 requests, eval ~230, steady ~15 answers.
Everything is appended to results/ ; zip that folder and send it back.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
BIG, SMALL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"


def run(*args):
    cmd = [PY, "-m", *args]
    print("\n$", " ".join(cmd), flush=True)
    t = time.time()
    r = subprocess.run(cmd, cwd=ROOT)
    print(f"(exit {r.returncode}, {time.time() - t:.0f}s)", flush=True)


def probe():
    run("relay.probe", "models")
    for m in (SMALL, BIG):
        run("relay.probe", "latency", "--model", m, "--n", "25", "--prompt", "small", "--gap", "2.2")
        run("relay.probe", "latency", "--model", m, "--n", "25", "--prompt", "rag", "--max-tokens", "400", "--gap", "6")
    print("waiting 65 s so the per-minute windows are empty before limit tests", flush=True)
    time.sleep(65)
    run("relay.probe", "rpm", "--model", SMALL, "--n", "40")          # 40 tiny requests at once
    time.sleep(65)
    run("relay.probe", "tpm", "--model", SMALL, "--n", "12")          # ~1.2k-token prompts back to back
    time.sleep(65)
    run("relay.probe", "concurrency", "--model", BIG, "--n", "12")
    run("relay.calibrate")


def evaluate():
    # retrieval comparison on the small model (its daily token budget can afford 4 settings)
    for s in ("small-k8-rr", "mid-k4-rr", "mid-k4-norr", "large-k2-norr"):
        run("relay.run", "eval", "--source", "real", "--setting", s, "--big", SMALL, "--small", SMALL, "--gap", "1")
    # the production setting on the big model (its daily token budget is the scarce one)
    run("relay.run", "eval", "--source", "real", "--setting", "mid-k4-rr", "--gap", "1")


def steady():
    run("relay.run", "load", "--source", "real", "--scenario", "steady-real")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "probe"):
        probe()
    if which in ("all", "eval"):
        evaluate()
    if which in ("all", "steady"):
        steady()
    print("\nDone. Send back the whole results/ folder (and calibration/).")
