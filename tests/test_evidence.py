from __future__ import annotations

import hashlib
import os
import shutil
import stat

import pytest

from stateledger.evidence import classify_evidence, hash_scope
from stateledger.models import CheckpointDraft, EvidenceInput
from stateledger.store import (
    LedgerError,
    LedgerStore,
    VerificationCaptureNotFound,
    default_db_path,
)


def _force_rmtree(path) -> None:
    """Windows Git object files are read-only; clear the bit before unlink."""

    def on_error(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=on_error)


def _capture(
    store: LedgerStore,
    *,
    task_id="task-a",
    scope=("src/",),
    status="passed",
    exit_code=0,
    check_key="tests:unit",
):
    return store.record_verification(
        task_id,
        EvidenceInput(
            check_key=check_key,
            kind="test",
            command="pytest -q",
            status=status,
            exit_code=exit_code,
            scope=list(scope),
        ),
    )


def _attach(store: LedgerStore, capture, *, task_id="task-a", goal="Attach capture"):
    checkpoint = store.append_checkpoint(
        task_id,
        CheckpointDraft(goal=goal),
        verification_capture_ids=[capture.id],
    )
    records = store.list_evidence(checkpoint_id=checkpoint.id)
    assert len(records) == 1
    return checkpoint, records[0]


def _append(store: LedgerStore, **kwargs):
    return _attach(store, _capture(store, **kwargs))


def _repo_with_source(git_repo):
    git_repo.write("src/app.py", "print('one')\n")
    git_repo.write("src/nested/helper.py", "x = 1\n")
    git_repo.commit("feat: add source")
    return git_repo


# ------------------------------------------------------------------- hashing


def test_scope_hashing_records_files_and_hashes(git_store, git_repo):
    _repo_with_source(git_repo)

    _, record = _append(git_store)

    assert record.coverage == "files"
    assert record.git_commit == git_repo.head()
    assert {item.path for item in record.files} == {
        "src/app.py",
        "src/nested/helper.py",
    }
    by_path = {item.path: item.sha256 for item in record.files}
    expected = hashlib.sha256((git_repo.root / "src/app.py").read_bytes()).hexdigest()
    assert by_path["src/app.py"] == expected


def test_hash_scope_excludes_git_and_ledger(git_store, git_repo):
    _repo_with_source(git_repo)
    assert (git_repo.root / ".stateledger/ledger.db").exists()

    result = hash_scope(git_repo.root, ["src/"])

    assert result.complete is True
    assert all(
        not path.startswith(".stateledger/") and not path.startswith(".git/")
        for path in result.files
    )


def test_hash_scope_incomplete_for_missing_and_empty_paths(git_repo):
    (git_repo.root / "empty").mkdir()

    missing_dir = hash_scope(git_repo.root, ["missing/"])
    assert missing_dir.complete is False
    assert "missing_scope_dir" in missing_dir.reasons

    empty_dir = hash_scope(git_repo.root, ["empty/"])
    assert empty_dir.complete is False
    assert "no_files_in_scope" in empty_dir.reasons

    missing_file = hash_scope(git_repo.root, ["src/missing.py"])
    assert missing_file.complete is False
    assert "missing_scope_file" in missing_file.reasons


def test_hash_scope_exact_file(git_repo):
    git_repo.write("src/app.py", "print('one')\n")
    git_repo.commit("feat: add file")

    result = hash_scope(git_repo.root, ["src/app.py"])

    assert result.complete is True
    assert set(result.files) == {"src/app.py"}


def test_hash_changes_with_content(git_repo):
    path = git_repo.write("src/app.py", "one\n")
    first = hash_scope(git_repo.root, ["src/"]).files["src/app.py"]
    path.write_text("two\n", encoding="utf-8", newline="\n")
    second = hash_scope(git_repo.root, ["src/"]).files["src/app.py"]

    assert first != second


# --------------------------------------------------------------- classifier


def test_append_captures_git_and_hashes_automatically(git_store, git_repo):
    _repo_with_source(git_repo)

    checkpoint, record = _append(git_store)

    assert checkpoint.git is not None
    assert checkpoint.git.commit == git_repo.head()
    assert checkpoint.git.dirty is False
    assert checkpoint.verification[0].coverage == "files"
    assert record.git_commit == checkpoint.git.commit
    assert [item.path for item in record.files] == [
        "src/app.py",
        "src/nested/helper.py",
    ]


def test_late_checkpoint_does_not_recapture_old_result(git_store, git_repo):
    _repo_with_source(git_repo)
    capture_a = _capture(git_store)
    commit_a = capture_a.git_commit
    hashes_a = {item.path: item.sha256 for item in capture_a.files}
    assert capture_a.coverage == "files"
    assert capture_a.trust == "agent_reported"

    git_repo.write("src/app.py", "print('two')\n")
    commit_b = git_repo.commit("fix: change behavior")
    assert commit_a != commit_b

    checkpoint, record = _attach(git_store, capture_a, goal="Checkpoint saved at B")

    # Checkpoint Git state is B; the evidence snapshot remains A.
    assert checkpoint.git is not None
    assert checkpoint.git.commit == commit_b
    assert record.git_commit == commit_a
    assert record.capture_id == capture_a.id
    assert {item.path: item.sha256 for item in record.files} == hashes_a

    verdict = classify_evidence(record, git_repo.root)
    assert verdict.status == "passed"
    assert verdict.freshness == "potentially_stale"
    assert "file_modified" in verdict.reasons
    assert "commit_touched_scope" in verdict.reasons


