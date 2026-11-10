## Table 2a
| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| steady-real | real | 11 | 299 | 2.3 | 1944 / 13583 / 13583 | 1944 / 13583 / 13583 |
| steady | standin | 27 | 234 | 7.6 | 730 / 7591 / 9517 | 744 / 9534 / 20836 |
| burst | standin | 66 | 214 | 19.7 | 457 / 10300 / 12628 | 487 / 10313 / 12645 |
| daily-small40left | standin | 15 | 119 | 8.2 | 506 / 8797 / 8797 | 536 / 8846 / 8846 |
| overload | standin | 145 | 317 | 29.4 | 357 / 11690 / 14390 | 408 / 11713 / 14397 |
| halved-static | standin | 29 | 256 | 7.4 | 632 / 3819 / 11566 | 652 / 3826 / 11585 |
| burst-nocache | standin | 62 | 249 | 16.0 | 1001 / 9991 / 15049 | 1016 / 10027 / 15060 |
| overload-naive | standin | 171 | 315 | 34.6 | 0 / 10298 / 12937 | 0 / 10311 / 12957 |
| halved | standin | 29 | 256 | 7.4 | 688 / 7580 / 10045 | 709 / 7600 / 10058 |
| daily | standin | 15 | 119 | 8.2 | 622 / 8837 / 8837 | 644 / 8887 / 8887 |
| halved-static-naive | standin | 19 | 215 | 5.9 | 697 / 20181 / 20181 | 704 / 20219 / 20219 |

## Table 2b
| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
|---|---|---|---|---|---|---|---|
| steady-real | 100.0 | 0.0 | 0.0 | 0.36 | 1 | 2424 | $0.43 |
| steady | 96.3 | 3.7 | 0.0 | 0.15 | 1 | 1553 | $0.28 |
| burst | 59.1 | 34.8 | 6.1 | 0.03 | 3 | 933 | $0.16 |
| daily-small40left | 93.3 | 6.7 | 0.0 | 0.13 | 1 | 1035 | $0.12 |
| overload | 79.3 | 13.1 | 7.6 | 0.01 | 3 | 788 | $0.11 |
| halved-static | 69.0 | 0.0 | 31.0 | 0.34 | 2 | 1147 | $0.17 |
| burst-nocache | 38.7 | 50.0 | 11.3 | 0.06 | 3 | 1664 | $0.26 |
| overload-naive | 71.3 | 12.3 | 16.4 | 0.02 | 3 | 677 | $0.10 |
| halved | 79.3 | 13.8 | 6.9 | 0.14 | 2 | 1201 | $0.17 |
| daily | 46.7 | 53.3 | 0.0 | 0.13 | 1 | 969 | $0.13 |
| halved-static-naive | 73.7 | 5.3 | 21.1 | 0.53 | 1 | 1553 | $0.24 |

## Table 3
| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| quality-small-k8-rr-sentence-20b (real) | 25 | 52.0 | 4.0 | 20.0 | 2107 | 1150 / 1912 | 1150 / 2144 |
| quality-mid-k4-rr-sentence-20b (real) | 25 | 60.0 | 0.0 | 20.0 | 2198 | 1342 / 3174 | 1342 / 3174 |
| quality-mid-k4-norr-sentence-20b (real) | 25 | 64.0 | 4.0 | 20.0 | 2041 | 1467 / 3343 | 1487 / 3343 |
| quality-mid-k4-rr-sentence-120b (real) | 25 | 40.0 | 0.0 | 20.0 | 2668 | 1651 / 12862 | 1707 / 12862 |
| quality-large-k2-norr-sentence-20b (real) | 25 | 60.0 | 4.0 | 20.0 | 1854 | 1181 / 7156 | 1183 / 7156 |
| quality-mid-k4-rr-group-120b (real) | 25 | 44.0 | 0.0 | 20.0 | 2355 | 1545 / 12786 | 1616 / 12786 |
| quality-mid-k4-rr-group-120b_citefix (real) | 25 | 60.0 | 0.0 | 20.0 | 1997 | 1335 / 3082 | 1335 / 3158 |

Refused wrongly % per setting:
- quality-large-k2-norr-sentence-20b: 16.0% (n=25), errors/non-ok: 0
- quality-mid-k4-norr-sentence-20b: 12.0% (n=25), errors/non-ok: 0
- quality-mid-k4-rr-group-120b: 36.0% (n=25), errors/non-ok: 0
- quality-mid-k4-rr-group-120b_citefix: 20.0% (n=25), errors/non-ok: 0
- quality-mid-k4-rr-sentence-120b: 40.0% (n=25), errors/non-ok: 0
- quality-mid-k4-rr-sentence-20b: 20.0% (n=25), errors/non-ok: 0
- quality-small-k8-rr-sentence-20b: 24.0% (n=25), errors/non-ok: 0

Excluded from tables (laptop lost network; HTTP status 0, request never reached Groq): quality-large-k2-norr-20b-netfail (25 rows: {'error': 20, 'ok': 4, 'rate_limited': 1}), quality-mid-k4-rr-120b-netfail (8 rows: {'error': 8})