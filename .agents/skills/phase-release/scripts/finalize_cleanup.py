#!/usr/bin/env python3
"""Compatibility entry point for SQLite-gated whole-workspace cleanup.

Legacy JSON journal/receipt arguments are deliberately unsupported: they cannot
prove global ownership or formal Released state. Use --owner and --cycle.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from colab_daily.lifecycle import main as lifecycle_main


def main(argv=None):
    # --project-root is a global lifecycle option; lift it before the subcommand.
    args = list(sys.argv[1:] if argv is None else argv)
    prefix = []
    if "--project-root" in args:
        index = args.index("--project-root")
        prefix = args[index:index + 2]
        del args[index:index + 2]
    return lifecycle_main([*prefix, "cleanup", *args])


if __name__ == "__main__":
    raise SystemExit(main())
