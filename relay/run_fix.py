"""Re-run the quality evals that were broken by network drop-outs, plus the verifier v2 run.
  1) large-k2-norr on gpt-oss-20b, v1 sentence verifier (comparable with the other 3 settings)
  2) the 8 gpt-oss-120b questions that hit connection errors (v1 verifier)
  3) the full 25 questions on gpt-oss-120b with the v2 group verifier
About 25-30 minutes. Keep the laptop awake and online."""
import subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent
def run(*a):
    print("\n$", " ".join(a), flush=True); t = time.time()
    subprocess.run([sys.executable, "-m", *a], cwd=ROOT)
    print(f"({time.time()-t:.0f}s)", flush=True)
run("relay.run", "eval", "--source", "real", "--setting", "large-k2-norr", "--big", "openai/gpt-oss-20b", "--small", "openai/gpt-oss-20b", "--verify", "sentence", "--gap", "1")
run("relay.run", "eval", "--source", "real", "--setting", "mid-k4-rr", "--verify", "sentence", "--only", "a1,a2,a3,a4,a5,a6,m1,m2", "--gap", "1")
run("relay.run", "eval", "--source", "real", "--setting", "mid-k4-rr", "--verify", "group", "--gap", "1")
print("\nDone.")
