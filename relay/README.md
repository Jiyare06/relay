# The Relay

A cited question-answering pipeline over three arXiv papers, built to stay honest under Groq free-tier limits.

    pip install aiohttp pandas
    # real service (needs GROQ_API_KEY in the environment)
    python run_real.py probe          # measure limits/latency -> results/probe_raw.jsonl, calibration/
    python -m relay.run eval --source real --setting mid-k4-rr --verify group
    python -m relay.run load --source real --scenario steady-real
    # stand-in (no key needed)
    python -m relay.standin --port 8787 --config calibration/standin.json &
    python -m relay.run load --source standin --scenario burst --out standin.csv
    # rebuild the submission's results.csv and every table
    python -m relay.finalize && python -m relay.analyze results/results.csv

Layout: `relay/pipeline.py` (5 steps, verifier), `relay/llm.py` (client + refilling-bucket governor),
`relay/standin.py` (stand-in service), `relay/retrieval.py` (BM25 tool), `relay/loadgen.py` (open-loop
load), `questions.json` (25 questions + rubric), `data/` (pdftotext of the pinned PDFs),
`incoming/` (untouched raw real-service files), `results/` (final results.csv, traces, tables).
