from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from stateledger.models import CheckpointDraft, EvidenceInput
from stateledger.store import (
    CheckpointNotFound,
    LedgerError,
    LedgerStore,
    MigrationError,
    ParentCheckpointNotFound,
    SupersessionError,
    TaskRepositoryMismatch,
    default_db_path,
)


def _scalar(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _capture(store, task_id="task-a", *, scope=("src/stateledger/", "tests/")):
    return store.record_verification(
        task_id,
        EvidenceInput(
            check_key="tests:unit",
            kind="test",
            command="pytest -q",
            status="passed",
            exit_code=0,
            scope=list(scope),
            output_excerpt="42 passed in 3.1s",
        ),
    )


def _append_with_capture(store, task_id, draft):
    capture = _capture(store, task_id)
    return store.append_checkpoint(
        task_id, draft, verification_capture_ids=[capture.id]
    )


# --------------------------------------------------------------- round trip


def test_round_trip_preserves_every_section(store, draft, git_snapshot, db_path):
    capture = _capture(store)
    saved = store.append_checkpoint(
        "task-a",
        draft,
        session_id="sess-1",
        git=git_snapshot,
        verification_capture_ids=[capture.id],
    )
    restored = store.get_checkpoint(saved.id)

    assert restored == saved
    assert restored.schema_version == 1
    assert restored.seq == 1
    assert restored.session_id == "sess-1"
    assert restored.goal == draft.goal
    assert [item.text for item in restored.decisions] == [
        item.text for item in draft.decisions
    ]
    assert [item.rationale for item in restored.decisions] == [
        item.rationale for item in draft.decisions
    ]
    assert restored.completed_steps == draft.completed_steps
    assert restored.changed_files == draft.changed_files
    assert restored.blockers == draft.blockers
    assert restored.next_actions == draft.next_actions
    assert restored.git == git_snapshot
    assert restored.verification[0].capture_id == capture.id
    assert restored.verification[0].scope == ["src/stateledger/", "tests/"]
    assert restored.verification[0].output_excerpt == "42 passed in 3.1s"
    assert restored.verification[0].git_commit is None

    stored_hash = _scalar(
        db_path, "SELECT payload_sha256 FROM checkpoints WHERE id = ?", (saved.id,)
    )
    assert restored.fingerprint() == stored_hash


def test_evidence_is_stored_with_scope_only_coverage(store, draft, db_path):
    capture = _capture(store)
    checkpoint = store.append_checkpoint(
        "task-a", draft, verification_capture_ids=[capture.id]
    )

    assert checkpoint.verification[0].coverage == "scope_only"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT coverage, scope_json, trust, superseded_by, capture_id "
            "FROM evidence"
        ).fetchone()
    finally:
        conn.close()
    assert row == (
        "scope_only",
        '["src/stateledger/","tests/"]',
        "agent_reported",
        None,
        capture.id,
    )


def test_empty_scope_is_rejected_before_storage(store):
    with pytest.raises(ValidationError):
        EvidenceInput(
            check_key="tests:unit",
            kind="test",
            command="pytest -q",
            status="passed",
            scope=[],
        )
    assert store.count_checkpoints("task-a") == 0


