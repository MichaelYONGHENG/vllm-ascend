# DeepSeek V4.1 Flash P/D contribution evidence

Measurements: 2026-10-30 (bounded reads), 2026-10-31 (failure reporting).
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
The contributions port the tested algorithms to main; current-main CPU tests
are reported separately in each PR. The driver/library combination is an
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
