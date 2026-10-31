# DeepSeek V4.1 Flash P/D contribution evidence

Measurements: 2026-10-30 (0.30 bounded reads), 2026-10-31 (failure reporting and current-main integration).
This branch contains supporting evidence, not a proposed upstream code change.
Only aggregate measurements, public dataset identifiers and synthetic tool
schemas are included. Host addresses, credentials, production configurations
and raw request/response logs are excluded.

## Hardware and software

Four ARM64 Ubuntu 22.04 servers, each with eight Ascend 910B3 64 GB NPUs and
200 Gb/s NPU network ports. Prefill DP4/TP4 across two servers; Decode DP8/TP2
across two servers; four P endpoints and eight D endpoints behind one proxy.
Driver 25.5.1, firmware 7.8.0.6.201, CANN 9.2.1, torch 2.10.0+cpu,
torch_npu 2.10.0.post7, Mooncake package metadata 0.3.13.

Hardware runs used vllm-ascend 0.30.0 at
`c09d3e20b9c45c80b34ae3781440d90904a44023` and vLLM 0.30.0+empty at
`ced6857afa0ea7b2e3f0846a62e1394e90f15607`, with deployment patches.
These are **not hardware benchmark results for current upstream main**.
The contributions port the tested algorithms to main; current-main NPU/CPU validation is reported separately below and in each PR. The driver/library combination is an
observed working setup, not a vendor compatibility certification.

## Fixed Agent replay

Source: [SWE-smith trajectories](https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories),
MIT dataset metadata, revision `08e109b4a59eaeebf80e4675cd125d42e7ac99a4`,
`tool` split. The manifest fixes 64 selected trajectories and their order.
Each has eight replay turns. Tool results are replayed as user messages;
newly generated assistant responses enter subsequent context. Tools are not
executed. This is a serving workload, not a SWE task success-rate evaluation.

Reconstruct with Python and `datasets` installed:

```bash
python prepare_dataset.py --output agentic_dataset.json
sha256sum agentic_dataset.json
# 79802869413e9aa1cef04249f3bc7e2a265ebda539c3298ff17d102566e6b7d1
```

The preparation function was run against the original cached source rows and
reproduced that SHA256 exactly. The remote streaming download path uses the
pinned revision and checks the same hash; it was not separately downloaded
during PR preparation.

EvalScope 1.14.1 command, run once per configuration/concurrency with a fresh
cache salt (replace the endpoint and served model name):

```bash
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
extra=$(python -c 'import json,uuid; print(json.dumps({"cache_salt":uuid.uuid4().hex,"tools":json.load(open("tools.json")),"tool_choice":"auto"}))')
evalscope perf --model dsv41 --url http://localhost:8000/v1/chat/completions \
  --api openai --parallel 64 --number 64 --multi-turn \
  --dataset swe_smith --dataset-path agentic_dataset.json \
  --min-turns 8 --max-turns 8 --stream --max-tokens 512 \
  --temperature 0 --seed 42 --extra-args "$extra" \
  --no-test-connection --warmup-num 0 --num-workers 1 \
  --log-every-n-query 64 --connect-timeout 20 --read-timeout 180 \
  --total-timeout 300 --outputs-dir results --name candidate64 --no-timestamp
```

Warm up the servers before the measured round. Change `--parallel` to 32 for
the other concurrency. The original harness also added unique conversation
request IDs for log correlation; they do not change prompt contents.

Metrics use total completion tokens (reasoning counted once) / wall time.
TTFT is time to the first nonempty content, reasoning or tool-call delta.
Per-request TPOT is `(total_time - TTFT) / (completion_tokens - 1)`.
The original EvalScope installation counted these output fields when measuring
visible SSE gaps; stock client versions may use different first-output rules.
Prefix hit rates are deltas of Prefill Prometheus hit/query token counters,
not EvalScope's missing-usage default of zero. KV durations include FIFO wait
and synchronous transfer, not just wire time.

## Bounded-read results and limitations

`kv-batching-results.json` preserves the valid per-round aggregate results.
All valid rounds completed 512 requests and 1024 TP-level KV reads. The
comparison used the same least-load test proxy and backend settings; sticky
session routing was not a variable. Only D-side synchronous KV reads changed.
QoS, collective communication, PFC/ECN, drivers and switch settings stayed
unchanged. FIFO admission and byte batching were tested together, not isolated
as independent causal factors.