def test_normalized_rows_mirror_the_canonical_payload(store, draft, db_path):
    capture = _capture(store)
    checkpoint = store.append_checkpoint(
        "task-a",
        draft,
        session_id="sess-1",
        verification_capture_ids=[capture.id],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT seq, goal, session_id, parent_id FROM checkpoints WHERE id = ?",
            (checkpoint.id,),
        ).fetchone()
        assert (row["seq"], row["goal"], row["session_id"], row["parent_id"]) == (
            1,
            draft.goal,
            "sess-1",
            None,
        )

        decisions = conn.execute(
            "SELECT position, text, rationale FROM decisions "
            "WHERE checkpoint_id = ? ORDER BY position",
            (checkpoint.id,),
        ).fetchall()
        assert [
            (item["position"], item["text"], item["rationale"])
            for item in decisions
        ] == [
            (position, item.text, item.rationale)
            for position, item in enumerate(checkpoint.decisions)
        ]

        files = conn.execute(
            "SELECT position, path, change_type FROM checkpoint_files "
            "WHERE checkpoint_id = ? ORDER BY position",
            (checkpoint.id,),
        ).fetchall()
        assert [
            (item["position"], item["path"], item["change_type"]) for item in files
        ] == [
            (position, item.path, item.change_type)
            for position, item in enumerate(checkpoint.changed_files)
        ]

        evidence = conn.execute(
            "SELECT checkpoint_id, capture_id, check_key, status, exit_code, "
            "scope_json, coverage FROM evidence WHERE checkpoint_id = ?",
            (checkpoint.id,),
        ).fetchone()
        assert evidence["checkpoint_id"] == checkpoint.id
        assert evidence["capture_id"] == capture.id
        assert evidence["check_key"] == capture.check_key
        assert evidence["status"] == capture.status
        assert evidence["exit_code"] == capture.exit_code
        assert evidence["scope_json"] == '["src/stateledger/","tests/"]'
        assert evidence["coverage"] == "scope_only"
    finally:
        conn.close()


def test_persisted_checkpoint_survives_reopen(store, draft, repo_root):
    saved = store.append_checkpoint("task-a", draft)

    reopened = LedgerStore(store.db_path, repo_root)
    reopened.initialize()

    assert reopened.latest_checkpoint("task-a") == saved
    assert reopened.get_checkpoint(saved.id) == saved


def test_latest_checkpoint_is_none_for_unknown_task(store):
    assert store.latest_checkpoint("missing-task") is None


def test_get_missing_checkpoint_raises(store):
    with pytest.raises(CheckpointNotFound):
        store.get_checkpoint("cp_missing")


# ---------------------------------------------------------------- migrations


def test_migrations_are_idempotent(store, draft, db_path):
    for _ in range(3):
        store.initialize()

    assert _scalar(db_path, "PRAGMA user_version") == 3
    assert _scalar(db_path, "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'") == 20
    assert store.append_checkpoint("task-a", draft).seq == 1


def test_migration_upgrades_existing_v1_ledger(repo_root, db_path):
    import stateledger

    migration = (
        Path(stateledger.__file__).resolve().parent
        / "migrations"
        / "001_init.sql"
    )
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(migration.read_text(encoding="utf-8"))
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()

    ledger = LedgerStore(db_path, repo_root)
    ledger.initialize()

    assert _scalar(db_path, "PRAGMA user_version") == 3
    assert _scalar(db_path, "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'") == 20
    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        conn.close()
    assert {
        "evidence_no_self_supersede",
        "evidence_supersede_same_task",
        "verification_captures_immutable_update",
    } <= names


def test_migration_upgrades_v2_with_existing_evidence(repo_root, db_path):
    import stateledger

    migration_dir = Path(stateledger.__file__).resolve().parent / "migrations"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    try:
        for name in ("001_init.sql", "002_evidence_supersession.sql"):
            conn.executescript((migration_dir / name).read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO tasks (id, repo_root, created_at) VALUES (?, ?, ?)",
            ("task-old", str(repo_root), "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO checkpoints (
                id, task_id, seq, goal, payload_json, payload_sha256, created_at
            ) VALUES ('cp_old', 'task-old', 1, 'old', '{}', 'legacy', ?)
            """,
            ("2026-01-01T00:00:00+00:00",),
        )
        conn.execute(
            """
            INSERT INTO evidence (
                id, checkpoint_id, position, check_key, kind, command, status,
                exit_code, scope_json, output_excerpt, git_commit, captured_at,
                coverage, superseded_by, trust
            ) VALUES (
                'ev_old', 'cp_old', 0, 'legacy', 'test', 'pytest -q', 'passed', 0,
                '["src/"]', NULL, NULL, '2026-01-01T00:00:00+00:00',
                'scope_only', NULL, 'agent_reported'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    ledger = LedgerStore(db_path, repo_root)
    ledger.initialize()

    assert _scalar(db_path, "PRAGMA user_version") == 3
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(evidence)")
        }
        row = conn.execute(
            "SELECT id, status, coverage, capture_id FROM evidence WHERE id = 'ev_old'"
        ).fetchone()
    finally:
        conn.close()
    assert "capture_id" in columns
    assert row == ("ev_old", "passed", "scope_only", None)


def test_newer_schema_version_is_rejected(repo_root, db_path):
    ledger = LedgerStore(db_path, repo_root)
    ledger.initialize()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MigrationError):
        LedgerStore(db_path, repo_root).initialize()


