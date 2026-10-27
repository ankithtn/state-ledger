from __future__ import annotations

import pytest
from pydantic import ValidationError

from stateledger.models import (
    ChangedFileInput,
    CheckpointDraft,
    DecisionInput,
    EvidenceInput,
    canonical_json,
    normalize_repo_path,
    sha256_payload,
    validate_session_id,
    validate_task_id,
)


def _evidence(**overrides) -> EvidenceInput:
    values = {
        "check_key": "tests:unit",
        "kind": "test",
        "command": "pytest -q",
        "status": "passed",
        "exit_code": 0,
        "scope": ["src/stateledger/"],
    }
    values.update(overrides)
    return EvidenceInput(**values)


# --------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    "bad_path",
    [
        "/absolute/path.py",
        "C:/windows/path.py",
        "C:\\windows\\path.py",
        "\\\\server\\share",
        "../escape.py",
        "src/../escape.py",
        "src/./file.py",
        "src/*.py",
        "src/[ab].py",
        "",
        "   ",
        "src/\x00null",
    ],
)
def test_scope_rejects_unsafe_paths(bad_path):
    with pytest.raises(ValidationError):
        _evidence(scope=[bad_path])


def test_scope_normalizes_backslashes_and_keeps_directory_marker():
    evidence = _evidence(scope=["src\\stateledger\\", "tests\\test_store.py"])
    assert evidence.scope == ["src/stateledger/", "tests/test_store.py"]


def test_scope_requires_at_least_one_entry():
    with pytest.raises(ValidationError):
        _evidence(scope=[])


def test_scope_rejects_duplicates_after_normalization():
    with pytest.raises(ValidationError):
        _evidence(scope=["src\\a.py", "src/a.py"])


def test_scope_rejects_more_than_fifty_entries():
    with pytest.raises(ValidationError):
        _evidence(scope=[f"src/file_{index}.py" for index in range(51)])


def test_changed_file_rejects_directory_prefix():
    with pytest.raises(ValidationError):
        ChangedFileInput(path="src/stateledger/", change_type="added")


def test_normalize_repo_path_rejects_non_string():
    with pytest.raises(ValueError):
        normalize_repo_path(123, allow_directory_prefix=False)  # type: ignore[arg-type]


# ---------------------------------------------------------------- validation


def test_rejects_empty_goal():
    with pytest.raises(ValidationError):
        CheckpointDraft(goal="   ")


def test_rejects_unknown_status_and_kind():
    with pytest.raises(ValidationError):
        _evidence(status="ok")
    with pytest.raises(ValidationError):
        _evidence(kind="smoke")


def test_rejects_exit_code_out_of_range():
    with pytest.raises(ValidationError):
        _evidence(exit_code=256)
    with pytest.raises(ValidationError):
        _evidence(exit_code=-256)


def test_rejects_duplicate_changed_file_paths():
    with pytest.raises(ValidationError):
        CheckpointDraft(
            goal="duplicate paths",
            changed_files=[
                ChangedFileInput(path="src/a.py", change_type="added"),
                ChangedFileInput(path="src\\a.py", change_type="modified"),
            ],
        )


def test_client_cannot_supply_git_or_server_fields():
    with pytest.raises(ValidationError):
        CheckpointDraft(goal="g", git={"commit": "a" * 40})
    with pytest.raises(ValidationError):
        _evidence(coverage="files")
    with pytest.raises(ValidationError):
        _evidence(id="ev_123")


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("decisions", [DecisionInput(text=f"decision {i}") for i in range(101)]),
        ("completed_steps", [f"step {i}" for i in range(101)]),
        (
            "changed_files",
            [
                ChangedFileInput(path=f"src/file_{i}.py", change_type="added")
                for i in range(201)
            ],
        ),
        ("blockers", [f"blocker {i}" for i in range(51)]),
        ("next_actions", [f"action {i}" for i in range(51)]),
    ],
)
def test_list_limits_enforced(field, values):
    with pytest.raises(ValidationError):
        CheckpointDraft(goal="limits", **{field: values})


def test_accepts_all_supported_enum_values():
    for status in ("passed", "failed", "error"):
        assert _evidence(status=status).status == status
    for kind in ("test", "build", "lint", "typecheck", "manual", "other"):
        assert _evidence(kind=kind).kind == kind
    for change_type in ("added", "modified", "deleted", "renamed"):
        assert (
            ChangedFileInput(path="src/a.py", change_type=change_type).change_type
            == change_type
        )


def test_task_and_session_id_validation():
    assert validate_task_id("stateledger.v1_x") == "stateledger.v1_x"
    with pytest.raises(ValueError):
        validate_task_id("Bad Task")
    with pytest.raises(ValueError):
        validate_task_id("-leading-dash")
    assert validate_session_id("sess-2026-10-26:a") == "sess-2026-10-26:a"
    with pytest.raises(ValueError):
        validate_session_id("bad session")


def test_canonical_json_is_stable_and_order_independent():
    left = {"b": 1, "a": [2, 3]}
    right = {"a": [2, 3], "b": 1}
    assert canonical_json(left) == canonical_json(right) == '{"a":[2,3],"b":1}'
    assert sha256_payload(left) == sha256_payload(right)
    assert len(sha256_payload(left)) == 64