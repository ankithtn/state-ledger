from __future__ import annotations

from stateledger.context import build_context_pack
from stateledger.models import CheckpointDraft, DecisionInput, EvidenceInput


def _repo_with_source(git_repo):
    git_repo.write("src/app.py", "print('one')\n")
    git_repo.write("src/nested/helper.py", "x = 1\n")
    git_repo.commit("feat: source")
    return git_repo


def _capture(store, *, scope=("src/",), status="passed", exit_code=0, command="pytest -q"):
    return store.record_verification(
        "task-a",
        EvidenceInput(
            check_key="tests:unit",
            kind="test",
            command=command,
            status=status,
            exit_code=exit_code,
            scope=list(scope),
        ),
    )


def _save(
    store,
    *,
    goal,
    decisions=None,
    blockers=None,
    next_actions=None,
    captures=None,
):
    return store.append_checkpoint(
        "task-a",
        CheckpointDraft(
            goal=goal,
            decisions=decisions or [],
            blockers=blockers or [],
            next_actions=next_actions or [],
        ),
        verification_capture_ids=[capture.id for capture in captures or []],
    )


def test_context_pack_contains_all_sections(git_store, git_repo):
    _repo_with_source(git_repo)
    capture = _capture(git_store)
    checkpoint = _save(
        git_store,
        goal="Ship the M3 context pack",
        decisions=[DecisionInput(text="Use normalized evidence reads", rationale="Mutable state")],
        blockers=["MCP wiring pending"],
        next_actions=["Add server tests"],
        captures=[capture],
    )

    pack = build_context_pack(git_store, "task-a")
    markdown = pack.markdown

    assert pack.checkpoint is not None
    assert pack.checkpoint.id == checkpoint.id
    assert pack.evidence[0].record.check_key == "tests:unit"
    assert pack.evidence[0].verdict.freshness == "fresh"
    assert "# StateLedger Context: `task-a`" in markdown
    assert "## Goal" in markdown
    assert "Ship the M3 context pack" in markdown
    assert "`seq 1`" in markdown
    assert "Captured revision" in markdown
    assert "`main`" in markdown
    assert "Working tree: clean" in markdown
    assert "MCP wiring pending" in markdown
    assert "Add server tests" in markdown
    assert "Use normalized evidence reads" in markdown
    assert "## Evidence" in markdown
    assert "| tests:unit | passed | fresh |" in markdown
    assert "(not needed)" in markdown
    assert "## Recent checkpoints" in markdown


def test_context_history_is_newest_first_and_limited(git_store, git_repo):
    _repo_with_source(git_repo)
    _save(git_store, goal="Goal Alpha")
    _save(git_store, goal="Goal Beta")
    _save(git_store, goal="Goal Gamma")

    pack = build_context_pack(git_store, "task-a", history=2)

    assert [summary.seq for summary in pack.history] == [3, 2]
    history_block = pack.markdown.split("## Recent checkpoints", 1)[1]
    assert history_block.index("Goal Gamma") < history_block.index("Goal Beta")
    assert "Goal Alpha" not in history_block


def test_context_missing_state_is_helpful(git_store, git_repo):
    _repo_with_source(git_repo)
    pack = build_context_pack(git_store, "empty-task")

    assert pack.checkpoint is None
    assert pack.evidence == ()
    assert pack.history == ()
    assert "No state found for task `empty-task`" in pack.markdown
    assert "record_verification" in pack.markdown
    assert "save_checkpoint" in pack.markdown


def test_context_respects_decision_supersession(git_store, git_repo):
    _repo_with_source(git_repo)
    first = _save(
        git_store,
        goal="First checkpoint",
        decisions=[DecisionInput(text="Approach Alpha", rationale="First idea")],
    )
    second = _save(
        git_store,
        goal="Second checkpoint",
        decisions=[
            DecisionInput(
                text="Approach Beta",
                rationale="Replacement",
                supersedes_id=first.decisions[0].id,
            )
        ],
    )

    pack = build_context_pack(git_store, "task-a")

    assert [decision.text for decision in pack.decisions] == ["Approach Beta"]
    assert pack.decisions[0].checkpoint_id == second.id
    assert pack.superseded_decision_count == 1
    assert "Approach Alpha" not in pack.markdown
    assert "Approach Beta" in pack.markdown
    assert "Superseded decisions omitted: 1" in pack.markdown


