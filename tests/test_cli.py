from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stateledger.models import CheckpointDraft

WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "stateledger", *args],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_cli_restore_prints_only_the_context_pack(git_store, git_repo):
    git_store.append_checkpoint("task-a", CheckpointDraft(goal="CLI restore goal"))

    result = _run_cli(
        "restore",
        "--repo",
        str(git_repo.root),
        "--task-id",
        "task-a",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("# StateLedger Context: `task-a`")
    assert "CLI restore goal" in result.stdout
    assert "## Evidence" in result.stdout


def test_cli_restore_no_state_is_success(repo_root):
    result = _run_cli(
        "restore",
        "--repo",
        str(repo_root),
        "--task-id",
        "empty",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert "No state found for task `empty`" in result.stdout
    assert result.stdout.startswith("# StateLedger Context:")


def test_cli_requires_task_id(repo_root):
    result = _run_cli("restore", "--repo", str(repo_root))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--task-id" in result.stderr


def test_cli_rejects_invalid_history(repo_root):
    result = _run_cli(
        "restore",
        "--repo",
        str(repo_root),
        "--task-id",
        "task-a",
        "--history",
        "0",
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--history" in result.stderr


def test_cli_invalid_task_id_is_usage_error(repo_root):
    result = _run_cli(
        "restore",
        "--repo",
        str(repo_root),
        "--task-id",
        "Bad Task",
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "task_id" in result.stderr


def test_cli_writes_utf8_markdown_without_crashing(git_store, git_repo):
    git_store.append_checkpoint(
        "task-a",
        CheckpointDraft(goal="Café — ✓ context"),
    )

    result = _run_cli(
        "restore",
        "--repo",
        str(git_repo.root),
        "--task-id",
        "task-a",
    )

    assert result.returncode == 0
    assert "Café — ✓ context" in result.stdout
    assert result.stderr == ""


def test_cli_missing_repository_is_unrecoverable(tmp_path):
    missing = tmp_path / "missing"
    result = _run_cli(
        "restore",
        "--repo",
        str(missing),
        "--task-id",
        "task-a",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "does not exist" in result.stderr