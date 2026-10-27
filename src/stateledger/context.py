"""Deterministic Markdown context packs for StateLedger.

The pack is assembled from the store's normalized read APIs:

- checkpoints (immutable Git/state snapshot),
- decisions (immutable, with supersession links),
- evidence records (normalized, including ``superseded_by`` and ``capture_id``),
- live freshness classification against the current repository.

Mutable state is never read from ``payload_json``. No wall-clock timestamp is
embedded in the Markdown, so the same database and repository state produce the
same pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence import EvidenceVerdict, classify_evidence
from .gitinfo import capture_git_state, commit_distance
from .models import (
    Checkpoint,
    CheckpointSummary,
    DecisionRecord,
    EvidenceRecord,
    GitSnapshot,
    validate_task_id,
)
from .store import LedgerStore

DEFAULT_HISTORY = 5
MAX_HISTORY = 20
MAX_EVIDENCE_ROWS = 1000
MAX_DECISION_ROWS = 5000

FRESH = "fresh"


@dataclass(frozen=True)
class EvidenceView:
    record: EvidenceRecord
    verdict: EvidenceVerdict


@dataclass(frozen=True)
class ContextPack:
    task_id: str
    markdown: str
    checkpoint: Checkpoint | None
    evidence: tuple[EvidenceView, ...]
    history: tuple[CheckpointSummary, ...]
    decisions: tuple[DecisionRecord, ...]
    drift: tuple[str, ...]
    superseded_evidence_count: int
    superseded_decision_count: int
    evidence_total: int = 0
    evidence_omitted: int = 0
    decision_total: int = 0
    decision_omitted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "checkpoint": (
                None
                if self.checkpoint is None
                else {
                    "id": self.checkpoint.id,
                    "seq": self.checkpoint.seq,
                    "goal": self.checkpoint.goal,
                    "created_at": self.checkpoint.created_at.isoformat(),
                    "git": (
                        None
                        if self.checkpoint.git is None
                        else _git_payload(self.checkpoint.git)
                    ),
                }
            ),
            "evidence_report": [_evidence_payload(view) for view in self.evidence],
            "drift": list(self.drift),
            "evidence_total": self.evidence_total,
            "evidence_omitted": self.evidence_omitted,
            "decision_total": self.decision_total,
            "decision_omitted": self.decision_omitted,
        }


def build_context_pack(
    store: LedgerStore,
    task_id: str,
    *,
    history: int = DEFAULT_HISTORY,
    repo_root: str | Path | None = None,
) -> ContextPack:
    """Build a context pack, or a helpful no-state pack when none exists."""

    task_id = validate_task_id(task_id)
    if not 1 <= history <= MAX_HISTORY:
        raise ValueError(f"history must be between 1 and {MAX_HISTORY}")

    root = Path(repo_root or store.repo_root)
    current = capture_git_state(root)
    latest = store.latest_checkpoint(task_id)
    recent = tuple(store.list_checkpoints(task_id, limit=history))

    if latest is None:
        return ContextPack(
            task_id=task_id,
            markdown=_render_no_state(task_id, root, current),
            checkpoint=None,
            evidence=(),
            history=(),
            decisions=(),
            drift=(),
            superseded_evidence_count=0,
            superseded_decision_count=0,
        )

    # Supersession is computed over the full task history, then the display cap
    # is applied. A newer decision beyond the cap must still hide its target.
    decisions = store.list_decisions(task_id)
    superseded_ids = {
        decision.supersedes_id
        for decision in decisions
        if decision.supersedes_id is not None
    }
    current_decisions = sorted(
        (decision for decision in decisions if decision.id not in superseded_ids),
        key=lambda decision: (decision.checkpoint_seq, decision.position),
    )
    decision_omitted = max(0, len(current_decisions) - MAX_DECISION_ROWS)
    displayed_decisions = tuple(
        current_decisions[-MAX_DECISION_ROWS:]
        if decision_omitted
        else current_decisions
    )

    # Current evidence only, newest first. Superseded rows are counted but
    # never consume the display cap.
    current_page = store.list_evidence_page(
        task_id=task_id,
        include_superseded=False,
        limit=MAX_EVIDENCE_ROWS,
    )
    total_all_evidence = store.list_evidence_page(
        task_id=task_id,
        include_superseded=True,
        limit=1,
    ).total
    classification_cache: dict = {}
    evidence_views = tuple(
        EvidenceView(
            record, classify_evidence(record, root, cache=classification_cache)
        )
        for record in current_page.items
    )
    evidence_omitted = current_page.total - len(current_page.items)
    superseded_evidence_count = total_all_evidence - current_page.total

    drift = list(_drift_warnings(latest, current, evidence_views, root))
    if evidence_omitted:
        drift.append(
            f"Evidence truncated: showing newest {len(current_page.items)} of "
            f"{current_page.total} current rows; {evidence_omitted} older row(s) "
            "omitted."
        )
    if decision_omitted:
        drift.append(
            f"Decision history truncated: showing newest "
            f"{len(displayed_decisions)} of {len(current_decisions)} current "
            f"decisions; {decision_omitted} older decision(s) omitted."
        )
    drift_tuple = tuple(drift)

    markdown = _render_pack(
        latest=latest,
        recent=recent,
        decisions=displayed_decisions,
        superseded_decisions=len(decisions) - len(current_decisions),
        decision_total=len(current_decisions),
        decision_omitted=decision_omitted,
        evidence=evidence_views,
        superseded_evidence=superseded_evidence_count,
        evidence_total=current_page.total,
        evidence_omitted=evidence_omitted,
        drift=drift_tuple,
    )

    return ContextPack(
        task_id=task_id,
        markdown=markdown,
        checkpoint=latest,
        evidence=evidence_views,
        history=recent,
        decisions=displayed_decisions,
        drift=drift_tuple,
        superseded_evidence_count=superseded_evidence_count,
        superseded_decision_count=len(decisions) - len(current_decisions),
        evidence_total=current_page.total,
        evidence_omitted=evidence_omitted,
        decision_total=len(current_decisions),
        decision_omitted=decision_omitted,
    )


def _git_payload(git: GitSnapshot) -> dict[str, Any]:
    return {
        "commit": git.commit,
        "branch": git.branch,
        "dirty": git.dirty,
        "repo_root": git.repo_root,
        "captured_at": None if git.captured_at is None else git.captured_at.isoformat(),
        "capture_error": git.capture_error,
    }


def _evidence_payload(view: EvidenceView) -> dict[str, Any]:
    record = view.record
    verdict = view.verdict
    return {
        "evidence_id": record.id,
        "check_key": record.check_key,
        "status": record.status,
        "freshness": verdict.freshness,
        "reasons": list(verdict.reasons),
        "captured_revision": _short(record.git_commit),
        "captured_at": record.captured_at.isoformat(),
        "ahead_by": verdict.ahead_by,
        "command": record.command,
        "rerun_command": (
            record.command if verdict.freshness != FRESH else None
        ),
        "capture_id": record.capture_id,
        "superseded_by": record.superseded_by,
    }


def _short(commit: str | None) -> str | None:
    return None if not commit else commit[:12]


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "(unknown)"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dirty(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "dirty" if value else "clean"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _code(value: str) -> str:
    return "`" + value.replace("`", "'").replace("|", "\\|").replace("\n", " ") + "`"


def _section(lines: list[str], title: str, values: list[str]) -> None:
    lines.extend([f"## {title}"])
    if values:
        lines.extend(f"- {_cell(value)}" for value in values)
    else:
        lines.append("- None.")
    lines.append("")


def _drift_warnings(
    checkpoint: Checkpoint,
    current: GitSnapshot,
    evidence: tuple[EvidenceView, ...],
    repo_root: Path,
) -> list[str]:
    warnings: list[str] = []
    git = checkpoint.git

    if git is None:
        warnings.append("Latest checkpoint has no captured Git state.")
    else:
        if git.capture_error:
            warnings.append(
                f"Latest checkpoint Git capture reported `{git.capture_error}`."
            )
        if git.commit is None:
            warnings.append("Latest checkpoint has no captured commit.")
        elif current.commit is not None and git.commit != current.commit:
            distance = commit_distance(repo_root, git.commit)
            if distance is None:
                warnings.append(
                    "HEAD moved from "
                    f"`{_short(git.commit)}` to `{_short(current.commit)}`; "
                    "commit distance is unavailable."
                )
            else:
                warnings.append(
                    "HEAD moved from "
                    f"`{_short(git.commit)}` to `{_short(current.commit)}` "
                    f"({distance} commit(s) since checkpoint)."
                )
        if git.dirty is True:
            warnings.append(
                "Latest checkpoint was captured with a dirty working tree."
            )

    if current.capture_error:
        warnings.append(
            f"Current Git state is unavailable (`{current.capture_error}`)."
        )
    else:
        if current.commit is None:
            warnings.append("Current repository has no commit.")
        if current.dirty is True:
            warnings.append(
                "Working tree is dirty; uncommitted changes are not covered "
                "unless captured."
            )

    not_fresh = [view for view in evidence if view.verdict.freshness != FRESH]
    if not_fresh:
        warnings.append(
            f"{len(not_fresh)} evidence row(s) are not fresh; stale results are "
            "not current proof."
        )

    return warnings


def _render_no_state(task_id: str, root: Path, current: GitSnapshot) -> str:
    revision = (
        _short(current.commit) if current.commit is not None else "(none)"
    )
    branch = current.branch or "(unknown)"
    lines = [
        f"# StateLedger Context: `{task_id}`",
        "",
        f"No state found for task `{task_id}`.",
        "",
        "## Repository",
        f"- Root: `{root}`",
        f"- Current revision: `{revision}`",
        f"- Branch: `{branch}`",
        f"- Working tree: {_dirty(current.dirty)}",
    ]
    if current.capture_error:
        lines.append(f"- Git capture error: `{current.capture_error}`")
    lines.extend(
        [
            "",
            "## Next step",
            "- Run the checks you need, call `record_verification` for each result, "
            "then call `save_checkpoint`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_pack(
    *,
    latest: Checkpoint,
    recent: tuple[CheckpointSummary, ...],
    decisions: tuple[DecisionRecord, ...],
    superseded_decisions: int,
    decision_total: int,
    decision_omitted: int,
    evidence: tuple[EvidenceView, ...],
    superseded_evidence: int,
    evidence_total: int,
    evidence_omitted: int,
    drift: tuple[str, ...],
) -> str:
    lines = [
        f"# StateLedger Context: `{latest.task_id}`",
        "",
        "## Goal",
        _one_line(latest.goal),
        "",
        f"Latest checkpoint: `seq {latest.seq}` at `{_fmt_dt(latest.created_at)}` "
        f"(`{latest.id}`).",
        "",
        "## Git",
    ]

    git = latest.git
    if git is None:
        lines.append("- Captured Git state: none.")
    else:
        revision = _short(git.commit) if git.commit else "(none)"
        branch = git.branch or "(unknown)"
        lines.extend(
            [
                f"- Captured revision: `{revision}`",
                f"- Branch: `{branch}`",
                f"- Working tree: {_dirty(git.dirty)}",
                f"- Captured at: `{_fmt_dt(git.captured_at)}`",
            ]
        )
        if git.capture_error:
            lines.append(f"- Capture error: `{git.capture_error}`")
    lines.append("")

    _section(lines, "Drift warnings", list(drift) or [])
    _section(lines, "Blockers", list(latest.blockers))
    _section(lines, "Next actions", list(latest.next_actions))

    lines.append("## Decisions")
    if decisions:
        for decision in decisions:
            lines.append(
                f"- **`{decision.id}`**: {_one_line(decision.text)} "
                f"(checkpoint `seq {decision.checkpoint_seq}`)"
            )
            if decision.rationale:
                lines.append(f"  - Rationale: {_one_line(decision.rationale)}")
    else:
        lines.append("- None.")
    if superseded_decisions:
        lines.extend(
            ["", f"Superseded decisions omitted: {superseded_decisions}."]
        )
    if decision_omitted:
        lines.extend(
            [
                "",
                f"Decision history truncated: showing newest {len(decisions)} of "
                f"{decision_total} current decisions; {decision_omitted} older "
                "decision(s) omitted.",
            ]
        )
    lines.append("")

    lines.append("## Evidence")
    if evidence:
        lines.extend(
            [
                "| Check | Status | Freshness | Captured revision | Reasons | Re-run |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for view in evidence:
            record = view.record
            verdict = view.verdict
            captured = _short(record.git_commit) or "(none)"
            reasons = ", ".join(verdict.reasons) if verdict.reasons else "(none)"
            rerun = (
                _code(record.command)
                if verdict.freshness != FRESH
                else "(not needed)"
            )
            lines.append(
                f"| {_cell(record.check_key)} | {record.status} | "
                f"{verdict.freshness} | {_cell(captured)} | "
                f"{_cell(reasons)} | {rerun} |"
            )
        lines.extend(
            [
                "",
                "> Status is what the check reported; freshness is whether it still "
                "applies. Stale results are not current proof.",
            ]
        )
    else:
        lines.append("No current verification evidence recorded.")
    if superseded_evidence:
        lines.extend(
            ["", f"Superseded evidence rows omitted: {superseded_evidence}."]
        )
    if evidence_omitted:
        lines.extend(
            [
                "",
                f"_Showing newest {len(evidence)} of {evidence_total} current "
                f"evidence rows; {evidence_omitted} older row(s) omitted._",
            ]
        )
    lines.append("")

    lines.append("## Recent checkpoints")
    if recent:
        for summary in recent:
            lines.append(
                f"- `seq {summary.seq}`: {_fmt_dt(summary.created_at)}: "
                f"{_one_line(summary.goal)}"
            )
    else:
        lines.append("- None.")
    lines.append("")

    return "\n".join(lines)


__all__ = [
    "ContextPack",
    "EvidenceView",
    "build_context_pack",
    "DEFAULT_HISTORY",
    "MAX_HISTORY",
]