There is one original baseline round per concurrency and two 256 KiB rounds.
Combined throughput is total tokens / total duration, not the mean of rates.
32 concurrency: 217.74 to 593.20 tok/s; 64: 254.96 to 729.35 tok/s.
The repeated candidate runs had zero new receive-full drops, RoCE retries and
KV calls over one second in those measured windows. This does not establish
permanent absence of loss, statistical significance or hardware health.
The 32-concurrency repeat still had 4/512 requests with TPOT over 50 ms.

An additional 25 Gb/s pacing experiment did not consistently help and is not
part of the proposed implementation. A switch-back round with two failed
KV reads and an aborted 64-concurrency round are excluded from performance
ratios; HTTP success alone was insufficient to validate those requests.
No multi-hour soak or other model/topology validation is claimed.

## Failure reporting

`kv-failure-results.json` records four real P-to-D metadata-failure cases:
both Decode servers, streaming and non-streaming. The test relay changed only
the P engine identity in real KV metadata for tagged requests; addresses and
block mappings stayed valid, so D entered the real receive path and failed
metadata lookup. All four returned HTTP 500, all four follow-up requests
answered `2+2` correctly, and all 12 backends remained healthy.

This deployment also needed a streaming API fix and proxy error handling:
with connector propagation alone, the old API could still emit an empty 200
stream. Current vLLM main already checks terminal errors before empty-output
filtering. The connector PR does not claim to independently fix an old API or
to guarantee a 500 status after streaming has already started.

No physical network outage or online `recompute` recovery was injected.
The main-port unit tests cover completion ordering, a concurrently active
writer, request isolation, empty-block failures and producer compatibility.

## Current-main NPU integration (2026-10-31)

This section is separate from the 0.30 backport measurements above.
`main-environment.json` pins Ascend main, its verified vLLM main, both revised
PR heads, the rebuilt image and dependencies. Main was built from source on
an official nightly ARM64 base; the base image's default release vLLM was
replaced, so its tag alone is not used as proof of a main build.

The same four-server topology ran the full DeepSeek-V4.1-Flash-1017 W8A8
checkpoint. All arms held the producer connector at the combined PR source,
with the same session-affinity proxy. Only the consumer connector and bounded
read enablement changed:

- **raw:** exact main consumer, no bounded reads or failure patch.
- **batch:** #20491 consumer, 256 KiB / 128 descriptors, no failure patch.
- **combined:** #20491 and #20488 consumer, same bounded-read settings.

Thus raw is a **consumer-main baseline**, not four completely unmodified
main servers. PR #20488 is tested for correctness and lack of ordinary-load
regression, not claimed to accelerate successful transfers. The same public
64-session/eight-turn replay and metric definitions above apply. Each measured
round gets a fresh cache salt. An entire 32-concurrency round warms each arm
before measurement; warmup records are retained but excluded from ratios.
Runtime connector hashes and settings are checked before/after each round,
and client success/token totals are reconciled with all 12 backend counters.

### Environment limitations held constant

This main revision rejects the legacy `enable_dsa_cp` configuration, so it is
omitted in every arm. MRv1, Prefill batch budget 4096, P DP4/TP4 and D DP8/TP2 remain fixed, with
no Prefill MC2. The old local CP-metadata and parser backports were not copied
into the refactored main runtime. Consequently these results are not a causal
comparison of main versus the historical stable 0.30 deployment.

Actual loading exposed a nightly CANN 9.2.1 operator-metadata incompatibility:
GE could not parse `hifloat4_scale` in `MoeDistributeDispatchV2`, causing an
INT8 ND-to-NZ TransData failure before model serving. A strictly pinned,
candidate-only read-only metadata overlay removed the three unsupported
signatures (indices 26–28), retaining the other 26 and all binary kernels.
Original/overlay SHA256 values are in `main-environment.json`. Explicitly
setting `torch.npu.config.allow_internal_format=True`, converting an
INT8[1280,16384] tensor to format 29 and back, checking its actual format and
exact equality, and running FP16 matmul failed on raw metadata and passed with
the overlay. This is a limited W8A8 workaround, not part of either PR, not a
vendor compatibility certification, and not support for those new datatypes.

`cann_metadata_compat.py` generates a separate overlay from the exact pinned
metadata file, verifies both hashes, and refuses in-place writes or an existing
output. `probe_npu.py` is the real-NPU repro used before and after mounting that
overlay read-only at the CANN operator-metadata path. The vendor JSON itself
is not distributed here. Formatting changed neither script’s Python AST.

NumPy 1.26.4 matches this Triton/mindstudio environment but conflicts with
OpenCV 5's declared NumPy>=2 dependency. A basic cv2 import/conversion passed;
this acceptance is for text serving, not full vision-stack validation.
No driver, firmware, network or switch changes were made.