def test_capture_after_change_is_fresh(git_store, git_repo):
    _repo_with_source(git_repo)
    _capture(git_store)  # state A, not attached

    git_repo.write("src/app.py", "print('two')\n")
    commit_b = git_repo.commit("fix: change behavior")
    capture_b = _capture(git_store)
    _, record = _attach(git_store, capture_b)

    assert record.git_commit == commit_b
    verdict = classify_evidence(record, git_repo.root)
    assert verdict.status == "passed"
    assert verdict.freshness == "fresh"
    assert verdict.reasons == []


def test_capture_survives_reopen(git_store, git_repo):
    _repo_with_source(git_repo)
    capture = _capture(git_store)

    reopened = LedgerStore(default_db_path(git_repo.root), git_repo.root)
    reopened.initialize()

    assert reopened.get_verification_capture(capture.id) == capture


def test_append_rejects_unknown_cross_task_duplicate_and_excess_captures(
    git_store, git_repo
):
    _repo_with_source(git_repo)
    capture_a = _capture(git_store, task_id="task-a")
    capture_b = _capture(git_store, task_id="task-b")
    draft = CheckpointDraft(goal="Attach failures")

    with pytest.raises(VerificationCaptureNotFound):
        git_store.append_checkpoint(
            "task-a", draft, verification_capture_ids=["vc_missing"]
        )
    with pytest.raises(LedgerError):
        git_store.append_checkpoint(
            "task-a", draft, verification_capture_ids=[capture_b.id]
        )
    with pytest.raises(ValueError):
        git_store.append_checkpoint(
            "task-a",
            draft,
            verification_capture_ids=[capture_a.id, capture_a.id],
        )
    with pytest.raises(ValueError):
        git_store.append_checkpoint(
            "task-a",
            draft,
            verification_capture_ids=[f"vc_{index}" for index in range(51)],
        )


def test_checkpoint_survives_git_capture_exception(git_store, monkeypatch):
    def boom(_root):
        raise TimeoutError("git timed out")

    monkeypatch.setattr("stateledger.store.capture_git_state", boom)
    checkpoint = git_store.append_checkpoint(
        "task-a", CheckpointDraft(goal="Git unavailable")
    )

    assert checkpoint.git is not None
    assert checkpoint.git.commit is None
    assert checkpoint.git.capture_error is not None
    assert "git_unavailable" in checkpoint.git.capture_error


def test_checkpoint_saves_when_git_status_times_out(git_store, git_repo, monkeypatch):
    import subprocess as sp

    import stateledger.gitinfo as gitinfo

    _repo_with_source(git_repo)
    original = gitinfo._run_git

    def flaky(root, args, **kwargs):
        if args and args[0] == "status":
            raise sp.TimeoutExpired("git", 1)
        return original(root, args, **kwargs)

    monkeypatch.setattr(gitinfo, "_run_git", flaky)
    checkpoint = git_store.append_checkpoint(
        "task-a", CheckpointDraft(goal="Status timed out")
    )

    assert checkpoint.git is not None
    assert checkpoint.git.commit == git_repo.head()
    assert checkpoint.git.dirty is None
    assert checkpoint.git.capture_error == "git_status_failed"


def test_record_verification_survives_git_failure(git_store, git_repo, monkeypatch):
    _repo_with_source(git_repo)

    def boom(_root):
        raise TimeoutError("git timed out")

    monkeypatch.setattr("stateledger.store.capture_git_state", boom)
    capture = _capture(git_store)

    assert capture.git_commit is None
    assert capture.git_error is not None
    assert "git_unavailable" in capture.git_error
    assert capture.coverage == "files"

    _, record = _attach(git_store, capture)
    verdict = classify_evidence(record, git_repo.root)
    assert verdict.status == "passed"
    assert verdict.freshness == "unknown"
    assert verdict.reasons == ["no_revision"]


def test_commit_distance_failure_never_produces_fresh(git_store, git_repo, monkeypatch):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    monkeypatch.setattr("stateledger.evidence.commit_distance", lambda *a, **k: None)
    monkeypatch.setattr("stateledger.evidence.changed_paths", lambda *a, **k: [])
    monkeypatch.setattr("stateledger.evidence.uncommitted_paths", lambda *a, **k: [])
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.status == "passed"
    assert verdict.freshness == "potentially_stale"
    assert "git_unavailable" in verdict.reasons
    assert verdict.ahead_by is None