def test_failed_migration_rolls_back_schema_and_version(repo_root, db_path):
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    try:
        # Force the first CREATE TABLE in 001_init.sql to fail.
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MigrationError):
        LedgerStore(db_path, repo_root).initialize()

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'sessions'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_store_requires_initialize(repo_root, db_path, draft):
    ledger = LedgerStore(db_path, repo_root)
    with pytest.raises(LedgerError):
        ledger.append_checkpoint("task-a", draft)


# ----------------------------------------------------------------- sequences


def test_sequence_numbers_are_monotonic(store, draft):
    first = _append_with_capture(store, "task-a", draft)
    second = store.append_checkpoint("task-a", draft)
    third = store.append_checkpoint("task-a", draft)

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert len({first.id, second.id, third.id}) == 3
    assert store.count_checkpoints("task-a") == 3


def test_sequences_are_per_task(store, draft):
    assert store.append_checkpoint("task-a", draft).seq == 1
    assert store.append_checkpoint("task-b", draft).seq == 1
    assert store.append_checkpoint("task-a", draft).seq == 2


def test_list_checkpoints_paginates_newest_first(store, draft):
    capture = _capture(store)
    for _ in range(3):
        store.append_checkpoint(
            "task-a", draft, verification_capture_ids=[capture.id]
        )

    newest = store.list_checkpoints("task-a", limit=2)
    assert [item.seq for item in newest] == [3, 2]
    assert newest[0].decision_count == 2
    assert newest[0].evidence_count == 1

    older = store.list_checkpoints("task-a", limit=10, before_seq=2)
    assert [item.seq for item in older] == [1]


def test_list_checkpoints_validates_limit(store):
    with pytest.raises(ValueError):
        store.list_checkpoints("task-a", limit=0)
    with pytest.raises(ValueError):
        store.list_checkpoints("task-a", limit=101)
    with pytest.raises(ValueError):
        store.list_checkpoints("task-a", before_seq=0)


# ------------------------------------------------------------------ history


def test_correction_creates_new_checkpoint_without_touching_history(
    store, draft
):
    original = store.append_checkpoint("task-a", draft)
    before = store.get_checkpoint(original.id)

    correction = store.append_checkpoint(
        "task-a",
        draft.model_copy(update={"goal": "Corrected goal"}),
        parent_id=original.id,
    )

    assert correction.seq == 2
    assert correction.parent_id == original.id
    assert store.get_checkpoint(original.id) == before
    assert [item.seq for item in store.list_checkpoints("task-a")] == [2, 1]


def test_parent_must_exist_and_same_task(store, draft):
    with pytest.raises(ParentCheckpointNotFound):
        store.append_checkpoint("task-a", draft, parent_id="cp_missing")
    assert store.count_checkpoints("task-a") == 0

    parent = store.append_checkpoint("task-a", draft)
    with pytest.raises(LedgerError):
        store.append_checkpoint("task-b", draft, parent_id=parent.id)


def test_task_cannot_be_rebound_to_another_repository(store, draft, repo_root, tmp_path):
    store.append_checkpoint("task-a", draft)

    other_root = tmp_path / "other-repo"
    other_root.mkdir()
    other = LedgerStore(store.db_path, other_root)
    other.initialize()

    with pytest.raises(TaskRepositoryMismatch):
        other.append_checkpoint("task-a", draft)


# ------------------------------------------------------------- transactions


