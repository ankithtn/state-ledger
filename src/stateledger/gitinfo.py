"""Read-only Git inspection for StateLedger.

Every operation shells out to ``git`` with ``GIT_OPTIONAL_LOCKS=0`` so status
checks cannot rewrite the index. The repository is never modified.

M2 scope: capture the current revision/dirty state and answer the questions the
freshness classifier needs (does a commit exist, what changed since it, what is
dirty). Nothing here writes to the working tree or to Git metadata.

Failure policy: capture never raises. Timeouts and command failures become a
``capture_error`` and unknown fields. Classification treats Git query failures
as stale/unknown, never fresh.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .models import GitSnapshot

GIT_TIMEOUT_SECONDS = 15.0
LEDGER_DIR = ".stateledger"


def _is_ledger_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return normalized == LEDGER_DIR or normalized.startswith(f"{LEDGER_DIR}/")


def _run_git(
    repo_root: str | Path,
    args: Sequence[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        [
            "git",
            "--no-pager",
            "--literal-pathspecs",
            "-c",
            "core.quotepath=false",
            "-C",
            str(repo_root),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def _safe_git(
    repo_root: str | Path,
    args: Sequence[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return _run_git(repo_root, args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _nul_paths(output: str) -> list[str]:
    return [item for item in output.split("\0") if item and not _is_ledger_path(item)]


def _status_paths(output: str) -> list[str]:
    """Parse ``git status --porcelain=v1 -z`` without losing rename targets."""

    entries = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if path and not _is_ledger_path(path):
            paths.append(path)
        if status[:1] in {"R", "C"}:
            # In -z rename/copy records the old path is the next NUL field.
            index += 1
    return paths


def is_git_repository(repo_root: str | Path) -> bool:
    root = Path(repo_root)
    if not root.is_dir():
        return False
    result = _safe_git(root, ["rev-parse", "--git-dir"])
    return result is not None and result.returncode == 0


def capture_git_state(repo_root: str | Path) -> GitSnapshot:
    """Capture the current Git state without raising.

    Missing paths, non-repos, empty repos, timeouts, and later command failures
    all produce a snapshot with ``capture_error`` and unknown fields.
    """

    path = Path(repo_root)
    captured_at = datetime.now(timezone.utc)
    resolved = str(path.resolve())

    try:
        if not path.exists():
            return GitSnapshot(
                repo_root=resolved,
                captured_at=captured_at,
                capture_error="repo_missing",
            )

        top = _safe_git(path, ["rev-parse", "--show-toplevel"])
        if top is None:
            return GitSnapshot(
                repo_root=resolved,
                captured_at=captured_at,
                capture_error="git_unavailable",
            )
        if top.returncode != 0:
            return GitSnapshot(
                repo_root=resolved,
                captured_at=captured_at,
                capture_error="not_a_git_repository",
            )

        root = top.stdout.strip() or resolved

        head = _safe_git(root, ["rev-parse", "--verify", "HEAD"])
        if head is None:
            return GitSnapshot(
                repo_root=root,
                captured_at=captured_at,
                capture_error="git_unavailable",
            )
        if head.returncode != 0:
            symbolic = _safe_git(root, ["symbolic-ref", "--short", "-q", "HEAD"])
            branch = symbolic.stdout.strip() if symbolic and symbolic.returncode == 0 else None
            return GitSnapshot(
                repo_root=root,
                branch=branch or None,
                dirty=None,
                captured_at=captured_at,
                capture_error="no_commits",
            )

        commit = head.stdout.strip()

        branch_result = _safe_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        branch_name = (
            branch_result.stdout.strip()
            if branch_result is not None and branch_result.returncode == 0
            else ""
        )
        branch = None if not branch_name or branch_name == "HEAD" else branch_name

        status = _safe_git(
            root, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]
        )
        if status is None or status.returncode != 0:
            return GitSnapshot(
                commit=commit,
                branch=branch,
                dirty=None,
                repo_root=root,
                captured_at=captured_at,
                capture_error="git_status_failed",
            )

        return GitSnapshot(
            commit=commit,
            branch=branch,
            dirty=bool(_status_paths(status.stdout)),
            repo_root=root,
            captured_at=captured_at,
            capture_error=None,
        )
    except Exception as exc:  # defensive: capture must never crash a save
        return GitSnapshot(
            repo_root=resolved,
            branch=None,
            dirty=None,
            captured_at=captured_at,
            capture_error=f"git_unavailable: {exc.__class__.__name__}",
        )


def commit_exists(repo_root: str | Path, commit: str | None) -> bool:
    if not commit:
        return False
    result = _safe_git(repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    return result is not None and result.returncode == 0


def commit_distance(repo_root: str | Path, commit: str) -> int | None:
    """Number of commits between ``commit`` and HEAD, or None if unavailable."""

    result = _safe_git(repo_root, ["rev-list", "--count", f"{commit}..HEAD"])
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def changed_paths(
    repo_root: str | Path,
    commit: str,
    scope: Sequence[str] = (),
) -> list[str] | None:
    """Paths changed between ``commit`` and HEAD, optionally limited to scope."""

    args = ["diff", "--name-only", "-z", f"{commit}..HEAD"]
    if scope:
        args.extend(["--", *scope])
    result = _safe_git(repo_root, args)
    if result is None or result.returncode != 0:
        return None
    return _nul_paths(result.stdout)


def uncommitted_paths(
    repo_root: str | Path,
    scope: Sequence[str] = (),
) -> list[str] | None:
    """Working-tree changes (staged, unstaged, untracked), optionally in scope."""

    args = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
    ]
    if scope:
        args.extend(["--", *scope])
    result = _safe_git(repo_root, args)
    if result is None or result.returncode != 0:
        return None
    return _status_paths(result.stdout)