def test_git_query_failure_never_produces_fresh(git_store, git_repo, monkeypatch):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    monkeypatch.setattr("stateledger.evidence.changed_paths", lambda *a, **k: None)
    monkeypatch.setattr(
        "stateledger.evidence.uncommitted_paths", lambda *a, **k: None
    )
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.status == "passed"
    assert verdict.freshness == "potentially_stale"
    assert "git_unavailable" in verdict.reasons


def test_append_does_not_hash_the_tree(git_store, git_repo, monkeypatch):
    _repo_with_source(git_repo)
    capture = _capture(git_store)

    def boom(*_args, **_kwargs):
        raise AssertionError("append_checkpoint must not hash the tree")

    monkeypatch.setattr("stateledger.store.hash_scope", boom)
    checkpoint, record = _attach(git_store, capture)

    assert checkpoint.seq == 1
    assert record.capture_id == capture.id
    assert record.files == capture.files


def test_current_revision_is_fresh(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "fresh"
    assert verdict.reasons == []
    assert verdict.status == "passed"
    assert verdict.ahead_by == 0


def test_modified_file_is_potentially_stale(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    git_repo.write("src/app.py", "print('two')\n")
    git_repo.commit("fix: change behavior")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "potentially_stale"
    assert verdict.status == "passed"
    assert "file_modified" in verdict.reasons
    assert "commit_touched_scope" in verdict.reasons
    assert verdict.ahead_by == 1


def test_deleted_file_is_potentially_stale(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    git_repo.remove("src/app.py")
    git_repo.commit("refactor: remove app")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "potentially_stale"
    assert "file_deleted" in verdict.reasons
    assert verdict.status == "passed"


def test_unrelated_commit_stays_fresh(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    git_repo.write("README.md", "docs\n")
    git_repo.commit("docs: readme")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "fresh"
    assert verdict.reasons == []
    assert verdict.ahead_by == 1


def test_dirty_scope_file_is_potentially_stale(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    git_repo.write("src/app.py", "print('dirty')\n")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "potentially_stale"
    assert "file_modified" in verdict.reasons
    assert "uncommitted_changes" in verdict.reasons
    assert verdict.status == "passed"


def test_dirty_out_of_scope_stays_fresh(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    git_repo.write("README.md", "dirty docs\n")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "fresh"
    assert verdict.reasons == []


def test_new_file_in_scope_is_potentially_stale(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    git_repo.write("src/new.py", "y = 2\n")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "potentially_stale"
    assert "file_added_in_scope" in verdict.reasons
    assert "uncommitted_changes" in verdict.reasons


def test_missing_repository_is_unknown(git_store, git_repo, tmp_path):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    verdict = classify_evidence(record, tmp_path / "gone")

    assert verdict.freshness == "unknown"
    assert verdict.reasons == ["repo_missing"]
    assert verdict.ahead_by is None


def test_non_git_directory_is_unknown(git_store, git_repo, tmp_path):
    _repo_with_source(git_repo)
    _, record = _append(git_store)
    plain = tmp_path / "plain"
    plain.mkdir()

    verdict = classify_evidence(record, plain)

    assert verdict.freshness == "unknown"
    assert verdict.reasons == ["not_a_git_repository"]
    assert verdict.ahead_by is None


def test_rewritten_history_is_unknown(git_store, git_repo, git_repo_factory):
    _repo_with_source(git_repo)
    _, record = _append(git_store)

    _force_rmtree(git_repo.root / ".git")
    rebuilt = git_repo_factory(git_repo.root)
    rebuilt.init()
    rebuilt.write("src/app.py", "completely different\n")
    rebuilt.commit("rewrite: new history")

    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "unknown"
    assert verdict.reasons == ["history_rewritten"]
    assert verdict.status == "passed"


def test_scope_only_evidence_is_never_fresh(git_store, git_repo):
    (git_repo.root / "empty").mkdir()
    _, record = _append(git_store, scope=("empty/",))

    assert record.coverage == "scope_only"
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.freshness == "potentially_stale"
    assert verdict.reasons == ["coverage_incomplete"]


def test_missing_recorded_files_are_never_fresh(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store)
    incomplete = record.model_copy(update={"files": []})

    verdict = classify_evidence(incomplete, git_repo.root)

    assert verdict.freshness == "potentially_stale"
    assert verdict.reasons == ["coverage_incomplete"]


def test_failed_status_can_also_be_stale(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store, status="failed", exit_code=1)

    git_repo.write("src/app.py", "print('changed')\n")
    git_repo.commit("fix: attempt")
    verdict = classify_evidence(record, git_repo.root)

    assert verdict.status == "failed"
    assert verdict.freshness == "potentially_stale"


def test_failed_status_stays_failed_when_fresh(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store, status="failed", exit_code=1)

    verdict = classify_evidence(record, git_repo.root)

    assert verdict.status == "failed"
    assert verdict.freshness == "fresh"


def test_error_status_stays_error(git_store, git_repo):
    _repo_with_source(git_repo)
    _, record = _append(git_store, status="error", exit_code=None)

    verdict = classify_evidence(record, git_repo.root)

    assert verdict.status == "error"
    assert verdict.freshness == "fresh"