### Current-main measured results

| Arm | Concurrency | tok/s | TTFT P99 s | TPOT mean / P99 ms | KV request-max P99 ms | Drop / retry |
| --- | --- | --- | --- | --- | --- | --- |
| raw | 32 | 266.08 | 19.204 | 34.64 / 157.63 | 8606.52 | 22786 / 1867 |
| raw | 64 | 345.19 | 25.492 | 70.39 / 207.60 | 12891.11 | 26293 / 2293 |
| batch | 32 | 664.52 | 9.698 | 14.36 / 19.29 | 172.26 | 0 / 0 |
| batch | 64 | 975.51 | 8.345 | 15.18 / 20.73 | 153.72 | 0 / 0 |
| combined (run 1) | 32 | 678.09 | 7.958 | 14.50 / 19.73 | 152.87 | 0 / 0 |
| combined (run 1) | 64 | 888.92 | 11.015 | 17.35 / 52.70 | 130.36 | 0 / 0 |
| combined (run 2) | 64 | 1073.66 | 6.602 | 15.23 / 22.09 | 182.23 | 0 / 0 |
| combined (run 3) | 64 | 1051.65 | 6.871 | 15.12 / 20.61 | 174.47 | 0 / 0 |

Eight formal rounds completed **4096/4096 requests**, with **8192 successful TP-level KV reads** and no receive errors. Four full warmup rounds are recorded separately and excluded. Relative to one consumer-main baseline round per concurrency, the standalone bounded-read PR improved throughput by **149.7% at 32** and **182.6% at 64**. Cache hit rates stayed approximately 80.9% in all arms. There is one raw and one batch round per concurrency, one combined 32 round, and three combined 64 rounds; no statistical-significance claim.

All measured bounded-read windows had zero new receive-full drops, RoCE retries and KV calls over one second. Raw 32/raw 64 had 132/191 of 1024 KV reads over one second. KV P99 in the table is the maximum TP-read duration per request, then the percentile across 512 requests; it includes admission wait, not pure wire time. Drop/retry deltas sum all 32 NPU ports.

Combined 64 throughput varied from 888.92 to 1073.66 tok/s; all three rounds pooled by tokens/time yield **997.20tok/s**. The first round had 10 requests with TPOT >50 ms, all in conversation turn 1; both repeats had none. Prefill timing also varied while its source/config remained fixed. These data do not isolate the cause of the first-round slowdown or establish a fixed overhead from the failure patch. The slower round remains included. Residual Prefill and visible-stream tails remain; there is no claim that every latency target is solved.

All four current-main functional suites passed 12/12 cases. Every formal replay's normally completed tool arguments parsed as JSON; incomplete arguments only occurred at the configured 512-token output cap and are counted separately. This establishes protocol/function checks, not correctness of generated code or SWE task success. Per-endpoint counts confirm all 4 Prefill/all 8 Decode endpoints served requests; all 64 sessions retained Prefill affinity. No preemption/recompute was observed in these rounds.

### Request-failure evidence

`main-kv-failure-results.json` records four real metadata failures on this
main stack: each Decode server, streaming and non-streaming. All returned
structured HTTP500 within 0.543–0.840s, all four immediate normal controls
answered correctly, and all 12 backend health endpoints passed. Matching
request IDs and `KV_RECV_FAILED` were verified on the same log lines.
The observed device path is the non-group read implementation inside
`MooncakeHybridConnector`; both internal transfer branches have CPU regression
coverage, but the grouped-cache branch is not claimed as NPU-covered here.
Physical link failure, mid-read corruption and online recompute were not
injected. Temporary fault relays were stopped before formal measurements.

### Review and CI

The review fixes add successful empty-block cleanup and registration before
queue publication (#20488), and token-specific FIFO cleanup on interrupted
waiters (#20491). Revised CPU suites pass: 22 tests/40 subtests and 37 tests/36
subtests respectively; the combined suite passes 42 tests/40 subtests.
The old empty-block/lifecycle implementation fails the added regression;
the old interrupted-waiter implementation deadlocks under the event-ordered
regression. RuntimeError and KeyboardInterrupt are covered.

Official pre-commit/mypy, documentation links and CPU lanes for the recorded
main and v0.32 pass on these heads. The overall E2E gate still requires a
maintainer to select exactly one device-test label (`ready-precise`,
`ready-all` or `main2main`). Local hardware evidence does not imply official
NPU CI has run or that all repository gates are green.
