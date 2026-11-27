## AgentFlight recorder overhead

- Machine: Linux 6.12.113+ (x86_64), Intel(R) Xeon(R) Processor, 8 CPUs, VM (KVM)
- Python: CPython 3.13.5
- Packages: agentflight 0.1.0, opentelemetry-sdk 1.45.2, httpx 0.29.0, fastapi 0.165.0, uvicorn 0.54.0
- Load average (1 min) at the start of each repeat: 0.81 to 1.24
- 5 repeat(s), each on a fresh server. Per repeat and scenario: 2000 timed runs after 200 warm-up runs (200 for batch=False); 11 spans per run (5 model calls, 5 tool calls)
- CPUs: benchmark pinned to [0], server to [1, 2, 3, 4, 5, 6, 7]

| Payload | Scenario | Median per run | Spread of medians | p95 per run | Added per span | Spans stored | Dropped (SDK count / on runs) | Runs closed |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| small (~100 chars) | baseline | 0.7 µs | 0.7 to 0.8 µs | 0.8 µs | — | — | — | — |
| small (~100 chars) | sdk, no exporter | 383.3 µs | 381.3 to 389.8 µs | 458.5 µs | 34.8 µs | — | — | — |
| small (~100 chars) | sdk, serialize only | 532.0 µs | 528.4 to 535.9 µs | 623.2 µs | 48.3 µs | — | — | — |
| small (~100 chars) | sdk, HTTP, batch=True, queue drained every 100 runs | 421.4 µs | 413.0 to 457.3 µs | 784.3 µs | 38.2 µs | 121,000 of 121,000 | 0 / 0 | 11,000 of 11,000 |
| small (~100 chars) | sdk, HTTP, batch=True, unpaced | 420.0 µs | 416.4 to 447.4 µs | 533.9 µs | 38.1 µs | 55,718 of 121,000 | 65,282 / 65,282 | 11,000 of 11,000 |
| small (~100 chars) | sdk, HTTP, batch=False | 64,347.6 µs | 59,479.7 to 65,389.2 µs | 71,882.5 µs | 5,849.7 µs | 12,100 of 12,100 | 0 / 0 | 1,100 of 1,100 |
| large (8,000 chars) | baseline | 1.5 µs | 1.4 to 1.6 µs | 1.5 µs | — | — | — | — |
| large (8,000 chars) | sdk, no exporter | 833.4 µs | 826.0 to 836.4 µs | 946.6 µs | 75.6 µs | — | — | — |
| large (8,000 chars) | sdk, serialize only | 1,235.4 µs | 1,228.5 to 1,252.0 µs | 1,397.1 µs | 112.2 µs | — | — | — |
| large (8,000 chars) | sdk, HTTP, batch=True, queue drained every 100 runs | 875.7 µs | 865.9 to 878.7 µs | 1,640.2 µs | 79.5 µs | 121,000 of 121,000 | 0 / 0 | 11,000 of 11,000 |
| large (8,000 chars) | sdk, HTTP, batch=True, unpaced | 864.9 µs | 861.3 to 928.2 µs | 1,127.0 µs | 78.5 µs | 60,018 of 121,000 | 60,982 / 60,982 | 11,000 of 11,000 |
| large (8,000 chars) | sdk, HTTP, batch=False | 70,389.9 µs | 66,609.9 to 72,645.4 µs | 80,479.0 µs | 6,398.9 µs | 12,100 of 12,100 | 0 / 0 | 1,100 of 1,100 |

### Server ingest rate

| Payload | Ingest, spans/s (median) | Spread | Unpaced loop: spans made/s | Unpaced loop: spans stored/s |
| --- | ---: | ---: | ---: | ---: |
| small (~100 chars) | 9,604 | 9,211 to 9,773 | 26,189 | 7,162 |
| large (8,000 chars) | 4,248 | 3,735 to 4,362 | 12,718 | 3,371 |

(Ingest: 20 batches of 512 spans per payload and repeat, sent back to back from one thread with nothing else running in the process. Unpaced loop: spans made per second on the agent thread (median run time), and spans the server stored per second of the scenario's wall time, while the exporter thread shared the benchmark's one CPU with the agent.)
