"""Validated checkpoint contract for StateLedger.

The draft models are the only client-facing shape. Persisted models add
server-generated identity, ordering, and coverage fields. Git data is an
internal, server-supplied value and is never part of a client draft.

M1 established the contract; M2 adds server-side file hashing and an
evidence read model. The checkpoint payload schema is versioned separately
from the SQLite schema (see store.DB_SCHEMA_VERSION).

Rules that matter:
- ``scope_only`` evidence must never be classified ``fresh``;
- ``superseded_by`` is mutable bookkeeping and is deliberately *not* stored in
  the immutable payload; reporting reads it from the normalized evidence table;
- identifiers are prefixed random UUID4 values (not sortable); ordering comes
  from the per-task ``seq`` column.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CHECKPOINT_SCHEMA_VERSION = 1
# Backward-compatible name used by the M1 docs/tests.
SCHEMA_VERSION = CHECKPOINT_SCHEMA_VERSION
MAX_PATH_LENGTH = 512
MAX_SCOPE_ENTRIES = 50

TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
CHECK_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
COMMIT_PATTERN = r"^[0-9a-f]{7,40}$"

ChangeType = Literal["added", "modified", "deleted", "renamed"]
EvidenceKind = Literal["test", "build", "lint", "typecheck", "manual", "other"]
EvidenceStatus = Literal["passed", "failed", "error"]
EvidenceCoverage = Literal["files", "scope_only"]

NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
RationaleText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_GLOB_CHARS = frozenset("*?[]")


class StrictModel(BaseModel):
    """Reject unknown fields so callers cannot smuggle server-owned data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def normalize_repo_path(value: str, *, allow_directory_prefix: bool) -> str:
    """Validate and normalize a repository-relative path.

    Directory prefixes are allowed only when ``allow_directory_prefix`` is true
    and must end with ``/``; otherwise the value is treated as a file path.
    Backslashes are normalized to forward slashes.
    """

    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if "\x00" in value:
        raise ValueError("path must not contain NUL bytes")

    raw = value.strip()
    if not raw:
        raise ValueError("path must not be empty")

    candidate = raw.replace("\\", "/")
    if candidate.startswith("/") or _WINDOWS_DRIVE.match(candidate):
        raise ValueError("path must be repository-relative")

    is_directory = candidate.endswith("/")
    if is_directory and not allow_directory_prefix:
        raise ValueError("file paths must not end with '/'")

    parts = [part for part in candidate.split("/") if part]
    if not parts:
        raise ValueError("path must not be empty")
    for part in parts:
        if part in {".", ".."}:
            raise ValueError("path must not contain '.' or '..' segments")
        if any(ch in _GLOB_CHARS for ch in part):
            raise ValueError("path must not contain glob metacharacters")
        if part != part.strip():
            raise ValueError("path segments must not have surrounding whitespace")

    normalized = "/".join(parts)
    if is_directory:
        normalized += "/"
    if len(normalized) > MAX_PATH_LENGTH:
        raise ValueError(f"path must be at most {MAX_PATH_LENGTH} characters")
    return normalized


def validate_task_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("task_id must be a string")
    task_id = value.strip()
    if not re.fullmatch(TASK_ID_PATTERN, task_id):
        raise ValueError(
            "task_id must match ^[a-z0-9][a-z0-9._-]{0,63}$"
        )
    return task_id


def validate_session_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("session_id must be a string")
    session_id = value.strip()
    if not re.fullmatch(SESSION_ID_PATTERN, session_id):
        raise ValueError(
            "session_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
        )
    return session_id