def test_failed_append_rolls_back_rows_and_sequence(
    store, draft, monkeypatch, db_path
):
    capture = _capture(store)

    def boom(self, conn, checkpoint_id, evidence, position, files):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(LedgerStore, "_insert_evidence", boom)
    with pytest.raises(RuntimeError):
        store.append_checkpoint(
            "task-a", draft, verification_capture_ids=[capture.id]
        )

    assert store.count_checkpoints("task-a") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM tasks") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM verification_captures") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM checkpoints") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM decisions") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM evidence") == 0

    monkeypatch.undo()
    retried = store.append_checkpoint(
        "task-a", draft, verification_capture_ids=[capture.id]
    )
    assert retried.seq == 1


def test_missing_parent_rolls_back_task_row(store, draft, db_path):
    with pytest.raises(ParentCheckpointNotFound):
        store.append_checkpoint("task-a", draft, parent_id="cp_missing")

    assert store.count_checkpoints("task-a") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM tasks WHERE id = 'task-a'") == 0
    assert store.append_checkpoint("task-a", draft).seq == 1


# -------------------------------------------------------------- immutability


def test_checkpoints_cannot_be_updated_or_deleted(store, draft, db_path):
    checkpoint = _append_with_capture(store, "task-a", draft)
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE checkpoints SET goal = 'rewritten' WHERE id = ?",
                (checkpoint.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint.id,))
    finally:
        conn.close()


def test_decisions_cannot_be_updated_or_deleted(store, draft, db_path):
    checkpoint = _append_with_capture(store, "task-a", draft)
    decision_id = checkpoint.decisions[0].id
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE decisions SET text = 'rewritten' WHERE id = ?", (decision_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
    finally:
        conn.close()


def test_checkpoint_files_cannot_be_updated_or_deleted(store, draft, db_path):
    checkpoint = _append_with_capture(store, "task-a", draft)
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE checkpoint_files SET change_type = 'deleted' "
                "WHERE checkpoint_id = ?",
                (checkpoint.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM checkpoint_files WHERE checkpoint_id = ?",
                (checkpoint.id,),
            )
    finally:
        conn.close()


