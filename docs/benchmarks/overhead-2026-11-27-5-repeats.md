## AgentFlight recorder overhead

- Machine: Linux 6.12.113+ (x86_64), Intel(R) Xeon(R) Processor, 8 CPUs, VM (KVM)
- Python: CPython 3.13.5
- Packages: agentflight 0.1.0.dev0, opentelemetry-sdk 1.45.2, httpx 0.29.0, fastapi 0.165.0, uvicorn 0.54.0
- Load average (1 min) at the start of each repeat: 0.57 to 2.28
- 5 repeat(s), each on a fresh server. Per repeat and scenario: 2000 timed runs after 200 warm-up runs (200 for batch=False); 11 spans per run (5 model calls, 5 tool calls)
- CPUs: benchmark pinned to [0], server to [1, 2, 3, 4, 5, 6, 7]

| Payload | Scenario | Median per run | Spread of medians | p95 per run | Added per span | Spans stored | Dropped (SDK count / on runs) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| small (~100 chars) | baseline | 0.7 µs | 0.7 to 0.8 µs | 0.7 µs | — | — | — |
| small (~100 chars) | sdk, no exporter | 380.3 µs | 373.4 to 384.0 µs | 390.3 µs | 34.5 µs | — | — |
| small (~100 chars) | sdk, serialize only | 524.6 µs | 518.0 to 533.4 µs | 539.4 µs | 47.6 µs | — | — |
| small (~100 chars) | sdk, HTTP, batch=True, queue drained every 100 runs | 409.8 µs | 403.8 to 417.3 µs | 542.5 µs | 37.2 µs | 121,000 of 121,000 | 0 / 0 |
| small (~100 chars) | sdk, HTTP, batch=True, unpaced | 406.0 µs | 402.5 to 407.4 µs | 480.4 µs | 36.8 µs | 59,828 of 121,000 | 61,172 / 41,966 |
| small (~100 chars) | sdk, HTTP, batch=False | 60,948.5 µs | 57,785.9 to 62,717.0 µs | 68,115.0 µs | 5,540.7 µs | 12,100 of 12,100 | 0 / 0 |
| large (8,000 chars) | baseline | 1.5 µs | 1.4 to 1.5 µs | 1.5 µs | — | — | — |
| large (8,000 chars) | sdk, no exporter | 825.1 µs | 815.3 to 825.3 µs | 838.1 µs | 74.9 µs | — | — |
| large (8,000 chars) | sdk, serialize only | 1,217.8 µs | 1,215.6 to 1,222.0 µs | 1,237.4 µs | 110.6 µs | — | — |
| large (8,000 chars) | sdk, HTTP, batch=True, queue drained every 100 runs | 853.2 µs | 851.1 to 862.5 µs | 1,009.1 µs | 77.4 µs | 121,000 of 121,000 | 0 / 0 |
| large (8,000 chars) | sdk, HTTP, batch=True, unpaced | 850.1 µs | 847.4 to 875.6 µs | 1,003.0 µs | 77.1 µs | 58,118 of 121,000 | 62,882 / 41,080 |
| large (8,000 chars) | sdk, HTTP, batch=False | 69,894.8 µs | 68,430.0 to 71,938.2 µs | 76,906.6 µs | 6,353.9 µs | 12,100 of 12,100 | 0 / 0 |

### Server ingest rate

| Payload | Ingest, spans/s (median) | Spread | Unpaced loop: spans made/s | Unpaced loop: spans stored/s |
| --- | ---: | ---: | ---: | ---: |
| small (~100 chars) | 10,356 | 10,076 to 10,441 | 27,092 | 8,648 |
| large (8,000 chars) | 4,397 | 4,024 to 4,474 | 12,940 | 3,857 |

(Ingest: 20 batches of 512 spans per payload and repeat, sent back to back from one thread with nothing else running in the process. Unpaced loop: spans made per second on the agent thread (median run time), and spans the server stored per second of the scenario's wall time, while the exporter thread shared the benchmark's one CPU with the agent.)
