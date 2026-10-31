"""Create a separate A2 metadata overlay for the pinned nightly, never edit CANN in place."""

import hashlib
import json
import pathlib
import sys

source, target = map(pathlib.Path, sys.argv[1:])
assert source.resolve() != target.resolve()
assert not target.exists(), target
assert (
    hashlib.sha256(source.read_bytes()).hexdigest()
    == "2fe2429180a16b3a599175b60cc03241f4432755cef84a9f95f0cbb66d708558"
)
data = json.loads(source.read_text())
op = data["MoeDistributeDispatchV2"]
io = {k: v for k, v in op.items() if k.startswith(("input", "output")) and isinstance(v, dict) and "dtype" in v}
indices = {i for item in io.values() for i, dtype in enumerate(item["dtype"].split(",")) if dtype == "hifloat4_scale"}
assert indices == {26, 27, 28}
for item in io.values():
    for field in ("dtype", "format"):
        values = item[field].split(",")
        assert len(values) == 29
        item[field] = ",".join(value for i, value in enumerate(values) if i not in indices)
target.write_text(json.dumps(data, indent=2) + "\n")
assert (
    hashlib.sha256(target.read_bytes()).hexdigest()
    == "2b8b01fc00857ad6de7ed817c1ac3d19fa21b6a0f888c460765756133443becd"
)
print("Created overlay; original unchanged. Only use with the pinned nightly and W8A8 acceptance.")
