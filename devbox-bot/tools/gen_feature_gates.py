"""Generate the DevBox Statsig feature-gate manifest from the Grok Bot source."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

EXPECTED_GATE_COUNT = 608
FLAGS_START = re.compile(r"export\s+const\s+FLAGS\s*=\s*\{")
GATE_ENTRY = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{\s*"
    r"client\s*:\s*(?:true|false)\s*,\s*"
    r"default\s*:\s*(true|false)\s*"
    r"\}\s*,?",
    re.MULTILINE | re.DOTALL,
)


def extract_gates(source_text: str) -> dict[str, bool]:
    source_text = re.sub(r"/\*.*?\*/", "", source_text, flags=re.DOTALL)
    source_text = re.sub(r"(?m)^[ \t]*//.*$", "", source_text)
    match = FLAGS_START.search(source_text)
    if match is None:
        raise ValueError("could not find the FLAGS block")

    depth = 1
    block_start = match.end()
    index = block_start
    while index < len(source_text) and depth:
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        raise ValueError("unterminated FLAGS block")

    gates: dict[str, bool] = {}
    for entry in GATE_ENTRY.finditer(source_text, block_start, index - 1):
        name, default = entry.groups()
        if name in gates:
            raise ValueError(f"duplicate feature gate: {name}")
        gates[name] = default == "true"
    assert len(gates) == EXPECTED_GATE_COUNT, (
        f"expected {EXPECTED_GATE_COUNT} flags, found {len(gates)}"
    )
    return dict(sorted(gates.items()))


def source_commit(source_path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source_path.parent), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def generate(source_path: Path, output_path: Path) -> int:
    gates = extract_gates(source_path.read_text(encoding="utf-8"))
    manifest = {"source": source_commit(source_path), "gates": gates}
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return len(gates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "devbox_bot" / "feature_gates.json"
    count = generate(args.source, output)
    print(f"wrote {count} gates to {output}")


if __name__ == "__main__":
    main()
