from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stateledger.models import (
    ChangedFileInput,
    CheckpointDraft,
    DecisionInput,
    EvidenceInput,
    GitSnapshot,
)
from stateledger.store import LedgerStore, default_db_path


class GitRepo:
    """Tiny helper for real Git-backed tests. Only test code uses it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
            )
        return result

    def init(self) -> None:
        # Explicit branch: do not inherit a machine's init.defaultBranch.
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "stateledger-tests@example.com")
        self.git("config", "user.name", "StateLedger Tests")
        self.git("config", "commit.gpgsign", "false")
        self.write(".gitignore", ".stateledger/\n")
        self.commit("chore: initial commit")

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def remove(self, relative: str) -> None:
        self.git("rm", "-q", relative)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def status(self) -> str:
        return self.git("status", "--porcelain").stdout


@pytest.fixture
def git_repo(tmp_path):
    repo = GitRepo(tmp_path / "project")
    repo.init()
    return repo


@pytest.fixture
def git_repo_factory():
    return GitRepo


@pytest.fixture
def git_store(git_repo):
    ledger = LedgerStore(default_db_path(git_repo.root), git_repo.root)
    ledger.initialize()
    return ledger


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def db_path(repo_root):
    return default_db_path(repo_root)


@pytest.fixture
def store(db_path, repo_root):
    ledger = LedgerStore(db_path, repo_root)
    ledger.initialize()
    return ledger


@pytest.fixture
def git_snapshot():
    return GitSnapshot(
        commit="a" * 40,
        branch="main",
        dirty=False,
        repo_root="C:/repo",
        captured_at=datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc),
        capture_error=None,
    )


@pytest.fixture
def draft():
    return CheckpointDraft(
        goal="Ship the M1 ledger core",
        decisions=[
            DecisionInput(
                text="Keep checkpoints append-only",
                rationale="Corrections must not rewrite the audit trail",
            ),
            DecisionInput(text="Store evidence coverage explicitly"),
        ],
        completed_steps=[
            "Verified the MCP stdio round trip",
            "Chose SQLite WAL with explicit migrations",
        ],
        changed_files=[
            ChangedFileInput(path="src/stateledger/store.py", change_type="added"),
            ChangedFileInput(path="tests/test_store.py", change_type="added"),
            ChangedFileInput(path="src/stateledger/models.py", change_type="modified"),
        ],
        blockers=["Cloud Alan MCP registration remains unverified"],
        next_actions=[
            "Capture Git state server-side in M2",
            "Implement the freshness classifier in M2",
        ],
    )


@pytest.fixture
def verification_result():
    return EvidenceInput(
        check_key="tests:unit",
        kind="test",
        command="pytest -q",
        status="passed",
        exit_code=0,
        scope=["src/stateledger/", "tests/"],
        output_excerpt="42 passed in 3.1s",
    )


@pytest.fixture
def seed_evidence_rows():
    """Insert raw evidence rows so cap tests do not need thousands of Git runs."""

    def seed(store, checkpoint, count: int) -> None:
        rows = [
            (
                f"ev_{index:05d}",
                checkpoint.id,
                None,
                index,
                f"check:{index}",
                "test",
                "pytest -q",
                "passed",
                0,
                '["src/"]',
                None,
                None,
                "2026-01-01T00:00:00+00:00",
                "scope_only",
                None,
                "agent_reported",
            )
            for index in range(count)
        ]
        conn = sqlite3.connect(str(store.db_path))
        try:
            conn.executemany(
                """
                INSERT INTO evidence (
                    id, checkpoint_id, capture_id, position, check_key, kind,
                    command, status, exit_code, scope_json, output_excerpt,
                    git_commit, captured_at, coverage, superseded_by, trust
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    return seed


@pytest.fixture
def seed_decision_rows():
    """Insert raw decision rows, optionally superseding the oldest from the newest."""

    def seed(store, checkpoint, count: int, *, supersede_last: bool = True) -> None:
        rows = [
            (
                f"dec_{index:05d}",
                checkpoint.id,
                index,
                f"Decision D{index:05d}",
                None,
                (
                    "dec_00000"
                    if supersede_last and index == count - 1 and count > 1
                    else None
                ),
                "2026-01-01T00:00:00+00:00",
            )
            for index in range(count)
        ]
        conn = sqlite3.connect(str(store.db_path))
        try:
            conn.executemany(
                """
                INSERT INTO decisions (
                    id, checkpoint_id, position, text, rationale, supersedes_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    return seed