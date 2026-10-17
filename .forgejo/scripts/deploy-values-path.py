#!/usr/bin/env python3
"""Read or update one scalar path in a Helm values file without reformatting it."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


KEY_VALUE = re.compile(
    r"^(?P<prefix> *)(?P<key>[A-Za-z0-9_.-]+):(?P<separator>\s*)(?P<value>.*?)(?P<newline>\r?\n?)$"
)


def find_scalar(lines: list[str], wanted: tuple[str, ...]) -> tuple[int, re.Match[str], str]:
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = KEY_VALUE.match(line)
        if not match:
            continue
        indent = len(match.group("prefix"))
        key = match.group("key")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = tuple(item[1] for item in stack) + (key,)
        value = match.group("value").strip()
        if current == wanted:
            if not value:
                raise SystemExit(f"{'.'.join(wanted)} is empty")
            return index, match, value.strip("'\"")
        if not value:
            stack.append((indent, key))
    raise SystemExit(f"could not locate {'.'.join(wanted)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("get", "set"))
    parser.add_argument("values", type=Path)
    parser.add_argument("path")
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()

    wanted = tuple(args.path.split("."))
    lines = args.values.read_text().splitlines(keepends=True)
    index, match, old_value = find_scalar(lines, wanted)
    if args.operation == "get":
        print(old_value)
        return 0
    if args.value is None:
        parser.error("set requires VALUE")
    lines[index] = (
        f'{match.group("prefix")}{match.group("key")}:'
        f'{match.group("separator")}{args.value}{match.group("newline")}'
    )
    args.values.write_text("".join(lines))
    print(old_value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