def canonical_json(payload: Any) -> str:
    """Stable JSON used for hashing and storage."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ChangedFileInput(StrictModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATH_LENGTH)]
    change_type: ChangeType

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return normalize_repo_path(value, allow_directory_prefix=False)


class DecisionInput(StrictModel):
    text: NonEmptyText
    rationale: RationaleText | None = None
    supersedes_id: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ] | None = None


class VerificationDraft(StrictModel):
    """A reported verification result, submitted at the moment it is observed.

    The server captures Git state and scope hashes when this is recorded. The
    draft never carries identity, commit, coverage, hashes, or trust.
    """

    check_key: Annotated[
        str, StringConstraints(min_length=1, max_length=128, pattern=CHECK_KEY_PATTERN)
    ]
    kind: EvidenceKind
    command: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    status: EvidenceStatus
    exit_code: Annotated[int, Field(ge=-255, le=255)] | None = None
    scope: Annotated[
        list[str], Field(min_length=1, max_length=MAX_SCOPE_ENTRIES)
    ]
    output_excerpt: Annotated[
        str, StringConstraints(max_length=4000)
    ] | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: list[str]) -> list[str]:
        normalized = [
            normalize_repo_path(entry, allow_directory_prefix=True) for entry in value
        ]
        if len(set(normalized)) != len(normalized):
            raise ValueError("scope entries must be unique after normalization")
        return normalized


# M1/M2 name kept as a compatibility alias for importers and tests.
EvidenceInput = VerificationDraft


class GitSnapshot(StrictModel):
    """Server-captured Git state. Never accepted from a client draft."""

    commit: Annotated[str, StringConstraints(pattern=COMMIT_PATTERN)] | None = None
    branch: Annotated[str, StringConstraints(max_length=255)] | None = None
    dirty: bool | None = None
    repo_root: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None
    captured_at: datetime | None = None
    capture_error: Annotated[str, StringConstraints(max_length=500)] | None = None


class CheckpointDraft(StrictModel):
    """Client-supplied checkpoint content.

    Deliberately excludes verification. Results are captured separately and
    referenced by id, so saving a checkpoint can never re-hash a later tree and
    attach those hashes to an earlier result.
    """

    goal: NonEmptyText
    decisions: list[DecisionInput] = Field(default_factory=list, max_length=100)
    completed_steps: list[NonEmptyText] = Field(default_factory=list, max_length=100)
    changed_files: list[ChangedFileInput] = Field(
        default_factory=list, max_length=200
    )
    blockers: list[NonEmptyText] = Field(default_factory=list, max_length=50)
    next_actions: list[NonEmptyText] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _unique_changed_paths(self) -> "CheckpointDraft":
        paths = [item.path for item in self.changed_files]
        if len(set(paths)) != len(paths):
            raise ValueError("changed_files paths must be unique")
        return self


class PersistedDecision(DecisionInput):
    id: str


class PersistedEvidence(VerificationDraft):
    """Immutable evidence reference inside a checkpoint payload.

    ``capture_id`` points at the verification snapshot whose hashes and commit
    are authoritative. ``git_commit`` is copied from that snapshot, not from
    the checkpoint's Git state. Both default to None for pre-v3 payloads.
    """

    id: str
    capture_id: str | None = None
    coverage: EvidenceCoverage
    git_commit: str | None = None
    captured_at: datetime


class Checkpoint(StrictModel):
    """The canonical persisted checkpoint. Stored as JSON and mirrored in tables."""

    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    id: str
    task_id: str
    seq: Annotated[int, Field(ge=1)]
    parent_id: str | None = None
    session_id: str | None = None
    goal: NonEmptyText
    decisions: list[PersistedDecision]
    completed_steps: list[NonEmptyText]
    changed_files: list[ChangedFileInput]
    blockers: list[NonEmptyText]
    next_actions: list[NonEmptyText]
    git: GitSnapshot | None = None
    verification: list[PersistedEvidence]
    created_at: datetime

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def fingerprint(self) -> str:
        return sha256_payload(self.canonical_payload())


class CheckpointSummary(StrictModel):
    id: str
    task_id: str
    seq: int
    parent_id: str | None = None
    goal: NonEmptyText
    created_at: datetime
    decision_count: int
    evidence_count: int


class DecisionRecord(StrictModel):
    """Normalized decision row, including supersession links."""

    id: str
    checkpoint_id: str
    task_id: str
    checkpoint_seq: Annotated[int, Field(ge=1)]
    position: int
    text: NonEmptyText
    rationale: RationaleText | None = None
    supersedes_id: str | None = None
    created_at: datetime


class EvidenceFileHash(StrictModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATH_LENGTH)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class VerificationCapture(VerificationDraft):
    """Immutable verification snapshot: result + Git state + scope hashes.

    Created only by ``LedgerStore.record_verification`` at the moment the result
    is observed. Checkpoints reference it by id and never re-capture it.
    """

    id: str
    task_id: str
    git_commit: Annotated[
        str, StringConstraints(pattern=COMMIT_PATTERN)
    ] | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    git_error: str | None = None
    captured_at: datetime
    coverage: EvidenceCoverage
    trust: Literal["agent_reported"] = "agent_reported"
    files: list[EvidenceFileHash] = Field(default_factory=list)


class EvidenceRecord(StrictModel):
    """Normalized evidence view used by reporting and freshness APIs.

    ``superseded_by`` is read from the mutable ``evidence`` table, never from
    the immutable checkpoint payload. File hashes come from ``evidence_files``.
    """

    id: str
    checkpoint_id: str
    task_id: str
    checkpoint_seq: Annotated[int, Field(ge=1)]
    capture_id: str | None = None
    check_key: str
    kind: EvidenceKind
    command: str
    status: EvidenceStatus
    exit_code: int | None = None
    scope: list[str]
    output_excerpt: str | None = None
    git_commit: str | None = None
    captured_at: datetime
    coverage: EvidenceCoverage
    superseded_by: str | None = None
    trust: str = "agent_reported"
    files: list[EvidenceFileHash] = Field(default_factory=list)


class EvidencePage(StrictModel):
    """Newest-first evidence page with explicit truncation metadata."""

    items: list[EvidenceRecord] = Field(default_factory=list)
    total: int = 0
    limit: int
    truncated: bool = False