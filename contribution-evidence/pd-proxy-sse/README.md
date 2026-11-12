# PD proxy response framing and cache usage evidence

Validated on 2026-11-12 against Ascend main
`a4971912ceaf3e775425a793369de32a93aa13ba`. This is a proxy correctness change,
not an inference performance claim.

## Current-main regression

Run the PR's proxy tests with Python 3.12, FastAPI 0.123.10, Starlette 0.50.0,
HTTPX 0.28.1, Uvicorn 0.54.0 and pytest 9.2.0:

```sh
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export NO_PROXY='*' no_proxy='*'
python -m pytest --noconftest tests/ut/proxy -q
```

- Candidate: **66 passed** (35 existing cases, 24 new real-loopback HTTP cases,
  seven deterministic framing cases).
- Replace only the proxy source with the pinned main version and run the three
  pre-existing test modules, retaining the newly added HTTP cases: **20 failed,
  39 passed**. All failures are new boundary/usage assertions; the 35 existing
  cases pass. The four passing new cases are complete non-streaming JSON.
- Framing coverage includes all two-piece splits, bytewise UTF-8, LF/CRLF/CR,
  multiline data, event headers, comments, DONE, incomplete tails, cancellation,
  upstream errors, and yielding a complete event before requesting the next one.
- Existing HTTP status/error, scheduling cleanup, recomputation and affinity
  tests remain intact. No test assertion was weakened.
- `bash format.sh ci`, Ruff, mypy and `git diff --check` passed locally.

Initial full-suite attempts failed the existing connection-refused assertion
because macOS system proxy discovery routed loopback requests through a proxy
and returned an empty HTTP 502. Unsetting proxy environment variables alone did
not disable system discovery. Setting `NO_PROXY=*` restored direct connections
and the existing test passed on both sources. An initial formatting run failed
while downloading a Go toolchain; the retry completed all hooks successfully.

## Real-backend controlled transport A/B

The unmodified running deployment supplied four Prefill and eight Decode
endpoints across four eight-card Ascend 910B3 64 GiB servers, serving
DeepSeek-V4.1-Flash-1017 W8A8. The backends retain the deployment's v0.30 engine
and fixes; they were not upgraded to main. The proxy source under test is main
plus this PR, imported into an isolated process on the client machine.

For each source: two APIs (Chat Completions and Completions), three response
modes (final usage, continuous usage, non-streaming JSON), two transport layouts
(coalesced body and 17-byte fragments), and cold/warm pairs: **24 requests**.
Both arms cover all four P and eight D endpoints. Each pair uses a fresh cache
salt, identical synthetic reference text and the same P/D endpoints. Sampling
is temperature 0, max_tokens 16; Chat uses `thinking: false`. Prompts contain
2,411 Chat tokens or 2,407 Completions tokens. P reports zero cold hits and
2,304 warm hits.

The recording HTTP transport sends real requests directly to the existing
backends and captures each request's P usage. It buffers the genuine D bytes,
then deliberately changes only their chunk boundaries before passing them to
the actual proxy handler. No backend response JSON values are synthesized or
changed. The output is compared with that same D response, allowing only the
cached-token field to change to the P value. The independent transport harness
therefore **cannot measure TTFT, throughput or streaming latency**.

| Response mode | Requests per arm | Main: all usage correct | Candidate: all usage correct |
| --- | ---: | ---: | ---: |
| Final streaming usage | 8 | 0/8 | 8/8 |
| Continuous streaming usage | 8 | 0/8 | 8/8 |
| Non-streaming JSON | 8 | 4/8 | 8/8 |
| Total | 24 | 4/24 | 24/24 |

Candidate: **40/40 usage frames** equal their own request's P count. Both APIs
preserve all other response JSON fields; streaming requests preserve DONE;
local scheduling counters drain. Main produced two truncated, invalid JSON
responses on fragmented non-streaming Completions, in addition to missed cache
rewrites. Main exposes 38 parseable usage frames, only four correctly rewritten.
These deliberately adversarial transport layouts are regression evidence, not
an estimate of failure frequency on ordinary networks.

`results.json` contains per-request assertions and token counts. Endpoint names
are aliases; no server addresses, credentials or business prompts are included.
`environment.json` records tested source hashes. `health-after.json` confirms
12 backend HTTP 200 checks and the original proxy's 4P/8D healthy response.
No deployed process, image, configuration or port was changed.

The initial harness was corrected to handle null usage details and to record
malformed baseline responses rather than aborting. Completed baseline pairs
were retained; the final pair was retried with a fresh salt. The candidate arm
completed all 24 cases without an interrupted run. Failed setup attempts are
not counted as candidate passes.

### Reproduce with an existing deployment

Extract baseline and candidate proxy scripts from their pinned Git commits,
then run the included harness. Supply your own endpoint addresses; it does not
start or reconfigure inference servers. The script refuses to overwrite results.

```sh
python live_verify.py \
  --baseline /path/to/main_proxy.py --candidate /path/to/pr_proxy.py \
  --prefill P0:7100 P1:7101 P2:7100 P3:7101 \
  --decode D0:7100 D1:7101 D2:7102 D3:7103 D4:7100 D5:7101 D6:7102 D7:7103 \
  --output-dir /path/to/new-results
```

Backends must expose the existing PD protocol and real P prompt-token details.
For CPU-only reproduction, use the PR's committed HTTP regression tests.

## Scope and remaining validation

Missing-P-detail fallback semantics are unchanged. The current-main proxy still
uses the existing zero fallback. Messages/Responses protocol support is outside
this PR. The NPU results above validate compatibility with the deployed engine,
not a new main-engine NPU deployment or an official CI run. No sustained-load,
multi-worker, broad model or performance certification is claimed.
