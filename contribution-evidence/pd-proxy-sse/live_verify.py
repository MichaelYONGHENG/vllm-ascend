"""Isolated main proxy handler against existing P/D; no server changes.
HTTP transport deliberately coalesces or splits genuine D bytes. Not a latency benchmark.
"""

import argparse
import asyncio
import datetime
import hashlib
import importlib.util
import json
import pathlib
import sys
import uuid

import httpx
from starlette.requests import Request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", type=pathlib.Path, required=True)
parser.add_argument("--candidate", type=pathlib.Path, required=True)
parser.add_argument("--prefill", nargs=4, required=True, metavar="HOST:PORT")
parser.add_argument("--decode", nargs=8, required=True, metavar="HOST:PORT")
parser.add_argument("--output-dir", type=pathlib.Path, required=True)
args = parser.parse_args()
ROOT = args.output_dir
ROOT.mkdir(parents=True, exist_ok=True)
if (ROOT / "live-results.json").exists():
    parser.error("Choose a new output directory to preserve previous evidence")
SOURCES = {"baseline": args.baseline, "candidate": args.candidate}
P = [(entry.rsplit(":", 1)[0], int(entry.rsplit(":", 1)[1])) for entry in args.prefill]
D = [(entry.rsplit(":", 1)[0], int(entry.rsplit(":", 1)[1])) for entry in args.decode]


def objects(raw, stream):
    if not stream:
        return [json.loads(raw)]
    return [
        json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"
    ]


class Bytes(httpx.AsyncByteStream):
    def __init__(self, raw, layout):
        self.raw, self.layout = raw, layout

    async def __aiter__(self):
        if self.layout == "coalesced":
            yield self.raw
        else:
            for i in range(0, len(self.raw), 17):
                yield self.raw[i : i + 17]


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, role, layout, record):
        self.inner = httpx.AsyncHTTPTransport()
        self.role, self.layout, self.record = role, layout, record

    async def handle_async_request(self, request):
        r = await self.inner.handle_async_request(request)
        raw = await r.aread()
        await r.aclose()
        if r.status_code != 200:
            raise RuntimeError(f"{self.role}: {r.status_code} {raw[:300]!r}")
        self.record[self.role] = raw
        return httpx.Response(
            r.status_code,
            headers={"content-type": r.headers.get("content-type", "application/json")},
            stream=Bytes(raw, self.layout if self.role == "D" else "coalesced"),
        )

    async def aclose(self):
        await self.inner.aclose()


async def main():
    rows = []
    run_id = uuid.uuid4().hex
    for arm in ["baseline", "candidate"]:
        spec = importlib.util.spec_from_file_location("proxy_" + arm, SOURCES[arm])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        idx = 0
        for endpoint in ["chat/completions", "completions"]:
            for mode in ["final", "continuous", "json"]:
                for layout in ["coalesced", "fragmented"]:
                    pi, di = idx % 4, idx % 8
                    idx += 1
                    scheduler = mod.SharedProxyScheduler([P[pi]], [D[di]])
                    runtime = mod.WorkerRuntime(scheduler)
                    mod.runtime = runtime
                    mod.global_args = argparse.Namespace(max_retries=1, retry_delay=0, enable_prefill_affinity=False)
                    record = {}
                    for role, addr in [(mod.ServerRole.PREFILL, P[pi]), (mod.ServerRole.DECODE, D[di])]:
                        name = "P" if role == mod.ServerRole.PREFILL else "D"
                        runtime._clients[role][mod.server_key(*addr)] = httpx.AsyncClient(
                            base_url=f"http://{addr[0]}:{addr[1]}/v1",
                            transport=RecordingTransport(name, layout, record),
                            timeout=90,
                            trust_env=False,
                        )
                    try:
                        for phase in ["cold", "warm"]:
                            salt = f"sse-pr-{run_id}-{arm}-{idx}"
                            prompt = (
                                "Reference text:\n"
                                + "".join(
                                    f"Record {j}: This is neutral reference text for a cache verification request.\n"
                                    for j in range(160)
                                )
                                + "\nReply only OK."
                            )
                            stream = mode != "json"
                            body = {
                                "model": "dsv41",
                                "cache_salt": salt,
                                "temperature": 0,
                                "max_tokens": 16,
                                "stream": stream,
                            }
                            if endpoint == "chat/completions":
                                body.update(
                                    messages=[{"role": "user", "content": prompt}],
                                    chat_template_kwargs={"thinking": False},
                                )
                            else:
                                body["prompt"] = prompt
                            if stream:
                                body["stream_options"] = {
                                    "include_usage": True,
                                    "continuous_usage_stats": mode == "continuous",
                                }
                            raw = json.dumps(body).encode()

                            async def receive(raw=raw):
                                return {"type": "http.request", "body": raw, "more_body": False}

                            req = Request(
                                {
                                    "type": "http",
                                    "method": "POST",
                                    "path": "/v1/" + endpoint,
                                    "headers": [],
                                    "query_string": b"",
                                },
                                receive,
                            )
                            result = await asyncio.wait_for(mod.handle_completions_impl("/" + endpoint, req), 120)
                            received = b"".join([chunk async for chunk in result.body_iterator])
                            parse_error = None
                            try:
                                actual = objects(received, stream)
                            except (ValueError, TypeError) as exc:
                                actual = []
                                parse_error = type(exc).__name__
                            original = objects(record["D"], stream)
                            pusage = json.loads(record["P"])["usage"]
                            cached = pusage["prompt_tokens_details"]["cached_tokens"]
                            expected = json.loads(json.dumps(original))
                            for o in expected:
                                if isinstance(o.get("usage"), dict):
                                    details = o["usage"].get("prompt_tokens_details") or {}
                                    details["cached_tokens"] = cached
                                    o["usage"]["prompt_tokens_details"] = details
                            usages = [o["usage"] for o in actual if isinstance(o.get("usage"), dict)]
                            counts = [(u.get("prompt_tokens_details") or {}).get("cached_tokens") for u in usages]
                            row = {
                                "parse_error": parse_error,
                                "arm": arm,
                                "endpoint": endpoint,
                                "mode": mode,
                                "layout": layout,
                                "phase": phase,
                                "prefill": f"P{pi}",
                                "decode": f"D{di}",
                                "prompt_tokens": pusage["prompt_tokens"],
                                "prefill_cached_tokens": cached,
                                "usage_frames": len(usages),
                                "reported_cached_tokens": counts,
                                "all_frames_match_prefill": bool(usages) and all(c == cached for c in counts),
                                "body_matches_except_cached_tokens": actual == expected,
                                "done": not stream or received.rstrip().endswith(b"[DONE]"),
                                "scheduler_drained": scheduler.request_num == 0,
                            }
                            rows.append(row)
                            (ROOT / "live-results.json").write_text(json.dumps(rows, indent=2))
                            print(json.dumps(row), flush=True)
                            if arm == "candidate":
                                assert all(
                                    row[k]
                                    for k in [
                                        "all_frames_match_prefill",
                                        "body_matches_except_cached_tokens",
                                        "done",
                                        "scheduler_drained",
                                    ]
                                ), row
                            assert cached == 0 if phase == "cold" else cached > 0, (phase, cached)
                    finally:
                        await runtime.close()
    print("COMPLETE", len(rows), flush=True)
    (ROOT / "live-meta.json").write_text(
        json.dumps(
            {
                "utc_finished": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source_sha256": {
                    a: hashlib.sha256(SOURCES[a].read_bytes()).hexdigest() for a in ["baseline", "candidate"]
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
