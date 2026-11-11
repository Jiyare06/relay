# The Relay

The Relay answers engineers' questions about three pinned arXiv papers, cites where each answer came from, and refuses when the papers don't say. It runs on Groq's free tier and stays within its limits under load.

## Running it

Install the two dependencies:

    pip install aiohttp pandas

The real-service steps need your key in `GROQ_API_KEY`:

    python run_real.py probe
    python -m relay.run eval --source real --setting mid-k4-rr --verify group
    python -m relay.run load --source real --scenario steady-real

The stand-in needs no key. Start it, then point a load scenario at it:

    python -m relay.standin --port 8787 --config calibration/standin.json
    python -m relay.run load --source standin --scenario burst --out standin.csv

To rebuild the submission's `results.csv` and every table in the report:

    python -m relay.finalize
    python -m relay.analyze results/results.csv

## What's where

`relay/pipeline.py` holds the five steps and the checker. `relay/llm.py` is the model client and the rate governor, built on refilling buckets. `relay/standin.py` is the stand-in service, `relay/retrieval.py` the BM25 search tool, and `relay/loadgen.py` the open-loop load generator. `questions.json` has the 25 test questions and their rubric. `data/` holds the text extracted from the pinned PDFs. `incoming/` keeps the raw real-service files exactly as they came off my laptop. `results/` has the final `results.csv`, the per-request traces and the tables.
