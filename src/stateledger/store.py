"""Append-only SQLite storage for StateLedger checkpoints.

Guarantees:
- migrations are versioned and idempotent (``PRAGMA user_version``, now v2);
- sequence allocation and every child insert share one IMMEDIATE transaction;
- a failed append leaves no rows and consumes no sequence number;
- checkpoint history is insert-only, enforced by database triggers;
- evidence supersession is one-way, non-self, and confined to one task.

Server-side Git capture and scope hashing are applied on append. The immutable
checkpoint payload is versioned independently from the SQLite schema: the
payload schema stays at 1, the database schema is ``DB_SCHEMA_VERSION``.

Not implemented: MCP server, CLI, context pack, frontend.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from pydantic import ValidationError

from .evidence import hash_scope
from .gitinfo import capture_git_state
from .models import (
    Checkpoint,
    CheckpointDraft,
    CheckpointSummary,
    DecisionRecord,
    EvidenceFileHash,
    EvidencePage,
    EvidenceRecord,
    EvidenceStatus,
    GitSnapshot,
    PersistedDecision,
    PersistedEvidence,
    VerificationCapture,
    VerificationDraft,
    canonical_json,
    validate_session_id,
    validate_task_id,
)

DEFAULT_DB_DIR = ".stateledger"
DB_FILENAME = "ledger.db"
DB_SCHEMA_VERSION = 3
MAX_VERIFICATION_CAPTURES = 50

_MIGRATION_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATIONS: dict[int, Path] = {
    1: _MIGRATION_DIR / "001_init.sql",
    2: _MIGRATION_DIR / "002_evidence_supersession.sql",
    3: _MIGRATION_DIR / "003_verification_captures.sql",
}


class LedgerError(Exception):
    """Base class for storage errors."""


class MigrationError(LedgerError):
    """The ledger schema is newer than this code, or a migration failed."""


class CheckpointNotFound(LedgerError):
    pass


class ParentCheckpointNotFound(LedgerError):
    pass


class TaskRepositoryMismatch(LedgerError):
    """A task id is already bound to a different repository root."""


class CorruptLedger(LedgerError):
    """Stored JSON does not match its recorded hash, or fails validation."""


class SupersessionError(LedgerError):
    """An evidence supersession link is invalid."""


class VerificationCaptureNotFound(LedgerError):
    pass


def default_db_path(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / DEFAULT_DB_DIR / DB_FILENAME


def _new_id(prefix: str) -> str:
    """Prefixed random UUID4. Not sortable; ``seq`` provides ordering."""
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LedgerStore:
    """One ledger per repository, opened explicitly and initialized on demand."""

    def __init__(self, db_path: str | Path, repo_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.repo_root = str(Path(repo_root).resolve())
        self._initialized = False

    # ------------------------------------------------------------------ setup

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, timeout=5.0
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Apply pending migrations. Safe to call repeatedly."""

        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > DB_SCHEMA_VERSION:
                raise MigrationError(
                    f"ledger schema version {version} is newer than supported "
                    f"version {DB_SCHEMA_VERSION}"
                )
            for target in range(version + 1, DB_SCHEMA_VERSION + 1):
                script_path = _MIGRATIONS.get(target)
                if script_path is None or not script_path.is_file():
                    raise MigrationError(f"missing migration for version {target}")
                script = script_path.read_text(encoding="utf-8")
                try:
                    conn.executescript(script)
                except sqlite3.Error as exc:
                    raise MigrationError(
                        f"migration {target} failed: {exc}"
                    ) from exc
                applied = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if applied != target:
                    raise MigrationError(
                        f"migration {target} left schema at version {applied}"
                    )
        self._initialized = True

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise LedgerError("initialize() must be called before using the store")

    # ------------------------------------------------------------------ write

    def record_verification(
        self,
        task_id: str,
        result: VerificationDraft,
        *,
        session_id: str | None = None,
    ) -> VerificationCapture:
        """Record a reported result and snapshot Git + scope hashes now.

        Call this at the moment the check is observed. Checkpoints attach the
        returned capture id later; they never re-hash the tree. ``trust`` is
        always ``agent_reported`` — the server does not prove the command ran.
        """

        self._require_initialized()
        task_id = validate_task_id(task_id)
        if not isinstance(result, VerificationDraft):
            raise TypeError("result must be a VerificationDraft")
        if session_id is not None:
            session_id = validate_session_id(session_id)

        git = self._capture_git_safe()
        hashed = hash_scope(self.repo_root, result.scope)
        coverage = "files" if hashed.complete and hashed.files else "scope_only"
        files = (
            [
                EvidenceFileHash(path=path, sha256=digest)
                for path, digest in sorted(hashed.files.items())
            ]
            if coverage == "files"
            else []
        )
        captured_at = _utc_now()

        capture = VerificationCapture(
            id=_new_id("vc"),
            task_id=task_id,
            check_key=result.check_key,
            kind=result.kind,
            command=result.command,
            status=result.status,
            exit_code=result.exit_code,
            scope=list(result.scope),
            output_excerpt=result.output_excerpt,
            git_commit=git.commit,
            git_branch=git.branch,
            git_dirty=git.dirty,
            git_error=git.capture_error,
            captured_at=captured_at,
            coverage=coverage,
            trust="agent_reported",
            files=files,
        )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_task(conn, task_id, captured_at)
                if session_id is not None:
                    self._touch_session(conn, session_id, task_id, captured_at)
                self._insert_capture(conn, capture)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return capture

    def append_checkpoint(
        self,
        task_id: str,
        draft: CheckpointDraft,
        *,
        session_id: str | None = None,
        parent_id: str | None = None,
        git: GitSnapshot | None = None,
        verification_capture_ids: Sequence[str] = (),
    ) -> Checkpoint:
        """Capture checkpoint Git state and append atomically.

        Verification is referenced by capture id. This method never hashes the
        current tree and never re-captures evidence, so a result recorded at A
        keeps A's commit and hashes even when the checkpoint is saved at B.

        ``git`` is an internal override for tests and imports; clients cannot
        supply it because it is not part of CheckpointDraft.
        """

        self._require_initialized()
        task_id = validate_task_id(task_id)
        if not isinstance(draft, CheckpointDraft):
            raise TypeError("draft must be a CheckpointDraft")
        if session_id is not None:
            session_id = validate_session_id(session_id)
        if git is not None and not isinstance(git, GitSnapshot):
            git = GitSnapshot.model_validate(git)

        capture_ids = list(verification_capture_ids)
        if len(capture_ids) > MAX_VERIFICATION_CAPTURES:
            raise ValueError(
                f"at most {MAX_VERIFICATION_CAPTURES} verification captures "
                "may be attached to one checkpoint"
            )
        if any(not isinstance(capture_id, str) or not capture_id for capture_id in capture_ids):
            raise ValueError("verification capture ids must be non-empty strings")
        if len(set(capture_ids)) != len(capture_ids):
            raise ValueError("verification capture ids must be unique")

        if git is None:
            git = self._capture_git_safe()

        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                captures = self._load_captures(conn, capture_ids, task_id=task_id)
                self._ensure_task(conn, task_id, now)
                if session_id is not None:
                    self._touch_session(conn, session_id, task_id, now)
                if parent_id is not None:
                    self._require_parent(conn, parent_id, task_id)

                next_seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                    "FROM checkpoints WHERE task_id = ?",
                    (task_id,),
                ).fetchone()["next_seq"]

                checkpoint = self._build_checkpoint(
                    task_id=task_id,
                    seq=int(next_seq),
                    draft=draft,
                    session_id=session_id,
                    parent_id=parent_id,
                    git=git,
                    created_at=now,
                    captures=captures,
                )
                self._insert_checkpoint(conn, checkpoint, captures)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        return checkpoint

    def _capture_git_safe(self) -> GitSnapshot:
        try:
            return capture_git_state(self.repo_root)
        except Exception as exc:  # defensive; capture itself should not raise
            return GitSnapshot(
                repo_root=self.repo_root,
                branch=None,
                dirty=None,
                captured_at=_utc_now(),
                capture_error=f"git_unavailable: {exc.__class__.__name__}",
            )

    def _load_captures(
        self,
        conn: sqlite3.Connection,
        capture_ids: Sequence[str],
        *,
        task_id: str | None = None,
    ) -> list[VerificationCapture]:
        if not capture_ids:
            return []

        placeholders = ",".join("?" for _ in capture_ids)
        rows = conn.execute(
            f"""
            SELECT id, task_id, check_key, kind, command, status, exit_code,
                   scope_json, output_excerpt, git_commit, git_branch,
                   git_dirty, git_error, captured_at, coverage, trust
            FROM verification_captures
            WHERE id IN ({placeholders})
            """,
            tuple(capture_ids),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}

        files_by_capture: dict[str, list[EvidenceFileHash]] = {}
        if rows:
            file_rows = conn.execute(
                f"""
                SELECT capture_id, path, sha256
                FROM verification_capture_files
                WHERE capture_id IN ({placeholders})
                ORDER BY capture_id, position
                """,
                tuple(capture_ids),
            ).fetchall()
            for file_row in file_rows:
                files_by_capture.setdefault(file_row["capture_id"], []).append(
                    EvidenceFileHash(
                        path=file_row["path"], sha256=file_row["sha256"]
                    )
                )

        captures: list[VerificationCapture] = []
        for capture_id in capture_ids:
            row = by_id.get(capture_id)
            if row is None:
                raise VerificationCaptureNotFound(
                    f"verification capture not found: {capture_id}"
                )
            if task_id is not None and row["task_id"] != task_id:
                raise LedgerError(
                    f"verification capture {capture_id!r} belongs to another task"
                )
            captures.append(
                VerificationCapture(
                    id=row["id"],
                    task_id=row["task_id"],
                    check_key=row["check_key"],
                    kind=row["kind"],
                    command=row["command"],
                    status=row["status"],
                    exit_code=row["exit_code"],
                    scope=json.loads(row["scope_json"]),
                    output_excerpt=row["output_excerpt"],
                    git_commit=row["git_commit"],
                    git_branch=row["git_branch"],
                    git_dirty=None if row["git_dirty"] is None else bool(row["git_dirty"]),
                    git_error=row["git_error"],
                    captured_at=row["captured_at"],
                    coverage=row["coverage"],
                    trust=row["trust"],
                    files=files_by_capture.get(capture_id, []),
                )
            )
        return captures

    @staticmethod
    def _insert_capture(
        conn: sqlite3.Connection, capture: VerificationCapture
    ) -> None:
        conn.execute(
            """
            INSERT INTO verification_captures (
                id, task_id, check_key, kind, command, status, exit_code,
                scope_json, output_excerpt, git_commit, git_branch, git_dirty,
                git_error, captured_at, coverage, trust
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture.id,
                capture.task_id,
                capture.check_key,
                capture.kind,
                capture.command,
                capture.status,
                capture.exit_code,
                canonical_json(capture.scope),
                capture.output_excerpt,
                capture.git_commit,
                capture.git_branch,
                None if capture.git_dirty is None else int(capture.git_dirty),
                capture.git_error,
                capture.captured_at.isoformat(),
                capture.coverage,
                capture.trust,
            ),
        )
        for position, item in enumerate(capture.files):
            conn.execute(
                """
                INSERT INTO verification_capture_files (
                    capture_id, position, path, sha256
                ) VALUES (?, ?, ?, ?)
                """,
                (capture.id, position, item.path, item.sha256),
            )

    def _ensure_task(
        self, conn: sqlite3.Connection, task_id: str, now: datetime
    ) -> None:
        row = conn.execute(
            "SELECT repo_root FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tasks (id, repo_root, created_at) VALUES (?, ?, ?)",
                (task_id, self.repo_root, now.isoformat()),
            )
            return
        if Path(row["repo_root"]) != Path(self.repo_root):
            raise TaskRepositoryMismatch(
                f"task {task_id!r} is bound to {row['repo_root']!r}, "
                f"not {self.repo_root!r}"
            )

    def _touch_session(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        task_id: str,
        now: datetime,
    ) -> None:
        timestamp = now.isoformat()
        row = conn.execute(
            "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions (id, task_id, client, started_at, last_seen_at) "
                "VALUES (?, ?, NULL, ?, ?)",
                (session_id, task_id, timestamp, timestamp),
            )
            return
        if row["task_id"] != task_id:
            raise LedgerError(
                f"session {session_id!r} belongs to task {row['task_id']!r}"
            )
        # The trigger permits only a real last_seen_at change.
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? "
            "WHERE id = ? AND last_seen_at IS NOT ?",
            (timestamp, session_id, timestamp),
        )

    def _require_parent(
        self, conn: sqlite3.Connection, parent_id: str, task_id: str
    ) -> None:
        row = conn.execute(
            "SELECT task_id FROM checkpoints WHERE id = ?", (parent_id,)
        ).fetchone()
        if row is None:
            raise ParentCheckpointNotFound(
                f"parent checkpoint not found: {parent_id}"
            )
        if row["task_id"] != task_id:
            raise LedgerError(
                f"parent checkpoint {parent_id!r} belongs to another task"
            )

    @staticmethod
    def _build_checkpoint(
        *,
        task_id: str,
        seq: int,
        draft: CheckpointDraft,
        session_id: str | None,
        parent_id: str | None,
        git: GitSnapshot | None,
        created_at: datetime,
        captures: Sequence[VerificationCapture],
    ) -> Checkpoint:
        decisions = [
            PersistedDecision(id=_new_id("dec"), **item.model_dump())
            for item in draft.decisions
        ]
        verification = [
            PersistedEvidence(
                id=_new_id("ev"),
                capture_id=capture.id,
                check_key=capture.check_key,
                kind=capture.kind,
                command=capture.command,
                status=capture.status,
                exit_code=capture.exit_code,
                scope=list(capture.scope),
                output_excerpt=capture.output_excerpt,
                coverage=capture.coverage,
                git_commit=capture.git_commit,
                captured_at=capture.captured_at,
            )
            for capture in captures
        ]
        return Checkpoint(
            id=_new_id("cp"),
            task_id=task_id,
            seq=seq,
            parent_id=parent_id,
            session_id=session_id,
            goal=draft.goal,
            decisions=decisions,
            completed_steps=list(draft.completed_steps),
            changed_files=list(draft.changed_files),
            blockers=list(draft.blockers),
            next_actions=list(draft.next_actions),
            git=git,
            verification=verification,
            created_at=created_at,
        )

    def _insert_checkpoint(
        self,
        conn: sqlite3.Connection,
        checkpoint: Checkpoint,
        captures: Sequence[VerificationCapture],
    ) -> None:
        payload_json = canonical_json(checkpoint.canonical_payload())
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        git = checkpoint.git

        conn.execute(
            """
            INSERT INTO checkpoints (
                id, task_id, seq, parent_id, session_id, goal,
                git_commit, git_branch, git_dirty, git_error,
                payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.id,
                checkpoint.task_id,
                checkpoint.seq,
                checkpoint.parent_id,
                checkpoint.session_id,
                checkpoint.goal,
                git.commit if git else None,
                git.branch if git else None,
                None if git is None or git.dirty is None else int(git.dirty),
                git.capture_error if git else None,
                payload_json,
                payload_sha256,
                checkpoint.created_at.isoformat(),
            ),
        )

        for position, decision in enumerate(checkpoint.decisions):
            conn.execute(
                """
                INSERT INTO decisions (
                    id, checkpoint_id, position, text, rationale, supersedes_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    checkpoint.id,
                    position,
                    decision.text,
                    decision.rationale,
                    decision.supersedes_id,
                    checkpoint.created_at.isoformat(),
                ),
            )

        for position, changed in enumerate(checkpoint.changed_files):
            conn.execute(
                """
                INSERT INTO checkpoint_files (
                    checkpoint_id, position, path, change_type, sha256, size_bytes
                ) VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                (checkpoint.id, position, changed.path, changed.change_type),
            )

        for position, evidence in enumerate(checkpoint.verification):
            self._insert_evidence(
                conn,
                checkpoint.id,
                evidence,
                position,
                captures[position].files,
            )

    @staticmethod
    def _insert_evidence(
        conn: sqlite3.Connection,
        checkpoint_id: str,
        evidence: PersistedEvidence,
        position: int,
        files: Sequence[EvidenceFileHash],
    ) -> None:
        conn.execute(
            """
            INSERT INTO evidence (
                id, checkpoint_id, capture_id, position, check_key, kind, command,
                status, exit_code, scope_json, output_excerpt, git_commit,
                captured_at, coverage, superseded_by, trust
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                evidence.id,
                checkpoint_id,
                evidence.capture_id,
                position,
                evidence.check_key,
                evidence.kind,
                evidence.command,
                evidence.status,
                evidence.exit_code,
                canonical_json(evidence.scope),
                evidence.output_excerpt,
                evidence.git_commit,
                evidence.captured_at.isoformat(),
                evidence.coverage,
                "agent_reported",
            ),
        )
        for file_position, item in enumerate(files):
            conn.execute(
                """
                INSERT INTO evidence_files (evidence_id, position, path, sha256)
                VALUES (?, ?, ?, ?)
                """,
                (evidence.id, file_position, item.path, item.sha256),
            )

    # ------------------------------------------------------------------ links

    def supersede_evidence(
        self, evidence_id: str, superseded_by: str
    ) -> EvidenceRecord:
        """Link an older evidence row to a newer one within the same task.

        The database enforces the same rules; this method exists so future MCP
        tools get a clear error instead of a raw IntegrityError.
        """

        self._require_initialized()
        if not evidence_id or not superseded_by:
            raise ValueError("evidence ids must not be empty")
        if evidence_id == superseded_by:
            raise SupersessionError("evidence cannot supersede itself")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute(
                    """
                    SELECT e.id, e.checkpoint_id, e.superseded_by, c.task_id
                    FROM evidence e
                    JOIN checkpoints c ON c.id = e.checkpoint_id
                    WHERE e.id = ?
                    """,
                    (evidence_id,),
                ).fetchone()
                new = conn.execute(
                    """
                    SELECT e.id, c.task_id
                    FROM evidence e
                    JOIN checkpoints c ON c.id = e.checkpoint_id
                    WHERE e.id = ?
                    """,
                    (superseded_by,),
                ).fetchone()

                if old is None:
                    raise LedgerError(f"evidence not found: {evidence_id}")
                if new is None:
                    raise LedgerError(f"evidence not found: {superseded_by}")
                if old["task_id"] != new["task_id"]:
                    raise SupersessionError(
                        "evidence supersession must stay within one task"
                    )
                if old["superseded_by"] is not None:
                    raise SupersessionError(
                        f"evidence {evidence_id!r} is already superseded"
                    )

                conn.execute(
                    "UPDATE evidence SET superseded_by = ? WHERE id = ?",
                    (superseded_by, evidence_id),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise SupersessionError(str(exc)) from exc
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            checkpoint_id = old["checkpoint_id"]

        for record in self.list_evidence(checkpoint_id=checkpoint_id):
            if record.id == evidence_id:
                return record
        raise CorruptLedger(f"evidence disappeared after supersession: {evidence_id}")

    # ------------------------------------------------------------------- read

    def get_verification_capture(self, capture_id: str) -> VerificationCapture:
        self._require_initialized()
        if not capture_id:
            raise ValueError("capture_id must not be empty")
        with self._connect() as conn:
            return self._load_captures(conn, [capture_id])[0]

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        self._require_initialized()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, payload_sha256 FROM checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                raise CheckpointNotFound(f"checkpoint not found: {checkpoint_id}")
            return self._load_payload(row)

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        self._require_initialized()
        task_id = validate_task_id(task_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, payload_sha256 FROM checkpoints "
                "WHERE task_id = ? ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return None if row is None else self._load_payload(row)

    def list_checkpoints(
        self,
        task_id: str,
        *,
        limit: int = 20,
        before_seq: int | None = None,
    ) -> list[CheckpointSummary]:
        self._require_initialized()
        task_id = validate_task_id(task_id)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if before_seq is not None and before_seq < 1:
            raise ValueError("before_seq must be >= 1")

        sql = (
            "SELECT c.id, c.task_id, c.seq, c.parent_id, c.goal, c.created_at, "
            "(SELECT COUNT(*) FROM decisions d WHERE d.checkpoint_id = c.id) "
            "AS decision_count, "
            "(SELECT COUNT(*) FROM evidence e WHERE e.checkpoint_id = c.id) "
            "AS evidence_count "
            "FROM checkpoints c WHERE c.task_id = ?"
        )
        params: list[object] = [task_id]
        if before_seq is not None:
            sql += " AND c.seq < ?"
            params.append(before_seq)
        sql += " ORDER BY c.seq DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            CheckpointSummary(
                id=row["id"],
                task_id=row["task_id"],
                seq=row["seq"],
                parent_id=row["parent_id"],
                goal=row["goal"],
                created_at=row["created_at"],
                decision_count=row["decision_count"],
                evidence_count=row["evidence_count"],
            )
            for row in rows
        ]

    def list_evidence(
        self,
        *,
        task_id: str | None = None,
        checkpoint_id: str | None = None,
        include_superseded: bool = True,
        statuses: Sequence[EvidenceStatus] | None = None,
        limit: int = 500,
    ) -> list[EvidenceRecord]:
        """Newest-first evidence rows. See ``list_evidence_page`` for truncation."""

        return self.list_evidence_page(
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            include_superseded=include_superseded,
            statuses=statuses,
            limit=limit,
        ).items

    def list_evidence_page(
        self,
        *,
        task_id: str | None = None,
        checkpoint_id: str | None = None,
        include_superseded: bool = True,
        statuses: Sequence[EvidenceStatus] | None = None,
        limit: int = 500,
    ) -> EvidencePage:
        """Read the newest evidence rows plus the total matching count.

        Ordering is checkpoint sequence descending, then position descending, so
        a capped page contains the newest relevant rows. Supersession/status
        filters are applied before LIMIT and before counting.
        """

        self._require_initialized()
        if task_id is None and checkpoint_id is None:
            raise ValueError("task_id or checkpoint_id is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if task_id is not None:
            task_id = validate_task_id(task_id)

        conditions: list[str] = []
        params: list[object] = []
        if task_id is not None:
            conditions.append("c.task_id = ?")
            params.append(task_id)
        if checkpoint_id is not None:
            conditions.append("e.checkpoint_id = ?")
            params.append(checkpoint_id)
        if not include_superseded:
            conditions.append("e.superseded_by IS NULL")
        if statuses is not None:
            status_values = tuple(statuses)
            if not status_values or any(
                status not in {"passed", "failed", "error"}
                for status in status_values
            ):
                raise ValueError("statuses must contain passed/failed/error values")
            placeholders = ",".join("?" for _ in status_values)
            conditions.append(f"e.status IN ({placeholders})")
            params.extend(status_values)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM evidence e
                    JOIN checkpoints c ON c.id = e.checkpoint_id
                    {where}
                    """,
                    tuple(params),
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT e.id, e.checkpoint_id, e.capture_id, c.task_id,
                       c.seq AS checkpoint_seq,
                       e.check_key, e.kind, e.command, e.status, e.exit_code,
                       e.scope_json, e.output_excerpt, e.git_commit, e.captured_at,
                       e.coverage, e.superseded_by, e.trust
                FROM evidence e
                JOIN checkpoints c ON c.id = e.checkpoint_id
                {where}
                ORDER BY c.seq DESC, e.position DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()

            files_by_evidence: dict[str, list[EvidenceFileHash]] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                file_rows = conn.execute(
                    f"""
                    SELECT evidence_id, path, sha256
                    FROM evidence_files
                    WHERE evidence_id IN ({placeholders})
                    ORDER BY evidence_id, position
                    """,
                    tuple(row["id"] for row in rows),
                ).fetchall()
                for file_row in file_rows:
                    files_by_evidence.setdefault(file_row["evidence_id"], []).append(
                        EvidenceFileHash(
                            path=file_row["path"], sha256=file_row["sha256"]
                        )
                    )

        items = [
            EvidenceRecord(
                id=row["id"],
                checkpoint_id=row["checkpoint_id"],
                task_id=row["task_id"],
                checkpoint_seq=row["checkpoint_seq"],
                capture_id=row["capture_id"],
                check_key=row["check_key"],
                kind=row["kind"],
                command=row["command"],
                status=row["status"],
                exit_code=row["exit_code"],
                scope=json.loads(row["scope_json"]),
                output_excerpt=row["output_excerpt"],
                git_commit=row["git_commit"],
                captured_at=row["captured_at"],
                coverage=row["coverage"],
                superseded_by=row["superseded_by"],
                trust=row["trust"],
                files=files_by_evidence.get(row["id"], []),
            )
            for row in rows
        ]
        return EvidencePage(
            items=items,
            total=total,
            limit=limit,
            truncated=total > len(items),
        )

    def list_decisions(
        self, task_id: str, *, limit: int | None = None
    ) -> list[DecisionRecord]:
        """Read decisions ordered by checkpoint and position.

        ``limit=None`` reads the full task history, which callers must use when
        supersession links can point beyond a display cap.
        """

        self._require_initialized()
        task_id = validate_task_id(task_id)
        if limit is not None and not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000 or None")

        sql = """
            SELECT d.id, d.checkpoint_id, c.task_id, c.seq AS checkpoint_seq,
                   d.position, d.text, d.rationale, d.supersedes_id,
                   d.created_at
            FROM decisions d
            JOIN checkpoints c ON c.id = d.checkpoint_id
            WHERE c.task_id = ?
            ORDER BY c.seq, d.position
        """
        params: tuple[object, ...] = (task_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (task_id, limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            DecisionRecord(
                id=row["id"],
                checkpoint_id=row["checkpoint_id"],
                task_id=row["task_id"],
                checkpoint_seq=row["checkpoint_seq"],
                position=row["position"],
                text=row["text"],
                rationale=row["rationale"],
                supersedes_id=row["supersedes_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def count_checkpoints(self, task_id: str) -> int:
        self._require_initialized()
        task_id = validate_task_id(task_id)
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )

    @staticmethod
    def _load_payload(row: sqlite3.Row) -> Checkpoint:
        payload_json = row["payload_json"]
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise CorruptLedger("stored checkpoint payload does not match its hash")
        try:
            return Checkpoint.model_validate_json(payload_json)
        except ValidationError as exc:
            raise CorruptLedger("stored checkpoint payload is invalid") from exc