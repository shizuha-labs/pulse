#!/usr/bin/env python3
"""PLAT-13191-shaped static gate: no unescaped % anywhere in supervisord.conf.

The 2026-11-09 Pulse release crash family (PLAT-13203) traced to a bare ``%h``
inside supervisord directives: supervisord's config parser reads a lone ``%`` as
the start of a ``%(key)s`` expansion and dies at boot with "incomplete format",
crash-looping every process in the file. The durable convention (pulse#949) is:

* every literal percent in ANY directive (program names, ``command=`` lines,
  environment, everything) is ``%%``-escaped; downstream consumers (e.g. celery
  ``-n worker@%%h``) receive a single ``%`` after supervisord's parse;
* the only allowed raw form is a complete Python-style expansion
  ``%(name)s`` / ``%(name)d`` / ``%(name)i`` (supervisord's known keys include
  ``here``, ``program_name``, ``group_name``, ``process_num``,
  ``host_node_name``).

This checker sweeps the whole rendered file (not a line-type whitelist, per the
2026-11-09 scope refinement with aoi) so the class is caught wherever it next
mutates.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass

DEFAULT_FILE = "supervisord.conf"

# A complete expansion: %(anything-without-parens)s, incl. flags/width (e.g. %(process_num)02d)
PCT_SPEC = re.compile(r"%\([^()]*\)[#0\- +]*\d*[sdi]")


@dataclass
class Violation:
    line_no: int
    column: int
    snippet: str
    message: str


def sweep(text: str, display: str = DEFAULT_FILE) -> list[Violation]:
    """Single pass over the whole file; flag any percent that is neither an
    escaped ``%%`` nor the start of a complete ``%(name)X`` expansion."""
    violations: list[Violation] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "%":
            i += 1
            continue
        if i + 1 < n and text[i + 1] == "%":
            i += 2  # escaped literal percent
            continue
        m = PCT_SPEC.match(text, i)
        if m:
            i = m.end()
            continue
        line_no = text.count("\n", 0, i) + 1
        line_start = text.rfind("\n", 0, i) + 1
        line_end = text.find("\n", i)
        if line_end == -1:
            line_end = n
        snippet = text[line_start:line_end].strip()
        violations.append(
            Violation(
                line_no=line_no,
                column=i - line_start + 1,
                snippet=snippet if len(snippet) <= 120 else snippet[:117] + "...",
                message=(
                    f"{display}:{line_no}: bare % not part of %% escape or "
                    f"complete %(name)X expansion (snippet: {snippet!r})"
                ),
            )
        )
        i += 1
    return violations


def self_test() -> int:
    """Internal assertions: the class is caught, valid forms pass."""
    escaped = "[program:w]\ncommand=celery worker -n w@%%h\n"
    assert sweep(escaped) == [], "escaped %%h must pass"
    bare = "[program:w]\ncommand=celery worker -n w@%h\n"
    vs = sweep(bare)
    assert len(vs) == 1 and vs[0].line_no == 2, "bare %h must be caught on line 2"
    name = "[program:worker-%(process_num)02d]\ncommand=echo %(here)s\n"
    assert sweep(name) == [], "valid expansions must pass"
    print("self-test OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root containing supervisord.conf")
    parser.add_argument("--file", default=None, help="explicit config path (default: <root>/supervisord.conf)")
    parser.add_argument("--self-test", action="store_true", help="run internal assertions")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    path = pathlib.Path(args.file) if args.file else pathlib.Path(args.root) / DEFAULT_FILE
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    violations = sweep(path.read_text(encoding="utf-8"), display=str(path))
    if violations:
        for v in violations:
            print(v.message)
        print(f"FAIL: {len(violations)} unescaped % occurrence(s) in {path}")
        return 1
    print(f"OK: {path} has no unescaped %")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
