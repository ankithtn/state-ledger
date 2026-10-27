# StateLedger

Persistent, evidence-backed memory for AI coding agents. Save structured task checkpoints, restore context in a new session, and know which verification evidence is still trustworthy after the code changes.

Local-first. Python + SQLite + Git. No cloud, no vector database.

---

## Status

**M1 and M2 (including the capture/save separation fix) are complete: the ledger, read-only Git capture, verification snapshots, scope hashing, and freshness classifier work and are tested. There is no MCP server or CLI yet.**

| Area | State |
| --- | --- |
| Project brief and implementation plan | Written — `PROJECT_BRIEF.md`, `PLAN.md` |
| Checkpoint contract and validation | **Implemented (M1)** — `models.py` |
| Append-only SQLite store, migrations v1→v3 | **Implemented (M1/M2)** — `store.py`, `migrations/` |
| Dedicated verification capture (`record_verification`) | **Implemented (M2 fix)** — snapshot at report time |
| Git revision capture (read-only, failure-safe) | **Implemented (M2)** — `gitinfo.py` |
| SHA-256 scope hashing + freshness classifier | **Implemented (M2)** — `evidence.py` |
| Evidence supersession hardening (self/cross-task) | **Implemented (M2)** — migration 002 |
| Normalized evidence reporting (`superseded_by`, `capture_id`) | **Implemented (M2)** — `LedgerStore.list_evidence()` |
| Test suite | **106 tests passing** (executed; see below) |
| MCP server and Markdown context pack | Planned (M3, five tools) |
| CLI fallback, demo, Alan wiring | Planned (M4) |
| Frontend, vector search | Not planned for v1 |
| MCP Python SDK | Verified by execution: `mcp==2.3.0`, stdio transport, `MCPServer` API |
| Alan (local engine) MCP registration | Verified in an isolated config: a local stdio server connects; user config merges with Alan's injected config |
| Alan (cloud session) MCP registration | **Not available** — no tool exists; not claimed |

## The problem

Agents forget. Between sessions they lose the goal, the decisions already made, the blockers, and — most dangerously — they keep trusting test results from code that no longer exists in that form. Chat logs are not a durable, queryable artifact, and plain notes carry no link to the revision or file contents they describe.

## What works today

- **Record a verification result at the moment it is observed.** `record_verification()` captures Git state and SHA-256 hashes of every in-scope file, then stores an immutable snapshot with `trust = "agent_reported"`.
- **Save a checkpoint separately.** `append_checkpoint(..., verification_capture_ids=[...])` attaches snapshots by id. It never re-hashes the tree, so a result from state A keeps A's commit and hashes even when the checkpoint is saved at state B.
- **Persist and restore every section** through the Python API, with the canonical payload hash verified on read.
- **Capture checkpoint Git state** before each save, using read-only `git` calls with optional locks disabled. Git timeouts or failures become `capture_error` values; they do not crash the save.
- **Classify freshness** against the current tree without ever downgrading a pass to a failure or upgrading a failure to a pass.
- **Keep history append-only**: corrections add a checkpoint with `parent_id`; evidence supersession is one-way, non-self, and cannot cross tasks.

```python
from pathlib import Path

from stateledger.evidence import classify_evidence
from stateledger.models import CheckpointDraft, EvidenceInput
from stateledger.store import LedgerStore, default_db_path

root = Path(".").resolve()
ledger = LedgerStore(default_db_path(root), root)
ledger.initialize()

# Phase 1: record the result now, at the current revision.
capture = ledger.record_verification(
    "stateledger",
    EvidenceInput(
        check_key="tests:unit",
        kind="test",
        command="pytest -q",
        status="passed",
        exit_code=0,
        scope=["src/stateledger/", "tests/"],
    ),
)

# Phase 2: save a checkpoint later. No hashing happens here.
checkpoint = ledger.append_checkpoint(
    "stateledger",
    CheckpointDraft(goal="Ship the capture/save split"),
    verification_capture_ids=[capture.id],
)

record = ledger.list_evidence(checkpoint_id=checkpoint.id)[0]
verdict = classify_evidence(record, root)
print(capture.git_commit, record.git_commit)  # same snapshot commit
print(verdict.status, verdict.freshness, verdict.reasons)
# e.g. passed fresh []          (before further edits)
#      passed potentially_stale ['commit_touched_scope', 'file_modified']
```

