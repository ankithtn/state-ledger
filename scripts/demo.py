"""End-to-end StateLedger demo.

    .venv\\Scripts\\python.exe scripts/demo.py

Uses a disposable Git repository and ledger. No network and no external
services. The sequence is: capture a pass at A, mutate/commit to B, show the
old pass is stale, run the same real check at B, capture it, save a new
checkpoint, and show fresh evidence alongside the preserved old result.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from stateledger.context import build_context_pack
from stateledger.models import CheckpointDraft, DecisionInput, VerificationDraft
from stateledger.store import LedgerStore, default_db_path

TASK_ID = "m4-demo"
# Keep the recorded command identical to the interpreter used for execution.
CHECK_COMMAND = f'"{sys.executable}" check.py'


@dataclass(frozen=True)
class DemoResult:
    before_status: str
    before_freshness: str
    before_reasons: tuple[str, ...]
    after_status: str
    after_freshness: str
    after_reasons: tuple[str, ...]
    old_evidence_still_present: bool
    checkpoints: int
    temp_dir: Path
    temp_removed: bool
    duration_seconds: float


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _run_check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "check.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _force_rmtree(path: Path) -> None:
    def on_error(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=on_error)


def _emit(title: str, markdown: str) -> None:
    print(f"\n=== {title} ===\n", flush=True)
    print(markdown, flush=True)


def run_demo(*, quiet: bool = False) -> DemoResult:
    started = time.perf_counter()
    temp_root = Path(tempfile.mkdtemp(prefix="stateledger-demo-"))
    repo = temp_root / "repo"

    try:
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "stateledger-demo@example.com")
        _git(repo, "config", "user.name", "StateLedger Demo")
        _write(repo / ".gitignore", ".stateledger/\n")
        _write(
            repo / "src" / "app.py",
            "def add():\n    return 1\n",
        )
        _write(
            repo / "check.py",
            "import runpy\n"
            "\n"
            "namespace = runpy.run_path('src/app.py')\n"
            "add = namespace['add']\n"
            "assert callable(add)\n"
            "assert isinstance(add(), int)\n"
            "print('check passed')\n",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "demo: initial state")

        ledger = LedgerStore(default_db_path(repo), repo)
        ledger.initialize()

        first_check = _run_check(repo)
        if first_check.returncode != 0:
            raise RuntimeError(f"initial check failed: {first_check.stderr}")
        first_capture = ledger.record_verification(
            TASK_ID,
            VerificationDraft(
                check_key="demo:app-import",
                kind="test",
                command=CHECK_COMMAND,
                status="passed",
                exit_code=0,
                scope=["src/"],
            ),
        )
        first_checkpoint = ledger.append_checkpoint(
            TASK_ID,
            CheckpointDraft(
                goal="Demonstrate capture-time evidence",
                decisions=[
                    DecisionInput(
                        text="Capture evidence when observed",
                        rationale="A later save must not re-hash newer code",
                    )
                ],
                blockers=[],
                next_actions=["Change the scoped file", "Re-run the check"],
            ),
            verification_capture_ids=[first_capture.id],
        )

        _write(repo / "src" / "app.py", "def add():\n    return 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "demo: change behavior")

        before_pack = build_context_pack(ledger, TASK_ID)
        before_view = next(
            view
            for view in before_pack.evidence
            if view.record.capture_id == first_capture.id
        )
        if not quiet:
            _emit("Restore after mutation, before re-check", before_pack.markdown)

        second_check = _run_check(repo)
        if second_check.returncode != 0:
            raise RuntimeError(f"re-check failed: {second_check.stderr}")

        second_capture = ledger.record_verification(
            TASK_ID,
            VerificationDraft(
                check_key="demo:app-import",
                kind="test",
                command=CHECK_COMMAND,
                status="passed",
                exit_code=0,
                scope=["src/"],
            ),
        )
        second_checkpoint = ledger.append_checkpoint(
            TASK_ID,
            CheckpointDraft(
                goal="Record the re-check against the new state",
                decisions=[
                    DecisionInput(
                        text="Re-ran the same check after the change",
                        rationale="The old result was stale, not failed",
                    )
                ],
                blockers=[],
                next_actions=["Review the restored context"],
            ),
            verification_capture_ids=[second_capture.id],
        )

        after_pack = build_context_pack(ledger, TASK_ID)
        after_view = next(
            view
            for view in after_pack.evidence
            if view.record.capture_id == second_capture.id
        )
        old_view = next(
            view
            for view in after_pack.evidence
            if view.record.capture_id == first_capture.id
        )

        all_evidence = ledger.list_evidence(task_id=TASK_ID, include_superseded=True)
        old_evidence_still_present = any(
            record.id == before_view.record.id for record in all_evidence
        )
        checkpoints = len(ledger.list_checkpoints(TASK_ID, limit=10))
        if not quiet:
            _emit("Restore after re-check and new checkpoint", after_pack.markdown)

        result = DemoResult(
            before_status=before_view.verdict.status,
            before_freshness=before_view.verdict.freshness,
            before_reasons=tuple(before_view.verdict.reasons),
            after_status=after_view.verdict.status,
            after_freshness=after_view.verdict.freshness,
            after_reasons=tuple(after_view.verdict.reasons),
            old_evidence_still_present=old_evidence_still_present,
            checkpoints=checkpoints,
            temp_dir=temp_root,
            temp_removed=False,
            duration_seconds=0.0,
        )
        # Keep the old evidence visible in the demo narrative.
        assert old_view.verdict.freshness == "potentially_stale"
        assert second_checkpoint.seq == 2
        assert first_checkpoint.seq == 1
    finally:
        _force_rmtree(temp_root)

    result = replace(
        result,
        temp_removed=not temp_root.exists(),
        duration_seconds=time.perf_counter() - started,
    )
    if not quiet:
        print("\n=== Result ===", flush=True)
        print(
            f"before: {result.before_status} / {result.before_freshness} "
            f"({', '.join(result.before_reasons)})",
            flush=True,
        )
        print(
            f"after:  {result.after_status} / {result.after_freshness} "
            f"({', '.join(result.after_reasons) or 'none'})",
            flush=True,
        )
        print(
            f"checkpoints: {result.checkpoints}; old evidence preserved: "
            f"{result.old_evidence_still_present}; temp cleaned: "
            f"{result.temp_removed}; elapsed: {result.duration_seconds:.2f}s",
            flush=True,
        )
    return result


def main() -> int:
    try:
        run_demo()
    except Exception as exc:
        print(f"demo failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())