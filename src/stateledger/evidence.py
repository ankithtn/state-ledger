"""Server-side evidence hashing and freshness classification.

Two axes stay independent:

- ``status`` is what the check reported when it ran: passed, failed, or error.
- ``freshness`` is whether that result still describes the working tree:
  fresh, potentially_stale, or unknown.

A fresh verdict requires complete coverage: ``coverage == "files"``, a
non-empty recorded file set, and a non-empty scope. Anything less is
``potentially_stale`` or ``unknown``, never ``fresh``.

M2 does not execute checks. It hashes files, reads Git, and reports whether a
recorded result still applies; the agent decides whether to re-run.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from .gitinfo import (
    changed_paths,
    commit_distance,
    commit_exists,
    is_git_repository,
    uncommitted_paths,
)
from .models import EvidenceRecord, EvidenceStatus, StrictModel

Freshness = Literal["fresh", "potentially_stale", "unknown"]

EXCLUDED_PATH_PARTS = frozenset({".git", ".stateledger"})
MAX_HASHED_FILES = 2000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
_CHUNK_BYTES = 128 * 1024


@dataclass(frozen=True)
class ScopeHashResult:
    files: dict[str, str]
    complete: bool
    reasons: tuple[str, ...]


class EvidenceVerdict(StrictModel):
    evidence_id: str
    check_key: str
    status: EvidenceStatus
    freshness: Freshness
    reasons: list[str]
    ahead_by: int | None = None
    command: str
    superseded_by: str | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_inside(root: Path, path: Path) -> str | None:
    try:
        relative = path.relative_to(root).as_posix()
        resolved = path.resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(root.resolve()):
        return None
    return relative


def hash_scope(repo_root: str | Path, scope: Sequence[str]) -> ScopeHashResult:
    """Hash every file currently present in the declared scope.

    Directory entries (trailing ``/``) are walked; exact entries are files.
    Coverage is complete only when enumeration succeeded, no file was skipped,
    and at least one file was hashed. Incomplete coverage yields no hashes and
    must never be treated as fresh.
    """

    root = Path(repo_root)
    if not root.is_dir():
        return ScopeHashResult({}, False, ("repo_missing",))
    if not scope:
        return ScopeHashResult({}, False, ("empty_scope",))

    found: dict[str, str] = {}
    reasons: set[str] = set()
    totals = {"files": 0, "bytes": 0}

    def add_file(path: Path) -> None:
        relative = _relative_inside(root, path)
        if relative is None:
            reasons.add("scope_outside_repo")
            return
        if relative in found:
            return
        if any(part in EXCLUDED_PATH_PARTS for part in relative.split("/")):
            return
        try:
            size = path.stat().st_size
        except OSError:
            reasons.add("unreadable_file")
            return
        if totals["files"] >= MAX_HASHED_FILES:
            reasons.add("too_many_files")
            return
        if size > MAX_FILE_BYTES:
            reasons.add("file_too_large")
            return
        if totals["bytes"] + size > MAX_TOTAL_BYTES:
            reasons.add("scope_too_large")
            return
        try:
            digest = _sha256_file(path)
        except OSError:
            reasons.add("unreadable_file")
            return
        found[relative] = digest
        totals["files"] += 1
        totals["bytes"] += size

    for entry in scope:
        if entry.endswith("/"):
            directory = root / entry.rstrip("/")
            if not directory.is_dir():
                reasons.add("missing_scope_dir")
                continue
            if _relative_inside(root, directory) is None:
                reasons.add("scope_outside_repo")
                continue

            def on_error(_error: OSError) -> None:
                reasons.add("unreadable_directory")

            for current, dirnames, filenames in os.walk(
                directory, topdown=True, followlinks=False, onerror=on_error
            ):
                current_path = Path(current)
                kept: list[str] = []
                for name in sorted(dirnames):
                    child = current_path / name
                    if name in EXCLUDED_PATH_PARTS:
                        continue
                    if child.is_symlink():
                        reasons.add("symlink_skipped")
                        continue
                    kept.append(name)
                dirnames[:] = kept
                for name in sorted(filenames):
                    child = current_path / name
                    if child.is_symlink() and _relative_inside(root, child) is None:
                        reasons.add("scope_outside_repo")
                        continue
                    add_file(child)
            continue

        file_path = root / entry
        if not file_path.is_file():
            reasons.add("missing_scope_file")
            continue
        add_file(file_path)

    files = dict(sorted(found.items()))
    if not files and not reasons:
        reasons.add("no_files_in_scope")
    return ScopeHashResult(
        files=files,
        complete=not reasons and bool(files),
        reasons=tuple(sorted(reasons)),
    )


def _verdict(
    evidence: EvidenceRecord,
    freshness: Freshness,
    reasons: Sequence[str],
    ahead_by: int | None,
) -> EvidenceVerdict:
    return EvidenceVerdict(
        evidence_id=evidence.id,
        check_key=evidence.check_key,
        status=evidence.status,
        freshness=freshness,
        reasons=sorted(set(reasons)),
        ahead_by=ahead_by,
        command=evidence.command,
        superseded_by=evidence.superseded_by,
    )


def classify_evidence(
    evidence: EvidenceRecord, repo_root: str | Path
) -> EvidenceVerdict:
    """Classify relevance of a recorded result against the current repository."""

    root = Path(repo_root)
    if not root.is_dir():
        return _verdict(evidence, "unknown", ["repo_missing"], None)
    if not is_git_repository(root):
        return _verdict(evidence, "unknown", ["not_a_git_repository"], None)
    if not evidence.git_commit:
        return _verdict(evidence, "unknown", ["no_revision"], None)
    if not commit_exists(root, evidence.git_commit):
        return _verdict(evidence, "unknown", ["history_rewritten"], None)

    ahead_by = commit_distance(root, evidence.git_commit)
    distance_reasons = ["git_unavailable"] if ahead_by is None else []

    # Coverage precondition: without a complete recorded file set, a pass is
    # not proof of the current tree. It is stale, never fresh.
    if evidence.coverage != "files" or not evidence.files or not evidence.scope:
        return _verdict(
            evidence,
            "potentially_stale",
            ["coverage_incomplete", *distance_reasons],
            ahead_by,
        )

    current = hash_scope(root, evidence.scope)
    reasons: set[str] = set(distance_reasons)

    recorded = {item.path: item.sha256 for item in evidence.files}
    for path, digest in recorded.items():
        if path not in current.files:
            reasons.add("file_deleted")
        elif current.files[path] != digest:
            reasons.add("file_modified")
    for path in current.files:
        if path not in recorded:
            reasons.add("file_added_in_scope")
    if not current.complete:
        reasons.add("coverage_incomplete")

    changed = changed_paths(root, evidence.git_commit, evidence.scope)
    if changed is None:
        reasons.add("git_unavailable")
    elif changed:
        reasons.add("commit_touched_scope")

    dirty = uncommitted_paths(root, evidence.scope)
    if dirty is None:
        reasons.add("git_unavailable")
    elif dirty:
        reasons.add("uncommitted_changes")

    freshness: Freshness = "potentially_stale" if reasons else "fresh"
    return _verdict(evidence, freshness, reasons, ahead_by)