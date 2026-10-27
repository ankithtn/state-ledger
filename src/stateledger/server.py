"""StateLedger MCP server (stdio only).

Five tools wrap the existing store and context APIs:

- record_verification
- save_checkpoint
- restore_context
- list_checkpoints
- check_evidence

SDK imports live only in this module. Tool failures become MCP tool errors with
short, actionable messages; diagnostics go to stderr. The server never executes
verification commands.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError

from .context import DEFAULT_HISTORY, MAX_HISTORY, build_context_pack
from .evidence import classify_evidence
from .models import (
    ChangedFileInput,
    CheckpointDraft,
    DecisionInput,
    EvidenceKind,
    EvidenceStatus,
    GitSnapshot,
    VerificationDraft,
)
from .store import LedgerError, LedgerStore, default_db_path

TOOL_NAMES = (
    "record_verification",
    "save_checkpoint",
    "restore_context",
    "list_checkpoints",
    "check_evidence",
)

Freshness = Literal["fresh", "potentially_stale", "unknown"]


class GitOut(BaseModel):
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    repo_root: str | None = None
    captured_at: datetime | None = None
    capture_error: str | None = None


class RecordVerificationOut(BaseModel):
    capture_id: str
    task_id: str
    check_key: str
    status: EvidenceStatus
    coverage: Literal["files", "scope_only"]
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    git_error: str | None = None
    captured_at: datetime
    trust: Literal["agent_reported"] = "agent_reported"
    file_count: int
    warnings: list[str] = Field(default_factory=list)


class SaveCheckpointOut(BaseModel):
    checkpoint_id: str
    task_id: str
    seq: int
    parent_id: str | None = None
    session_id: str | None = None
    created_at: datetime
    git: GitOut | None = None
    verification_count: int
    verification_capture_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CheckpointContextOut(BaseModel):
    id: str
    seq: int
    goal: str
    created_at: datetime
    git: GitOut | None = None


class EvidenceReportOut(BaseModel):
    evidence_id: str
    check_key: str
    status: EvidenceStatus
    freshness: Freshness
    reasons: list[str]
    captured_revision: str | None = None
    captured_at: datetime
    ahead_by: int | None = None
    command: str
    rerun_command: str | None = None
    capture_id: str | None = None
    superseded_by: str | None = None


class RestoreContextOut(BaseModel):
    markdown: str
    checkpoint: CheckpointContextOut | None = None
    evidence_report: list[EvidenceReportOut] = Field(default_factory=list)
    drift: list[str] = Field(default_factory=list)
    evidence_total: int = 0
    evidence_omitted: int = 0
    decision_total: int = 0
    decision_omitted: int = 0


class CheckpointItemOut(BaseModel):
    id: str
    task_id: str
    seq: int
    parent_id: str | None = None
    goal: str
    created_at: datetime
    decision_count: int
    evidence_count: int


class ListCheckpointsOut(BaseModel):
    checkpoints: list[CheckpointItemOut] = Field(default_factory=list)


class EvidenceCheckOut(BaseModel):
    evidence_id: str
    checkpoint_id: str
    capture_id: str | None = None
    check_key: str
    status: EvidenceStatus
    freshness: Freshness
    reasons: list[str]
    captured_revision: str | None = None
    captured_at: datetime
    ahead_by: int | None = None
    command: str
    rerun_command: str | None = None
    superseded_by: str | None = None


class CheckEvidenceOut(BaseModel):
    evidence: list[EvidenceCheckOut] = Field(default_factory=list)
    total: int = 0
    returned: int = 0
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


def _fail(exc: Exception) -> NoReturn:
    if isinstance(exc, ToolError):
        raise exc
    if isinstance(exc, ValidationError):
        details = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            details.append(f"{location}: {error['msg']}")
        raise ToolError("Invalid input: " + "; ".join(details)) from exc
    raise ToolError(str(exc)) from exc


def _git_out(git: GitSnapshot | None) -> GitOut | None:
    if git is None:
        return None
    return GitOut(
        commit=git.commit,
        branch=git.branch,
        dirty=git.dirty,
        repo_root=git.repo_root,
        captured_at=git.captured_at,
        capture_error=git.capture_error,
    )


class StateLedgerToolset:
    """Plain-Python tool implementations, registered by ``create_server``."""

    def __init__(self, store: LedgerStore) -> None:
        self.store = store

    def record_verification(
        self,
        task_id: str,
        check_key: str,
        kind: EvidenceKind,
        command: str,
        status: EvidenceStatus,
        scope: Annotated[list[str], Field(min_length=1, max_length=50)],
        exit_code: int | None = None,
        output_excerpt: str | None = None,
    ) -> RecordVerificationOut:
        """Record a verification result now and snapshot Git + scope hashes.

        Call this immediately after observing a check. Trust is always
        agent_reported: the server records the claim, it does not prove the
        command ran.
        """
        try:
            draft = VerificationDraft(
                check_key=check_key,
                kind=kind,
                command=command,
                status=status,
                exit_code=exit_code,
                scope=scope,
                output_excerpt=output_excerpt,
            )
            capture = self.store.record_verification(task_id, draft)
        except (ValidationError, LedgerError, ValueError, OSError) as exc:
            _fail(exc)

        warnings: list[str] = []
        if capture.coverage != "files":
            warnings.append(
                "Coverage is scope_only; this evidence can never be fresh."
            )
        if capture.git_error:
            warnings.append(f"Git capture issue: {capture.git_error}")

        return RecordVerificationOut(
            capture_id=capture.id,
            task_id=capture.task_id,
            check_key=capture.check_key,
            status=capture.status,
            coverage=capture.coverage,
            git_commit=capture.git_commit,
            git_branch=capture.git_branch,
            git_dirty=capture.git_dirty,
            git_error=capture.git_error,
            captured_at=capture.captured_at,
            trust=capture.trust,
            file_count=len(capture.files),
            warnings=warnings,
        )

    def save_checkpoint(
        self,
        task_id: str,
        goal: str,
        decisions: list[DecisionInput] | None = None,
        completed_steps: list[str] | None = None,
        changed_files: list[ChangedFileInput] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
        verification_capture_ids: list[str] | None = None,
        session_id: str | None = None,
        parent_id: str | None = None,
    ) -> SaveCheckpointOut:
        """Save a checkpoint and attach existing verification captures by id.

        This never hashes the working tree. A capture recorded earlier keeps
        its original commit and hashes.
        """
        try:
            draft = CheckpointDraft(
                goal=goal,
                decisions=decisions or [],
                completed_steps=completed_steps or [],
                changed_files=changed_files or [],
                blockers=blockers or [],
                next_actions=next_actions or [],
            )
            checkpoint = self.store.append_checkpoint(
                task_id,
                draft,
                session_id=session_id,
                parent_id=parent_id,
                verification_capture_ids=verification_capture_ids or [],
            )
        except (ValidationError, LedgerError, ValueError, OSError) as exc:
            _fail(exc)

        warnings: list[str] = []
        if checkpoint.git is not None and checkpoint.git.capture_error:
            warnings.append(
                f"Checkpoint Git capture issue: {checkpoint.git.capture_error}"
            )

        return SaveCheckpointOut(
            checkpoint_id=checkpoint.id,
            task_id=checkpoint.task_id,
            seq=checkpoint.seq,
            parent_id=checkpoint.parent_id,
            session_id=checkpoint.session_id,
            created_at=checkpoint.created_at,
            git=_git_out(checkpoint.git),
            verification_count=len(checkpoint.verification),
            verification_capture_ids=[
                item.capture_id
                for item in checkpoint.verification
                if item.capture_id is not None
            ],
            warnings=warnings,
        )

    def restore_context(
        self,
        task_id: str,
        history: Annotated[int, Field(ge=1, le=MAX_HISTORY)] = DEFAULT_HISTORY,
        repo_root: str | None = None,
    ) -> RestoreContextOut:
        """Return a deterministic Markdown context pack for a task.

        Includes the latest goal, captured Git state, drift warnings, current
        decisions, evidence freshness, and recent checkpoint history. Returns a
        helpful no-state pack when the task has no checkpoint.
        """
        try:
            if repo_root is not None:
                requested = Path(repo_root).resolve()
                if requested != Path(self.store.repo_root):
                    raise ToolError(
                        "This server is bound to "
                        f"{self.store.repo_root}; repo_root must match."
                    )
            pack = build_context_pack(self.store, task_id, history=history)
        except ToolError:
            raise
        except (ValidationError, LedgerError, ValueError, OSError) as exc:
            _fail(exc)

        return RestoreContextOut.model_validate(pack.to_dict())

    def list_checkpoints(
        self,
        task_id: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        before_seq: int | None = None,
    ) -> ListCheckpointsOut:
        """List checkpoints newest first, optionally before a sequence number."""
        try:
            summaries = self.store.list_checkpoints(
                task_id, limit=limit, before_seq=before_seq
            )
        except (ValidationError, LedgerError, ValueError) as exc:
            _fail(exc)

        return ListCheckpointsOut(
            checkpoints=[
                CheckpointItemOut(
                    id=summary.id,
                    task_id=summary.task_id,
                    seq=summary.seq,
                    parent_id=summary.parent_id,
                    goal=summary.goal,
                    created_at=summary.created_at,
                    decision_count=summary.decision_count,
                    evidence_count=summary.evidence_count,
                )
                for summary in summaries
            ]
        )

    def check_evidence(
        self,
        task_id: str,
        include_failed: bool = True,
        include_superseded: bool = False,
    ) -> CheckEvidenceOut:
        """Classify evidence freshness. Status remains passed/failed/error.

        When include_failed is false, only passed rows are returned; an error
        is not treated as a pass. Superseded rows are hidden by default.
        """
        try:
            page = self.store.list_evidence_page(
                task_id=task_id,
                include_superseded=include_superseded,
                statuses=None if include_failed else ("passed",),
                limit=1000,
            )
            classification_cache: dict = {}
            verdicts = [
                (
                    record,
                    classify_evidence(
                        record, self.store.repo_root, cache=classification_cache
                    ),
                )
                for record in page.items
            ]
        except (ValidationError, LedgerError, ValueError) as exc:
            _fail(exc)

        evidence = []
        for record, verdict in verdicts:
            evidence.append(
                EvidenceCheckOut(
                    evidence_id=record.id,
                    checkpoint_id=record.checkpoint_id,
                    capture_id=record.capture_id,
                    check_key=record.check_key,
                    status=record.status,
                    freshness=verdict.freshness,
                    reasons=verdict.reasons,
                    captured_revision=(
                        record.git_commit[:12] if record.git_commit else None
                    ),
                    captured_at=record.captured_at,
                    ahead_by=verdict.ahead_by,
                    command=record.command,
                    rerun_command=(
                        record.command
                        if verdict.freshness != "fresh"
                        else None
                    ),
                    superseded_by=record.superseded_by,
                )
            )

        warnings = []
        if page.truncated:
            omitted = page.total - len(page.items)
            warnings.append(
                f"Evidence truncated: showing newest {len(page.items)} of "
                f"{page.total} matching rows; {omitted} older row(s) omitted."
            )
        return CheckEvidenceOut(
            evidence=evidence,
            total=page.total,
            returned=len(evidence),
            truncated=page.truncated,
            warnings=warnings,
        )


def create_server(
    repo_root: str | Path,
    db_path: str | Path | None = None,
) -> MCPServer:
    """Create the stdio MCP server bound to one repository ledger."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repository root does not exist: {root}")

    store = LedgerStore(db_path or default_db_path(root), root)
    store.initialize()
    tools = StateLedgerToolset(store)

    server = MCPServer(
        "stateledger",
        instructions=(
            "Persistent, evidence-backed task memory. Record verification when "
            "you observe it; save checkpoints separately; restore a deterministic "
            "context pack in a new session."
        ),
    )
    server.add_tool(
        tools.record_verification,
        name="record_verification",
        description=tools.record_verification.__doc__,
        structured_output=True,
    )
    server.add_tool(
        tools.save_checkpoint,
        name="save_checkpoint",
        description=tools.save_checkpoint.__doc__,
        structured_output=True,
    )
    server.add_tool(
        tools.restore_context,
        name="restore_context",
        description=tools.restore_context.__doc__,
        structured_output=True,
    )
    server.add_tool(
        tools.list_checkpoints,
        name="list_checkpoints",
        description=tools.list_checkpoints.__doc__,
        structured_output=True,
    )
    server.add_tool(
        tools.check_evidence,
        name="check_evidence",
        description=tools.check_evidence.__doc__,
        structured_output=True,
    )
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stateledger",
        description="StateLedger MCP server over stdio",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repository root whose .stateledger/ledger.db is used",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="optional explicit ledger database path",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        server = create_server(args.repo, args.db)
    except Exception as exc:
        print(f"stateledger: failed to start: {exc}", file=sys.stderr)
        return 1

    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["StateLedgerToolset", "create_server", "main", "TOOL_NAMES"]