def test_evidence_cannot_be_updated_except_one_time_supersede(store, draft, db_path):
    old = _append_with_capture(store, "task-a", draft)
    new = _append_with_capture(store, "task-a", draft)
    old_evidence = old.verification[0].id
    new_evidence = new.verification[0].id

    conn = sqlite3.connect(str(db_path))
    try:
        # The single permitted mutation.
        conn.execute(
            "UPDATE evidence SET superseded_by = ? WHERE id = ?",
            (new_evidence, old_evidence),
        )
        assert (
            conn.execute(
                "SELECT superseded_by FROM evidence WHERE id = ?", (old_evidence,)
            ).fetchone()[0]
            == new_evidence
        )

        # No second supersede, no field edits, no deletes.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence SET superseded_by = NULL WHERE id = ?",
                (old_evidence,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence SET status = 'failed' WHERE id = ?",
                (old_evidence,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM evidence WHERE id = ?", (old_evidence,))
    finally:
        conn.close()


def test_database_rejects_empty_scope_json(store, draft, db_path):
    checkpoint = _append_with_capture(store, "task-a", draft)
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO evidence (
                    id, checkpoint_id, position, check_key, kind, command, status,
                    exit_code, scope_json, output_excerpt, git_commit, captured_at,
                    coverage, superseded_by, trust
                ) VALUES (
                    'ev_empty', ?, 99, 'empty', 'test', 'pytest -q', 'passed', 0,
                    '[]', NULL, NULL, '2026-01-01T00:00:00+00:00', 'scope_only',
                    NULL, 'agent_reported'
                )
                """,
                (checkpoint.id,),
            )
    finally:
        conn.close()


def test_database_rejects_self_supersession(store, draft, db_path):
    checkpoint = _append_with_capture(store, "task-a", draft)
    evidence_id = checkpoint.verification[0].id

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence SET superseded_by = ? WHERE id = ?",
                (evidence_id, evidence_id),
            )
        assert (
            conn.execute(
                "SELECT superseded_by FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_database_rejects_cross_task_supersession(store, draft, db_path):
    first = _append_with_capture(store, "task-a", draft)
    second = _append_with_capture(store, "task-b", draft)
    first_evidence = first.verification[0].id
    second_evidence = second.verification[0].id

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence SET superseded_by = ? WHERE id = ?",
                (second_evidence, first_evidence),
            )
        assert (
            conn.execute(
                "SELECT superseded_by FROM evidence WHERE id = ?", (first_evidence,)
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_supersede_evidence_api_rejects_invalid_links(store, draft):
    first = _append_with_capture(store, "task-a", draft)
    second = _append_with_capture(store, "task-b", draft)
    first_evidence = first.verification[0].id
    second_evidence = second.verification[0].id

    with pytest.raises(SupersessionError):
        store.supersede_evidence(first_evidence, first_evidence)
    with pytest.raises(SupersessionError):
        store.supersede_evidence(first_evidence, second_evidence)
    with pytest.raises(LedgerError):
        store.supersede_evidence(first_evidence, "ev_missing")


def test_list_evidence_exposes_normalized_superseded_by(store, draft):
    older = _append_with_capture(store, "task-a", draft)
    newer = _append_with_capture(store, "task-a", draft)
    older_evidence = older.verification[0].id
    newer_evidence = newer.verification[0].id

    updated = store.supersede_evidence(older_evidence, newer_evidence)
    assert updated.superseded_by == newer_evidence

    records = {item.id: item for item in store.list_evidence(task_id="task-a")}
    assert records[older_evidence].superseded_by == newer_evidence
    assert records[newer_evidence].superseded_by is None

    active = store.list_evidence(task_id="task-a", include_superseded=False)
    assert [item.id for item in active] == [newer_evidence]

    # The immutable payload intentionally has no supersession field.
    payload_evidence = store.get_checkpoint(older.id).verification[0]
    assert "superseded_by" not in payload_evidence.model_dump()

    with pytest.raises(SupersessionError):
        store.supersede_evidence(older_evidence, newer_evidence)


def test_evidence_files_cannot_be_updated_or_deleted(store, draft, db_path):
    checkpoint = _append_with_capture(store, "task-a", draft)
    evidence_id = checkpoint.verification[0].id

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO evidence_files (evidence_id, position, path, sha256) "
            "VALUES (?, 0, 'src/a.py', NULL)",
            (evidence_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence_files SET path = 'src/b.py' WHERE evidence_id = ?",
                (evidence_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM evidence_files WHERE evidence_id = ?", (evidence_id,)
            )
    finally:
        conn.close()


def test_sessions_only_allow_touching_last_seen(store, draft, db_path):
    store.append_checkpoint("task-a", draft, session_id="sess-1")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE sessions SET last_seen_at = '2099-01-01T00:00:00+00:00' "
            "WHERE id = 'sess-1'"
        )
        assert (
            conn.execute(
                "SELECT last_seen_at FROM sessions WHERE id = 'sess-1'"
            ).fetchone()[0]
            == "2099-01-01T00:00:00+00:00"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE sessions SET started_at = '2000-01-01T00:00:00+00:00' "
                "WHERE id = 'sess-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM sessions WHERE id = 'sess-1'")
    finally:
        conn.close()


# ------------------------------------------------------------------- inputs


def test_append_rejects_wrong_types_and_bad_ids(store, repo_root):
    with pytest.raises(TypeError):
        store.append_checkpoint("task-a", {"goal": "not a model"})
    with pytest.raises(ValueError):
        store.append_checkpoint("Bad Task", CheckpointDraft(goal="g"))
    with pytest.raises(ValueError):
        store.append_checkpoint(
            "task-a", CheckpointDraft(goal="g"), session_id="bad session"
        )


def test_default_db_path_is_repository_local(repo_root):
    assert default_db_path(repo_root) == repo_root.resolve() / ".stateledger" / "ledger.db"