"""Reconstruct the fixed public SWE-smith replay input (no tools are executed)."""

import argparse
import hashlib
import json
from pathlib import Path


def prepare(manifest, rows):
    conversations = []
    for selected in manifest["selected"]:
        row = rows[selected["row_idx"]]
        assert row["instance_id"] == selected["instance_id"]
        assert row["traj_id"] == selected["traj_id"]
        messages = []
        for message in json.loads(row["messages"]):
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    item.get("text", "")
                    if isinstance(item, dict) and item.get("type") == "text"
                    else item
                    if isinstance(item, str)
                    else ""
                    for item in content
                )
            role = message.get("role")
            if content and role in ("system", "user", "tool"):
                messages.append({"role": "user" if role == "tool" else role, "content": content})
        users = [m for m in messages if m["role"] == "user"][:8]
        systems = [m for m in messages if m["role"] == "system"][:1]
        assert len(users) == 8
        conversations.append([{"messages": systems + [users[0]]}] + [{"messages": [m]} for m in users[1:]])
    payload = json.dumps({"conversations": conversations}, ensure_ascii=False).encode()
    assert hashlib.sha256(payload).hexdigest() == manifest["dataset_sha256"]
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, help="Optional original datasets-server raw-*.json cache")
    parser.add_argument("--output", type=Path, default=Path("agentic_dataset.json"))
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("dataset-manifest.json").read_text())
    wanted = {r["row_idx"] for r in manifest["selected"]}
    rows = {}
    if args.raw_dir:
        for path in args.raw_dir.glob("raw-*.json"):
            for row in json.loads(path.read_text())["rows"]:
                if row["row_idx"] in wanted:
                    rows[row["row_idx"]] = row["row"]
    else:
        from datasets import load_dataset

        dataset = load_dataset(
            manifest["dataset"], revision=manifest["revision"], split=manifest["split"], streaming=True
        )
        for index, row in enumerate(dataset):
            if index in wanted:
                rows[index] = row
            if index >= max(wanted):
                break
    args.output.write_bytes(prepare(manifest, rows))
    print(manifest["dataset_sha256"])