**Why two phases.** Hashing during checkpoint save would attach later code to an earlier result. The regression tests record at A, change to B, then save the checkpoint: StateLedger reports `potentially_stale` and never claims B was verified by A. A capture taken at B is `fresh`.

### Freshness rules

`status` and `freshness` are independent:

| Saved status | Freshness | Meaning |
| --- | --- | --- |
| passed | fresh | All captured hashes match; commit distance, diff, and status all succeeded and show no scoped drift |
| passed | potentially_stale | Was passing; code changed — re-run `<command>` |
| passed | unknown | Missing repo, no revision, or rewritten history |
| failed | fresh | Known failure at the current revision |
| failed | potentially_stale | Failed earlier; may now be fixed — re-run |
| failed | unknown | Historical failure, relevance unknown |
| error | any | The check could not run; not evidence of correctness |

Reasons are explicit: `file_modified`, `file_deleted`, `file_added_in_scope`, `commit_touched_scope`, `uncommitted_changes`, `coverage_incomplete`, `git_unavailable`, `repo_missing`, `not_a_git_repository`, `no_revision`, `history_rewritten`.

**Coverage is a precondition, not a hint.** `scope_only` evidence, empty captured file sets, and incomplete current enumeration are always `potentially_stale` or `unknown` — never `fresh`. No fresh verdict exists without complete captured hashes that still match.

**Every Git query must succeed for `fresh`.** Commit distance (`rev-list --count`), scoped diff, and scoped status all have to return a usable result. If any of them returns `None`, the verdict carries `git_unavailable` and is `potentially_stale` — including when the other two queries succeed and show no drift.

### Evidence supersession and reporting

- The database rejects a self-link and a link to evidence belonging to another task (migration 002 triggers). `LedgerStore.supersede_evidence()` adds actionable errors.
- `superseded_by` is mutable bookkeeping and is **not** stored in the immutable checkpoint payload.
- Reports must read normalized tables: `LedgerStore.list_evidence()` returns `EvidenceRecord` with `capture_id`, `superseded_by`, the original commit, and file hashes. `get_verification_capture()` returns the original snapshot. Parsing `payload_json` for supersession or hashes is wrong by design.

### Identifiers and versions

- IDs are `cp_…`, `dec_…`, `ev_…`, `vc_…` + 32 random hex chars from `uuid.uuid4().hex`. They are **not sortable** and not UUIDv7.
- Ordering is per-task `seq`, allocated inside one `BEGIN IMMEDIATE` transaction.
- Checkpoint payload schema is `1`; SQLite schema is `3` (`PRAGMA user_version`). Migration 003 added capture tables and an `evidence.capture_id` column without changing the payload contract.

### Running the tests

Executed on Windows with Python 3.11.3, pytest 8.3.4, Pydantic 2.12.5:

```powershell
python -m pytest
```

Observed result: `106 passed in 57.88s`. The suite is offline and uses temporary Git repositories and temp ledgers. It covers:

- Round-trip persistence, normalized-table integrity, migration idempotency, v1/v2→v3 upgrades (including a v2 ledger with existing evidence), atomic migration rollback, newer-schema rejection.
- Monotonic per-task sequences, correction history, injected mid-write rollback, append-only UPDATE/DELETE on every table.
- Self-link and cross-task supersession rejection at both database and API layers; reporting from normalized evidence.
- Git capture (clean, dirty, unborn, non-repo, missing), timeouts on first and later commands, commit existence/distance, scoped diff/status, and a commit-distance failure that must not yield fresh.
- **Capture/save regression:** result at A + checkpoint at B stays stale, never fresh; capture at B is fresh; saving never calls the hasher.
- Freshness: modified, deleted, unrelated commit, dirty in/out of scope, new in-scope file, `scope_only`, empty file set, missing repo, non-repo, rewritten history, failed-stays-failed, failed-can-be-stale, error-stays-error.

## Proposed setup

M1/M2 require Python 3.11 and Pydantic v2 only; the MCP SDK is an optional extra until M3.

```powershell
# Proposed for a clean checkout. Not executed in this workspace.
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

`pyproject.toml` declares `pydantic>=2.12,<3` at runtime, `mcp==2.3.0` under the `mcp` extra, and `pytest>=8.3` under `dev`. A wheel built with `uv build` was inspected and contains all three migrations plus `gitinfo.py` and `evidence.py`.

## Architecture

```
Agent / MCP client                  (M3)
        │  MCP over stdio
        ▼
