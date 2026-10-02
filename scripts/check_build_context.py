"""Measure the effective Docker build context and fail when it grows.

The image build uses the repository root as its context, so anything not named
by ``.dockerignore`` is uploaded to the daemon on every build: local Room data,
virtualenvs, build output and the Git object store all count. This script
applies ``.dockerignore`` to the working tree and reports the size that would
actually be sent, so a regression (a missing exclusion, a new large directory)
fails a gate instead of silently slowing every image build.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_MAX_MIB = 16.0
CONTEXT_ROOT = Path(__file__).resolve().parent.parent


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern[index : index + 2] == "**":
            parts.append(".*")
            index += 2
            continue
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        elif char == "[":
            closing = pattern.find("]", index)
            if closing < 0:
                parts.append(re.escape(char))
            else:
                parts.append(pattern[index : closing + 1])
                index = closing
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


def load_dockerignore(path: Path) -> list[tuple[bool, re.Pattern[str]]]:
    """Return (negated, matcher) rules in the order Docker applies them."""
    if not path.exists():
        return []
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        line = line.rstrip("/")
        if not line:
            continue
        rules.append((negated, _pattern_to_regex(line)))
    return rules


def is_excluded(rules: list[tuple[bool, re.Pattern[str]]], relative: str) -> bool:
    excluded = False
    parents = relative.split("/")
    # A rule that matches a directory also covers everything below it, so every
    # path prefix is tested as well as the path itself.
    for depth in range(1, len(parents) + 1):
        candidate = "/".join(parents[:depth])
        for negated, matcher in rules:
            if matcher.match(candidate):
                excluded = not negated
    return excluded


def measure(context_root: Path) -> tuple[int, int]:
    rules = load_dockerignore(context_root / ".dockerignore")
    total = 0
    files = 0
    for path in sorted(context_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(context_root).as_posix()
        if is_excluded(rules, relative):
            continue
        files += 1
        total += path.stat().st_size
    return total, files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-mib",
        type=float,
        default=DEFAULT_MAX_MIB,
        help=f"fail when the effective context is larger than this (default {DEFAULT_MAX_MIB})",
    )
    parser.add_argument(
        "--context-root", type=Path, default=CONTEXT_ROOT, help="build context root"
    )
    args = parser.parse_args(argv)

    if not (args.context_root / ".dockerignore").exists():
        print("ERROR: no .dockerignore in the build context; the whole repository "
              "would be uploaded to the daemon.", file=sys.stderr)
        return 1

    total, files = measure(args.context_root)
    mib = total / (1024 * 1024)
    limit = args.max_mib
    print(
        f"build context: {files} files, {mib:.2f} MiB after .dockerignore "
        f"(limit {limit:.2f} MiB)"
    )
    if mib > limit:
        print(
            "ERROR: the image build context is oversized. Exclude the offending "
            "directory in .dockerignore, do not raise the limit.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
