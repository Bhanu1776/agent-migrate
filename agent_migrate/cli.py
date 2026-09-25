"""agent-migrate FROM TO [--dry-run] [--only parts] [--target DIR]

Adding a harness = one reader or writer module + one line in READERS / WRITERS.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from importlib import import_module
from pathlib import Path

from .model import PARTS

READERS = {"claude-code": "claude_code", "codex": "codex"}
WRITERS = {"pi": "pi", "opencode": "opencode", "oh-my-pi": "oh_my_pi", "prime-agent": "prime_agent", "hermes": "hermes"}  # each writer module exposes DEFAULT_TARGET


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-migrate", description="Move a coding-agent setup from one harness to another.")
    ap.add_argument("source", choices=sorted(READERS))
    ap.add_argument("dest", choices=sorted(WRITERS))
    ap.add_argument("--dry-run", "-n", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--only", help=f"comma list of parts: {','.join(PARTS)}")
    ap.add_argument("--target", help="destination config dir (default: the harness's usual one)")
    ap.add_argument("--home", default="~", help="home dir to read from (for testing)")
    ap.add_argument("--verbose", "-v", action="store_true", help="list every item, not just the first few")
    a = ap.parse_args(argv)

    parts = set(PARTS) if not a.only else {x.strip() for x in a.only.split(",")}
    if bad := parts - set(PARTS):
        ap.error(f"unknown parts: {', '.join(sorted(bad))}")
    home = Path(a.home).expanduser()
    writer = import_module(f".writers.{WRITERS[a.dest]}", __package__)
    target = Path(a.target or writer.DEFAULT_TARGET).expanduser()

    bundle = import_module(f".readers.{READERS[a.source]}", __package__).read(home)
    plan = writer.plan(bundle, target, home, parts)

    print(f"{a.source} → {a.dest}   target: {target}{'   (DRY RUN — nothing is written)' if a.dry_run else ''}\n")
    for part in list(PARTS) + ["settings"]:
        rows = [x for x in plan.actions if x.part == part]
        if not rows:
            continue
        counts = ", ".join(f"{n} {k}" for k, n in Counter(r.kind for r in rows).items())
        print(f"■ {part}  ({counts})")
        shown = rows if a.verbose else [r for r in rows if r.kind != "skip"][:6]
        for r in shown:
            print(f"    {r.kind:5} {r.desc}")
        if len(shown) < len(rows):
            print(f"    … {len(rows) - len(shown)} more (use -v)")

    if plan.gaps:
        print(f"\n■ not migrated — do these by hand ({len(plan.gaps)})")
        for g in plan.gaps:
            print(f"    - {g}")

    if a.dry_run:
        print("\nDry run only. Run again without --dry-run to apply.")
        return 0

    failed = 0
    for x in plan.actions:
        if x.apply is None:
            continue
        try:
            x.apply()
        except Exception as e:  # keep going: one bad item must not block the rest
            failed += 1
            print(f"  ✗ {x.part}: {x.desc} — {e}", file=sys.stderr)
    done = sum(1 for x in plan.actions if x.apply) - failed
    print(f"\nApplied {done} changes" + (f", {failed} FAILED (see above)" if failed else "") + ". Restart the harness to load them.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