stateledger server                  (M3 — not built)
        ├── store.py     SQLite WAL, append-only tables, capture/save  (M1/M2 — built)
        ├── models.py    Pydantic contract + read models               (M1/M2 — built)
        ├── gitinfo.py   read-only Git capture, diff, status           (M2 — built)
        ├── evidence.py  SHA-256 scope hashing + freshness             (M2 — built)
        └── context.py   Markdown context pack                         (M3)
        │
        ▼
<repo>/.stateledger/ledger.db       (ignored by convention)
```

Design choices, all deliberate:

- **Capture and save are separate.** Hashing belongs to the observation, not to the checkpoint write.
- **stdio, not HTTP,** for v1: no port, no auth, no firewall.
- **SQLite via stdlib `sqlite3`,** not an ORM: one file, WAL, explicit `PRAGMA user_version` migrations.
- **Git CLI, not a library:** read-only, no `GitPython`, `GIT_OPTIONAL_LOCKS=0`; failures never crash a save.
- **Canonical JSON plus normalized tables:** the payload is the immutable snapshot; normalized rows hold mutable state, captures, and hashes.
- **No vector search:** retrieval is exact (`task_id`) plus recency.

## Planned, not implemented

- Five MCP tools: `record_verification`, `save_checkpoint`, `restore_context`, `list_checkpoints`, `check_evidence` (M3).
- Markdown context pack with drift warnings and re-run hints (M3).
- CLI fallback and a 90-second scripted demo (M4).
- `streamable-http` transport (SDK support verified; runtime untested).
- Frontend viewer and semantic search (post-hackathon; explicitly out of v1).

## Known limitations

- No MCP server, context pack, CLI, or demo yet; only the Python API exists.
- Evidence status is agent-reported. The server snapshots Git and hashes, but it cannot prove the command ran.
- `record_verification` must be called when the result is observed. Calling it late creates a new snapshot that honestly describes the later tree; `captured_at` and the captured commit expose that. The server cannot detect an agent narrating an old result.
- Hashing is filesystem-based (untracked files count); symlinked directories downgrade coverage to `scope_only`.
- Bounds: 2,000 files, 5 MB per file, 50 MB total per capture. Exceeding a bound downgrades to `scope_only` (never fresh).
- One ledger is bound to one repository root, assumed to be the Git top level; running from a subdirectory would make Git-relative paths and hashed paths diverge.
- A decision's `supersedes_id` is stored as an opaque reference; existence is not checked.
- Supersession does not require the target to be newer, only same-task, non-self, and one-time.
- Concurrency assumes a single writer with WAL and `busy_timeout`; there is no cross-process coordination beyond SQLite locking.
- Test runtime is about 58 seconds because the classifier spawns several Git processes per case on Windows.

## Non-goals

- Hosted sync, accounts, multi-tenancy.
- Running tests or executing agent commands server-side.
- Replacing Git, issues, or CI.
- Understanding code semantics or judging whether a claim is complete.
- Editing or rewriting past checkpoints.

## Verified environment (2026-10-26)

| Check | Result |
| --- | --- |
| Python / pip / uv / Git | 3.11.3 / 25.1 / 0.7.19 / 2.43.0 |
| SQLite (stdlib) | 3.40.1, WAL + JSON1 + FTS5 available |
| Pytest / Pydantic | 8.3.4 / 2.12.5 |
| Full suite | `106 passed in 57.88s` (`python -m pytest`) |
| Wheel build | `uv build --wheel` succeeded; all migrations and M2 modules included |
| `mcp` on PyPI | 2.3.0; `Requires-Python >=3.10`; server transports stdio/SSE/streamable-HTTP |
| SDK API | 2.x removed `FastMCP`; `mcp.server.mcpserver.MCPServer` is the current class |
| stdio round trip | Probe server listed its `echo` tool and returned `echo:hello` through the SDK client |
| Alan engine | OpenCode 1.19.3; `opencode mcp` command present |
| Alan's own tools | Remote MCP server `https://api.tryalan.ai/mcp`, connected |
| Local MCP registration | Verified in an isolated config dir: `✓ stateledger_probe connected` |

## Documentation

- `PROJECT_BRIEF.md` — problem, users, differentiator, scope, non-goals.
- `PLAN.md` — architecture, schema, capture lifecycle, classifier algorithm, milestones, acceptance criteria, risks.

## Next step

**M3 — MCP server + context pack**, after review. Wrap the store in five tools over stdio, render the context pack, and prove a fresh-process restore through an SDK client.
