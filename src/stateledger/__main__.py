"""Minimal CLI fallback for StateLedger.

Only ``restore`` is supported: it prints the Markdown context pack to stdout.
Task discovery is intentionally not invented; ``--task-id`` is required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .context import DEFAULT_HISTORY, MAX_HISTORY, build_context_pack
from .store import LedgerStore, default_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stateledger",
        description="StateLedger CLI fallback",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    restore = subparsers.add_parser(
        "restore",
        help="print the Markdown context pack for a task",
    )
    restore.add_argument(
        "--repo",
        required=True,
        help="repository root containing .stateledger/ledger.db",
    )
    restore.add_argument(
        "--task-id",
        required=True,
        help="task id to restore (task discovery is not implemented)",
    )
    restore.add_argument(
        "--history",
        type=int,
        default=DEFAULT_HISTORY,
        help=f"checkpoints to include (1-{MAX_HISTORY}, default {DEFAULT_HISTORY})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            # Markdown can contain non-ANSI user text; never fail while writing it.
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    if not 1 <= args.history <= MAX_HISTORY:
        parser.error(f"--history must be between 1 and {MAX_HISTORY}")

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"stateledger: repository root does not exist: {repo}", file=sys.stderr)
        return 1

    try:
        store = LedgerStore(default_db_path(repo), repo)
        store.initialize()
        pack = build_context_pack(store, args.task_id, history=args.history)
    except ValueError as exc:
        print(f"stateledger: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive boundary
        print(
            f"stateledger: restore failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    sys.stdout.write(pack.markdown)
    if not pack.markdown.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())