def test_context_stale_pass_is_flagged_not_proof(git_store, git_repo):
    _repo_with_source(git_repo)
    capture = _capture(git_store)
    _save(git_store, goal="State A", captures=[capture])

    git_repo.write("src/app.py", "print('two')\n")
    git_repo.commit("fix: change")

    pack = build_context_pack(git_store, "task-a")

    assert len(pack.evidence) == 1
    view = pack.evidence[0]
    assert view.verdict.status == "passed"
    assert view.verdict.freshness == "potentially_stale"
    assert "file_modified" in view.verdict.reasons
    assert "| tests:unit | passed | potentially_stale |" in pack.markdown
    assert "file_modified" in pack.markdown
    assert "`pytest -q`" in pack.markdown
    assert "not current proof" in pack.markdown
    assert any("not fresh" in warning for warning in pack.drift)


def test_context_warns_about_dirty_tree(git_store, git_repo):
    _repo_with_source(git_repo)
    _save(git_store, goal="Clean state")

    git_repo.write("src/app.py", "print('dirty')\n")
    pack = build_context_pack(git_store, "task-a")

    assert any("Working tree is dirty" in warning for warning in pack.drift)
    assert "Working tree is dirty" in pack.markdown


def test_context_history_after_head_moved(git_store, git_repo):
    _repo_with_source(git_repo)
    _save(git_store, goal="State A")
    git_repo.write("README.md", "new docs\n")
    git_repo.commit("docs: update")

    pack = build_context_pack(git_store, "task-a")

    assert any("HEAD moved from" in warning for warning in pack.drift)
    assert "HEAD moved from" in pack.markdown


def test_context_omits_superseded_evidence(git_store, git_repo):
    _repo_with_source(git_repo)
    first = _save(git_store, goal="Old result", captures=[_capture(git_store)])
    second = _save(git_store, goal="New result", captures=[_capture(git_store)])

    git_store.supersede_evidence(
        first.verification[0].id, second.verification[0].id
    )
    pack = build_context_pack(git_store, "task-a")

    assert len(pack.evidence) == 1
    assert pack.superseded_evidence_count == 1
    assert "Superseded evidence rows omitted: 1" in pack.markdown


def test_context_evidence_cap_prefers_newest_and_reports_truncation(
    git_store, git_repo, seed_evidence_rows
):
    _repo_with_source(git_repo)
    checkpoint = _save(git_store, goal="Evidence cap")
    seed_evidence_rows(git_store, checkpoint, 1001)

    page = git_store.list_evidence_page(
        task_id="task-a", include_superseded=False, limit=1000
    )
    assert page.total == 1001
    assert len(page.items) == 1000
    assert page.truncated is True
    assert page.items[0].check_key == "check:1000"
    assert page.items[-1].check_key == "check:1"

    pack = build_context_pack(git_store, "task-a")

    assert pack.evidence_total == 1001
    assert pack.evidence_omitted == 1
    assert pack.evidence[0].record.check_key == "check:1000"
    assert "check:1000" in pack.markdown
    assert "| check:0 |" not in pack.markdown
    assert "Evidence truncated" in pack.markdown
    assert any("Evidence truncated" in warning for warning in pack.drift)


def test_context_decision_cap_uses_full_history_for_supersession(
    git_store, git_repo, seed_decision_rows
):
    _repo_with_source(git_repo)
    checkpoint = _save(git_store, goal="Decision cap")
    seed_decision_rows(git_store, checkpoint, 5002)

    pack = build_context_pack(git_store, "task-a")
    decision_ids = {decision.id for decision in pack.decisions}

    assert "dec_00000" not in decision_ids  # superseded beyond the cap
    assert "dec_00001" not in decision_ids  # omitted by newest-first cap
    assert "dec_05001" in decision_ids
    assert len(pack.decisions) == 5000
    assert pack.decision_total == 5001
    assert pack.decision_omitted == 1
    assert "dec_05001" in pack.markdown
    assert "Decision history truncated" in pack.markdown
    assert any("Decision history truncated" in warning for warning in pack.drift)


def test_context_pack_is_deterministic(git_store, git_repo):
    _repo_with_source(git_repo)
    _save(git_store, goal="Deterministic", captures=[_capture(git_store)])

    first = build_context_pack(git_store, "task-a")
    second = build_context_pack(git_store, "task-a")

    assert first.markdown == second.markdown
    assert first.to_dict() == second.to_dict()