#!/usr/bin/env python3
"""Generate missing LightRAG numbered test-instance API keys.

The target file is expected to be ignored by Git (for this repository it is
``.secrets/test.secrets``). Values are never printed; command output reports
only key names and lengths so it can be pasted into operational notes safely.
"""

from __future__ import annotations

import argparse
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

INSTANCE_RE = re.compile(r"^[0-9]{2}$")


@dataclass(frozen=True)
class EnsureResult:
    created: list[str]
    existing: list[str]
    key_length: int


def parse_instances(raw: str) -> list[str]:
    instances = [item for item in re.split(r"[\s,]+", raw.strip()) if item]
    for instance in instances:
        if not INSTANCE_RE.fullmatch(instance):
            raise ValueError(f"invalid instance id: {instance!r}")
    if len(set(instances)) != len(instances):
        raise ValueError("duplicate instance ids are not allowed")
    return instances


def read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def ensure_instance_api_keys(
    path: Path,
    *,
    instances: list[str],
    key_bytes: int = 32,
) -> EnsureResult:
    if key_bytes < 16:
        raise ValueError("key_bytes must be at least 16")
    for instance in instances:
        if not INSTANCE_RE.fullmatch(instance):
            raise ValueError(f"invalid instance id: {instance!r}")

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_values = read_key_values(path)
    created: list[str] = []
    existing: list[str] = []
    additions: list[str] = []
    for instance in instances:
        field = f"lightrag_test_{instance}_api_key"
        if existing_values.get(field):
            existing.append(field)
            continue
        value = secrets.token_hex(key_bytes)
        additions.append(f"{field}={value}")
        created.append(field)

    if additions:
        prefix = ""
        if path.exists() and path.read_text(encoding="utf-8") and not path.read_text(
            encoding="utf-8"
        ).endswith("\n"):
            prefix = "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix)
            handle.write("\n# LightRAG numbered test instance API keys\n")
            handle.write("\n".join(additions))
            handle.write("\n")

    return EnsureResult(created=created, existing=existing, key_length=key_bytes * 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(".secrets/test.secrets"),
        help="Secrets file to update.",
    )
    parser.add_argument(
        "--instances",
        default="01,02,03,04,05",
        help="Comma or whitespace separated instance ids.",
    )
    parser.add_argument(
        "--key-bytes",
        type=int,
        default=32,
        help="Random bytes per generated key; 32 bytes produce 64 hex chars.",
    )
    args = parser.parse_args(argv)
    result = ensure_instance_api_keys(
        args.path,
        instances=parse_instances(args.instances),
        key_bytes=args.key_bytes,
    )
    for field in result.created:
        print(f"created={field} length={result.key_length}")
    for field in result.existing:
        print(f"existing={field} length={result.key_length}")
    if not result.created:
        print("created=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
