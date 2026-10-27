# StateLedger — Implementation Plan

**Status:** M1 and M2 complete (ledger core, Git capture, verification capture, freshness classification, SQLite schema v3). M3 not started.
**Date:** 2026-10-26
**Companion docs:** `PROJECT_BRIEF.md`, `README.md`

---

## 1. Verified environment

Inspected on this machine, not assumed:

| Item | Observed |
| --- | --- |
| Workspace | `C:\StateLedger` — empty (0 entries), **not** a Git repository |
| Python | 3.11.3 |
| pip | 25.1 |
| uv / uvx | 0.7.19 |
| Git | 2.43.0.windows.1 |
| SQLite (stdlib) | 3.40.1; WAL, JSON1, FTS5 available in a temp-DB probe |
| Pytest | 8.3.4 (system interpreter) |
| Pydantic | 2.12.5 (system interpreter) |
| Missing | `mcp` (not installed system-wide), `sqlite3` CLI, `gh`, `rg` |

Implications: use `sqlite3` from stdlib (no CLI tooling), use a project venv instead of the system interpreter, and do not depend on `rg`/`gh` in scripts.

## 2. MCP SDK — verified findings

Installed `mcp==2.3.0` into an isolated throwaway venv (`%TEMP%\opencode\mcpverify`) and introspected it. Results:

- PyPI publishes `mcp` up to **2.3.0**.
- `Requires-Python: >=3.10`.
- **Server transports:** `stdio`, `sse`, `streamable_http` (plus `mcpserver`, `streamable_http_manager`, auth, etc.).
- **Client transports:** `stdio`, `sse`, `streamable_http`.
- **API change in 2.x:** `mcp.server.fastmcp.FastMCP` no longer exists. It raises `ModuleNotFoundError` with a migration notice; the class is now `mcp.server.mcpserver.MCPServer`, and FastMCP was renamed to MCPServer.
- `MCPServer.run(transport: Literal["stdio","sse","streamable-http"] = "stdio")` and `MCPServer.streamable_http_app()` both exist.
- End-to-end stdio proof: a two-file probe (server with one `echo` tool, client using `StdioServerParameters` + `ClientSession`) produced:
  - `TOOLS: ['echo']`
  - `CALL RESULT: ['echo:hello']`

**Decision:** pin `mcp==2.3.0`, build on `MCPServer`, and keep every SDK import inside `server.py`. Do **not** follow v1 `FastMCP` tutorials. If the SDK regresses, the documented fallback is `mcp<2` with `FastMCP`, isolated to the same module.

## 3. Alan integration — verified findings

The Alan Code harness on this machine is an **OpenCode 1.19.3** engine (`resources\engine\opencode.exe`), and Alan's own tools are delivered to it as a **remote MCP server**:

- `opencode mcp list` → `✓ alan connected` at `https://api.tryalan.ai/mcp` (1 server).
- The resolved config (`opencode debug config`) contains `mcp.alan = { type: "remote", url: "https://api.tryalan.ai/mcp", enabled: true }`.
- Alan injects that config through `OPENCODE_CONFIG_CONTENT`; user config lives at `C:\Users\ankit\.config\opencode` (currently no `opencode.json`).

Custom-server path, tested with the real engine binary in an **isolated** `XDG_CONFIG_HOME` (the user's real config was not modified):

1. Wrote `opencode.json` with:
   `mcp.stateledger_probe = { type: "local", command: ["<venv python>", "<probe_server.py>"], enabled: true }`
2. `opencode mcp list` → `✓ stateledger_probe connected` (and `✓ alan connected`).
3. With both an injected `OPENCODE_CONFIG_CONTENT` and the file config present, `debug config` showed **both** server keys, i.e. file config **merges** with Alan's injected config rather than replacing it.

`opencode mcp add` exists; help exposes `--url`, `--header`, `--env` but **no** non-interactive `--command`, so local servers are registered by editing the config file (or the interactive prompt). Alan exposes **no tool** for adding an MCP server to the current cloud session.

**What is verified:** the bundled engine reads `~/.config/opencode`, connects a local stdio server, and merges user config with Alan's injected config.

**What is not verified:** that a live Alan desktop session picks up a newly added entry without restarting, and any registration path for the hosted cloud agent. Do not claim cloud integration.

**Fallbacks, in order:**

1. **CLI context pack.** `python -m stateledger restore --repo .` prints the same Markdown pack to stdout. Works with any agent that can run a shell command; no MCP client needed.
2. **Standalone MCP client.** The pytest integration test and a small `scripts/mcp_smoke.py` drive the server over stdio, proving protocol correctness independently of Alan.
3. **Remote HTTP transport.** `streamable-http` exists in the SDK but is **untested**; treat as post-M4 stretch, not the demo path.
4. **Other MCP clients** (Claude Desktop, Cursor) accept `command` + `args`; not tested here, so documented as expected, not verified.

## 4. Architecture

```
┌──────────────────────┐        MCP over stdio         ┌──────────────────────────┐
│  Agent / MCP client  │ ────────────────────────────► │  stateledger (Python)    │
│  Alan Code (OpenCode)│ ◄──────────────────────────── │  server.py (5 tools)     │
└──────────────────────┘        JSON results           └───────────┬──────────────┘
                                                                   │
                        ┌──────────────────────────────────────────┼───────────────────────┐
                        │                                          │                       │
                 ┌──────▼──────┐                          ┌────────▼────────┐    ┌─────────▼────────┐
                 │ store.py    │                          │ gitinfo.py      │    │ evidence.py      │
                 │ SQLite WAL  │                          │ git CLI reads   │    │ hashes+freshness │
                 └──────┬──────┘                          └────────┬────────┘    └─────────┬────────┘
                        │                                          │                       │
              .stateledger/ledger.db                        repository (read-only)   .stateledger/ledger.db
```

**Runtime rules**

- One process per client session, launched on demand. No daemon, no port, no migrations service.
- All logging goes to **stderr**; stdout is reserved for MCP frames. This is a hard invariant on Windows stdio.
- DB path: `<repo>/.stateledger/ledger.db` (fixed; no environment override yet). The directory is self-created and gitignored by convention.
- Read-only Git access via `subprocess` (`rev-parse`, `status --porcelain`, `diff --name-only`, `cat-file -e`, `rev-list --count`), with `GIT_OPTIONAL_LOCKS=0`. No `GitPython`, no working-tree writes.
- File hashes are captured by `record_verification` at the moment a result is reported, never by checkpoint saving. SHA-256 over full file bytes; coverage is `files` only when every declared scope path resolves and every file is hashable within limits, otherwise `scope_only`.
- The immutable checkpoint payload is versioned separately from the SQLite schema: payload `schema_version = 1`, database `user_version = 3`.
- Normalized tables hold mutable bookkeeping (`superseded_by`, `last_seen_at`), verification snapshots, and file hashes. Reporting reads those tables, never the stale payload for mutable state.
- Capture and save are separate operations. A checkpoint attaches capture ids and copies the stored snapshot; it never re-hashes the current tree.

## 5. Repository structure

```
C:\StateLedger\
  PROJECT_BRIEF.md
  PLAN.md
  README.md
  pyproject.toml               # project metadata, deps, console scripts
  .gitignore                   # .venv/, .stateledger/, __pycache__/
src\stateledger\
    __init__.py                # version
    __main__.py                # CLI: serve | restore | check | list     (M4)
    server.py                  # MCPServer + 4 tool handlers             (M3)
    models.py                  # Pydantic v2 contract + read models      (M1/M2)
    store.py                   # SQLite, migrations, append-only writes  (M1/M2)
    gitinfo.py                 # read-only Git capture + diffs           (M2)
    evidence.py                # SHA-256 scope hashing + classifier      (M2)
    context.py                 # context-pack + evidence report          (M3)
    migrations\
      001_init.sql
      002_evidence_supersession.sql
      003_verification_captures.sql
  tests\
    conftest.py                # temp repo + temp ledger fixtures
    test_models.py
    test_store.py
    test_gitinfo.py
    test_evidence.py
    test_context.py            # M3
    test_server_stdio.py       # M3, spawns the real server
  scripts\
    demo.py                    # scripted save → mutate → restore story
    mcp_smoke.py               # protocol smoke test without Alan
```

No `src` layout changes later; no second package. `scripts/` is excluded from the wheel.

## 6. Checkpoint schema

### 6.1 Canonical JSON contract (v1)

```json
{
  "schema_version": 1,
  "id": "cp_4f1c...",
  "task_id": "stateledger",
  "seq": 1,
  "parent_id": null,
  "session_id": "s-2026-10-26-a",
  "goal": "Ship checkpoint save/restore with staleness detection",
  "decisions": [
    {
      "id": "dec_9a02...",
      "text": "Use stdio transport only for v1",
      "rationale": "Local-first; no ports or auth in the demo path",
      "supersedes_id": null
    }
  ],
  "completed_steps": [
    "Verified MCP SDK 2.3.0 stdio round trip",
    "Chose SQLite WAL with append-only checkpoints"
  ],
  "changed_files": [
    { "path": "src/stateledger/store.py", "change_type": "added" }
  ],
  "blockers": [
    "No verified way to register an MCP server into a cloud Alan session"
  ],
  "next_actions": [
    "Implement 001_init.sql and store.append_checkpoint",
    "Add append-only trigger tests"
  ],
  "git": {
    "commit": null,
    "branch": null,
    "dirty": null,
    "repo_root": "C:/StateLedger",
    "captured_at": "2026-10-26T00:00:00Z",
    "capture_error": "not_a_git_repository"
  },
  "verification": [
    {
      "id": "ev_71b3...",
      "capture_id": "vc_9d20...",
      "check_key": "tests:unit",
      "kind": "test",
      "command": "pytest -q",
      "status": "passed",
      "exit_code": 0,
      "scope": ["src/stateledger/", "tests/"],
      "output_excerpt": "42 passed in 3.1s",
      "coverage": "files",
      "git_commit": null,
      "captured_at": "2026-10-26T00:00:00Z"
    }
  ],
  "created_at": "2026-10-26T00:00:00Z"
}
```

`id`, `seq`, `parent_id`, `session_id`, `created_at`, evidence `id`/`capture_id`/`coverage`/`git_commit`/`captured_at`, and the Git block are server-owned. The draft a caller submits contains goal, decisions, completed steps, changed files, blockers, and next actions only; verification results are recorded separately and referenced by capture id.

Rules enforced by `models.py`:

- `goal` non-empty, ≤ 2,000 chars; `task_id` matches `^[a-z0-9][a-z0-9._-]{0,63}$`.
- `status ∈ {passed, failed, error}`; `kind ∈ {test, build, lint, typecheck, manual, other}`.
- `scope` must contain **1–50** entries. Each entry is a repo-relative file path or a directory prefix ending in `/`. Absolute paths, drive letters, `..`, `.`, NUL bytes, and glob metacharacters are rejected; backslashes are normalized to `/`.
- Evidence coverage is explicit: `files` when `record_verification` hashed every in-scope file at report time, otherwise `scope_only`. Scope requires 1–50 entries, so missing or empty scope never reaches storage. Hashes live in `verification_capture_files` and the copied `evidence_files`, not in this payload.
- `git`, `payload_sha256`, and evidence file hashes are **server-captured**. Checkpoint Git is captured at save time; evidence Git and hashes are captured by `record_verification`. Client JSON cannot supply either.
- Every list has a size cap so a single checkpoint cannot bloat the pack: 100 decisions, 100 completed steps, 200 changed files, 50 blockers, 50 next actions, 50 verification entries.
- Duplicate changed-file paths and duplicate scope entries are rejected (scope entries are normalized before comparison).

### 6.2 SQLite schema (migrations 001 + 002)

```sql
-- Applied atomically by store.initialize() inside BEGIN IMMEDIATE ... COMMIT.
-- PRAGMA foreign_keys and journal_mode=WAL are set per connection by the store.
PRAGMA user_version = 1;

CREATE TABLE tasks (
  id          TEXT PRIMARY KEY,
  repo_root   TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id),
  client      TEXT,
  started_at  TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE checkpoints (
  id           TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL REFERENCES tasks(id),
  seq          INTEGER NOT NULL CHECK (seq >= 1),
  parent_id    TEXT REFERENCES checkpoints(id),
  session_id   TEXT REFERENCES sessions(id),
  goal         TEXT NOT NULL,
  git_commit   TEXT,
  git_branch   TEXT,
  git_dirty    INTEGER CHECK (git_dirty IS NULL OR git_dirty IN (0, 1)),
  git_error    TEXT,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  payload_sha256 TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (task_id, seq)
);

CREATE TABLE decisions (
  id            TEXT PRIMARY KEY,
  checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
  position      INTEGER NOT NULL CHECK (position >= 0),
  text          TEXT NOT NULL,
  rationale     TEXT,
  supersedes_id TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (checkpoint_id, position)
);

CREATE TABLE checkpoint_files (
  checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
  position      INTEGER NOT NULL CHECK (position >= 0),
  path          TEXT NOT NULL,
  change_type   TEXT NOT NULL CHECK (change_type IN ('added', 'modified', 'deleted', 'renamed')),
  sha256        TEXT,
  size_bytes    INTEGER,
  PRIMARY KEY (checkpoint_id, path),
  UNIQUE (checkpoint_id, position)
);

CREATE TABLE evidence (
  id             TEXT PRIMARY KEY,
  checkpoint_id  TEXT NOT NULL REFERENCES checkpoints(id),
  position       INTEGER NOT NULL CHECK (position >= 0),
  check_key      TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('test', 'build', 'lint', 'typecheck', 'manual', 'other')),
  command        TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'error')),
  exit_code      INTEGER CHECK (exit_code IS NULL OR exit_code BETWEEN -255 AND 255),
  scope_json     TEXT NOT NULL CHECK (
    json_valid(scope_json) AND json_array_length(scope_json) >= 1
  ),
  output_excerpt TEXT,
  git_commit     TEXT,
  captured_at    TEXT NOT NULL,
  coverage       TEXT NOT NULL CHECK (coverage IN ('files', 'scope_only')),
  superseded_by  TEXT REFERENCES evidence(id),
  trust          TEXT NOT NULL DEFAULT 'agent_reported' CHECK (trust IN ('agent_reported', 'server_verified')),
  UNIQUE (checkpoint_id, position)
);

CREATE TABLE evidence_files (
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  position    INTEGER NOT NULL CHECK (position >= 0),
  path        TEXT NOT NULL,
  sha256      TEXT,
  PRIMARY KEY (evidence_id, path),
  UNIQUE (evidence_id, position)
);
```

Immutability is enforced in the database, not only in Python:

```sql
-- Fully immutable: UPDATE and DELETE abort for tasks, checkpoints, decisions,
-- checkpoint_files, evidence_files. Examples:
CREATE TRIGGER checkpoints_immutable_update
BEFORE UPDATE ON checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;

CREATE TRIGGER checkpoints_immutable_delete
BEFORE DELETE ON checkpoints
BEGIN SELECT RAISE(ABORT, 'checkpoints cannot be deleted'); END;

-- evidence: DELETE always aborts; UPDATE is allowed only for a one-time
-- NULL -> non-NULL superseded_by link, with every other column unchanged.
CREATE TRIGGER evidence_supersede_once
BEFORE UPDATE ON evidence
WHEN NOT (
  OLD.superseded_by IS NULL
  AND NEW.superseded_by IS NOT NULL
  AND NEW.id IS OLD.id
  AND NEW.checkpoint_id IS OLD.checkpoint_id
  AND NEW.position IS OLD.position
  AND NEW.check_key IS OLD.check_key
  AND NEW.kind IS OLD.kind
  AND NEW.command IS OLD.command
  AND NEW.status IS OLD.status
  AND NEW.exit_code IS OLD.exit_code
  AND NEW.scope_json IS OLD.scope_json
  AND NEW.output_excerpt IS OLD.output_excerpt
  AND NEW.git_commit IS OLD.git_commit
  AND NEW.captured_at IS OLD.captured_at
  AND NEW.coverage IS OLD.coverage
  AND NEW.trust IS OLD.trust
)
BEGIN SELECT RAISE(ABORT, 'evidence rows are immutable except a one-time superseded_by link'); END;

CREATE TRIGGER evidence_immutable_delete
BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence cannot be deleted'); END;
```

The full migration also creates equivalent triggers for `tasks`, `decisions`, `checkpoint_files`, and `evidence_files`, plus a guarded `sessions` trigger.

Migration `002_evidence_supersession.sql` (`PRAGMA user_version = 2`) hardens the one mutable column without rebuilding the table:

- `evidence_no_self_supersede` aborts `superseded_by = id`;
- `evidence_supersede_same_task` aborts a link whose source and target evidence belong to different tasks.

`LedgerStore.supersede_evidence()` applies the same checks for actionable errors, but the triggers are authoritative. New databases apply 001 then 002; a version-1 ledger upgrades in place.

Migration `003_verification_captures.sql` (`PRAGMA user_version = 3`) separates verification capture from checkpoint saving:

- `verification_captures` stores one immutable snapshot per reported result: status, scope, Git commit/branch/dirty/error, coverage, `trust = 'agent_reported'`, and `captured_at`.
- `verification_capture_files` stores the SHA-256 hashes taken at that moment.
- `evidence` gains `capture_id`; checkpoint evidence rows copy the capture and reference it.
- `evidence_supersede_once` is recreated so `capture_id` is frozen too; capture tables reject UPDATE and DELETE.

New databases apply 001 → 002 → 003; version-1 and version-2 ledgers upgrade in place without losing existing evidence (old rows keep `capture_id = NULL`).

### 6.3 Permitted mutable bookkeeping fields

Only two columns may ever change after insertion:

| Column | Permitted transition | Why |
| --- | --- | --- |
| `sessions.last_seen_at` | any newer timestamp; all other session columns must be unchanged | Track an active session without rewriting its history |
| `evidence.superseded_by` | `NULL → non-NULL` exactly once, never to itself, never to evidence in another task; all other evidence columns must be unchanged | Link a re-run result to the evidence it replaces, while keeping the old row intact |

Everything else is insert-only: `tasks`, `sessions` (apart from `last_seen_at`), `checkpoints`, `decisions`, `checkpoint_files`, `evidence` (apart from `superseded_by`), `evidence_files`, `verification_captures`, and `verification_capture_files`. No table permits DELETE. Corrections create a new checkpoint with `parent_id`; they never overwrite the correction target.

**Reporting contract for mutable state.** The checkpoint payload is the immutable snapshot taken at write time. `superseded_by` is intentionally absent from it, and `sessions.last_seen_at` is not checkpoint data. Any evidence report — M3's `check_evidence`, a CLI listing, or a future UI — must read the `evidence` table joined to `checkpoints`; `LedgerStore.list_evidence()` is that path and returns `EvidenceRecord` with `capture_id`, `superseded_by`, the original commit, and the `evidence_files` hashes. `get_verification_capture()` exposes the original snapshot directly. Parsing `payload_json` for supersession, hashes, or `last_seen_at` is incorrect by design.

### 6.4 Milestone coverage of the contract

| Field or behaviour | M1 | M2 + review fix (current) |
| --- | --- | --- |
| `task_id`, `goal`, decisions, completed steps, changed files, blockers, next actions | Validated and persisted | Unchanged |
| Verification result fields (`check_key`, `kind`, `command`, `status`, `exit_code`, `scope`, `output_excerpt`) | Lived in `CheckpointDraft.verification` | Moved to a separate `VerificationDraft`; captured by `record_verification` |
| Checkpoint-level Git snapshot (`commit`, `branch`, `dirty`, `repo_root`, `captured_at`, `capture_error`) | Persisted only when injected by a test/import | Captured automatically when the checkpoint is saved; the `git=` keyword remains an internal override |
| Evidence Git snapshot | None | Captured at record time, stored on `verification_captures`, and copied to evidence (`git_commit`) |
| Evidence coverage | Always `scope_only`; no hashes | Captured at record time: `files` when hashing completes, otherwise `scope_only` |
| Evidence file hashes | None | `verification_capture_files` at record time, copied to `evidence_files` at attach time; never re-hashed by save |
| Checkpoint payload version | 1 | 1 (additive `capture_id`/`git_commit`; old payloads parse) |
| SQLite schema version | 1 | 3 (captures + self/cross-task supersession guards) |
| `payload_sha256` | Computed at write, verified on read | Unchanged |
| Evidence reads | `get_checkpoint()` payload only | `list_evidence()` + `get_verification_capture()` from normalized rows |
| Freshness classification | Not implemented | `classify_evidence()` against the captured snapshot |
| MCP tools / CLI | Not implemented | Not implemented (M3 / M4) |

`record_verification(task_id, VerificationDraft)` returns a `vc_…` capture after snapshotting Git and scope hashes. `append_checkpoint(..., verification_capture_ids=[...])` attaches those captures, copies their data, and performs **no hashing**. `append_checkpoint(..., git=...)` remains an internal override for tests and imports; client payloads cannot set `git` (`extra="forbid"`).

### 6.5 Identifiers and version numbers

- **IDs.** Checkpoint, decision, evidence, and verification-capture ids are `cp_`, `dec_`, `ev_`, or `vc_` followed by 32 lowercase hex characters generated by `uuid.uuid4().hex`. They are random and **not sortable**. They are not RFC 4122 hyphenated strings and not UUIDv7; no code may order by id.
- **Ordering.** The per-task `seq` column is the only ordering key (`UNIQUE (task_id, seq)`, allocated as `MAX(seq)+1` inside `BEGIN IMMEDIATE`). History pagination uses `seq`. Captures are ordered by `captured_at` for display only.
- **Checkpoint payload version.** `schema_version` inside the checkpoint JSON is `1` (`models.CHECKPOINT_SCHEMA_VERSION`). Additive optional fields (`capture_id`, `git_commit`) do not bump it; incompatible changes do.
- **Database schema version.** `PRAGMA user_version` is `3` (`store.DB_SCHEMA_VERSION`). It changes whenever a migration is added.
- **Why two versions.** The capture tables and triggers are storage changes that leave the payload readable; conflating storage and payload versions would invalidate existing checkpoints for a schema-only change.

### 6.6 Verification capture lifecycle

Verification is now two-phase, and the phases cannot be collapsed:

1. `record_verification(task_id, VerificationDraft)` — called when the result is observed. It captures Git state, hashes the declared scope, writes `verification_captures` + `verification_capture_files`, and returns a `vc_…` id. `trust` is always `agent_reported`; the server records the claim, it does not prove the command ran.
2. `append_checkpoint(task_id, CheckpointDraft, verification_capture_ids=[...])` — called when state is saved. It loads the captures, copies their status/commit/coverage, links the hashes, and writes the checkpoint. It performs no hashing and never touches the working tree for evidence.

**Invariant.** A checkpoint saved at revision B that attaches a capture taken at revision A keeps A's `git_commit`, A's hashes, and A's `captured_at`. Classification compares A's snapshot with the current tree, so B is reported as `potentially_stale`, never as verified by A. The checkpoint's own Git snapshot may be B; it is checkpoint context, not evidence provenance.

Regression coverage: `test_late_checkpoint_does_not_recapture_old_result`, `test_capture_after_change_is_fresh`, and `test_append_does_not_hash_the_tree`.

## 7. Freshness model

Two independent axes, never conflated:

- **`status`** — what happened when the check ran: `passed`, `failed`, `error`.
- **`freshness`** — whether that result still describes the working tree: `fresh`, `potentially_stale`, `unknown`.

| Saved status | Freshness at restore | How it is presented |
| --- | --- | --- |
| passed | fresh | Verified at this revision: captured hashes match, every Git query succeeded, and no scoped drift |
| passed | potentially_stale | Was passing; code changed — re-run `<command>` |
| passed | unknown | Cannot verify relevance (no repo / rewritten history) |
| failed | fresh | Known failure at this revision |
| failed | potentially_stale | Failed at an older revision; may now be fixed — re-run |
| failed | unknown | Historical failure, relevance unknown |
| error | any | Check could not run; not evidence of correctness |

**"Definitively failed" means `status = failed` at a specific commit.** It is never inferred from staleness, and staleness never turns a pass into a failure or vice versa.

**Coverage precondition.** No evidence row may be classified `fresh` unless `coverage = 'files'` **and** every in-scope file recorded with the capture still matches its stored hash. If coverage is `scope_only` (including every row written by M1), if `evidence_files` is empty, if the scope is empty, or if the server cannot enumerate the current scope, the verdict is `potentially_stale` or `unknown` with reason `coverage_incomplete` — never `fresh`. Empty scope is rejected at validation, so the "missing scope" case cannot exist in storage; the classifier still defends against it because corrupt or older rows must not gain unearned confidence.

Classification algorithm (`evidence.py`, implemented in M2):

1. Missing repo ⇒ `unknown` (`repo_missing`); not a Git repository ⇒ `unknown` (`not_a_git_repository`); no captured commit ⇒ `unknown` (`no_revision`).
2. `git cat-file -e <commit>^{commit}` fails ⇒ `unknown` (`history_rewritten`).
3. `coverage != 'files'`, no recorded files, or no scope ⇒ `potentially_stale` (`coverage_incomplete`). This branch is unreachable for a fresh verdict by construction.
4. Re-hash the current scope. If enumeration is incomplete, add `coverage_incomplete`. Then compare:
   - a recorded path that is gone ⇒ `file_deleted`;
   - a recorded path whose SHA-256 differs ⇒ `file_modified`;
   - a current path absent from the recorded set ⇒ `file_added_in_scope`.
5. Git drift and availability for the scope:
   - `git rev-list --count <commit>..HEAD` fails ⇒ `git_unavailable` (no fresh verdict);
   - `git diff --name-only <commit>..HEAD -- <scope>` fails ⇒ `git_unavailable`; non-empty ⇒ `commit_touched_scope`;
   - `git status --porcelain -- <scope>` fails ⇒ `git_unavailable`; non-empty ⇒ `uncommitted_changes`.
6. Any reason ⇒ `potentially_stale` with the sorted, de-duplicated reason list. No reason **and `ahead_by` available** ⇒ `fresh`. A missing `ahead_by` always contributes `git_unavailable`, so a failed commit-distance query can never be fresh. `ahead_by` is reported for context only.
7. Path matching: exact path match, or a scope entry ending in `/` matches that prefix. Declared scope is validated and normalized server-side. When hashing completes, hashes of every in-scope file present at capture time are stored in evidence_files, so a client cannot shrink its blast radius by editing the scope; when it does not, the row is scope_only and can never be fresh.

**Deletion nuance.** Deleting a file inside a directory scope is `file_deleted`. Deleting the directory itself — or an exact-file scope whose file is gone — makes current enumeration incomplete, so the verdict carries `coverage_incomplete` (and may also carry `file_deleted`). Both are stale; neither is fresh.

**Why commit diff *and* hashes:** the diff catches committed drift; hashes catch a dirty working tree that Git history cannot describe; hashes still work when the commit is unreachable (then reported as `unknown`, not silently fresh).

**Why the server does not re-run tests:** executing arbitrary client commands is a security, portability, and latency liability. The server returns the exact `command` and a re-run hint; the agent re-runs and saves a new evidence row. Superseding is explicit (`evidence.superseded_by`), so history stays intact.

## 8. Session recovery and history

`restore_context(task_id, repo_root, history=5)` returns a Markdown pack plus structured JSON, ordered by actionability:

1. **Goal**, last checkpoint `seq`/timestamp, captured revision, branch, dirty flag.
2. **Drift warning** — `HEAD` moved since the checkpoint, files changed that the checkpoint did not mention, or the revision is gone.
3. **Blockers**, then **next actions**.
4. **Decisions** — newest per `supersedes` chain; superseded entries hidden but retrievable.
5. **Verification table** — check key, status, freshness, reasons, `ahead_by`, and the re-run command.
6. **History** — last N checkpoints as `seq · time · goal`, plus deltas since the previous checkpoint (new decisions, resolved blockers, completed steps).

If no checkpoint exists, the pack is a short "no state found" note with the save command — never an error.

**Checkpoint history** is append-only per task. Corrections are new checkpoints with `parent_id` pointing at the checkpoint being corrected; nothing is edited in place. `list_checkpoints` and `restore_context` paginate by `seq` descending.

## 9. MCP tool surface

| Tool | Input (required) | Output |
| --- | --- | --- |
| `record_verification` | `task_id`, `check_key`, `kind`, `command`, `status`, `scope`; optional `exit_code`, `output_excerpt` | `capture_id`, captured commit/coverage, `agent_reported` trust, warnings[] |
| `save_checkpoint` | `task_id`, `goal`; optional decisions/steps/files/blockers/actions/session_id/`verification_capture_ids` | `checkpoint_id`, `seq`, captured git summary, `warnings[]` |
| `restore_context` | `task_id` or `repo_root`; optional `history` | `markdown`, `checkpoint`, `evidence_report`, `drift` |
| `list_checkpoints` | `task_id`; optional `limit`, `before_seq` | `[{seq, id, goal, created_at, evidence_counts}]` |
| `check_evidence` | `task_id`; optional `include_failed`, `include_superseded` | `[{check_key, status, freshness, reasons, command, ahead_by, capture_id}]` |

`record_verification` is separate from `save_checkpoint` on purpose: an agent can record a result immediately after running a check, then save a checkpoint later without changing the snapshot.

Tool descriptions state the contract in plain language so an agent can choose them without this document. Every handler validates with Pydantic and returns a compact JSON object; exceptions become MCP tool errors with actionable messages (never stack traces).

## 10. Milestones

| # | Milestone | Deliverable | Exit criteria |
| --- | --- | --- | --- |
| M0 | Phase 1 inspection + plan | This document, brief, README | Docs exist; claims backed by observed output; **no app code** |
| M1 | Ledger core | `pyproject.toml`, `models.py`, `store.py`, `001_init.sql`, fixtures | Section round trip without data loss; append-only UPDATE/DELETE blocked on checkpoints, decisions and evidence; only documented bookkeeping fields mutable; `seq` strictly increasing; rollback leaves no rows or consumed sequence; migrations idempotent |
| M2 | Git + evidence | `gitinfo.py`, `evidence.py`, migrations 002–003, tests | Capture/save split: a late checkpoint keeps the original commit and hashes; status stays independent; scope-only never fresh; Git failures non-fatal; self/cross-task supersession blocked at the database |
| M3 | MCP server | `server.py`, `context.py`, `__main__.py` | SDK client spawns server over stdio: initialize → `tools/list` (5 tools) → record → save → new process → restore; stdout clean |
| M4 | Demo + Alan wiring | `scripts/demo.py`, `scripts/mcp_smoke.py`, real config entry | Scripted stale-evidence demo passes; isolated-config `opencode mcp list` shows `✓ stateledger`; documented manual step for real config |
| M5 | Hardening | packaging, perf, docs accuracy | `pytest` green offline; restore < 500 ms at 100 checkpoints; README status table matches reality |
| M6 | Stretch (post-core) | optional HTTP transport, UI, search | Only after M4 demo is stable; not part of the hackathon gate |

Sequencing rule: **working MCP server and end-to-end demo before any frontend or vector search.** M6 is explicitly cuttable.

## 11. Test strategy

- **Unit — schema (M1, implemented):** valid/invalid payloads, size caps, path traversal and glob rejection, duplicate detection, unknown status/kind/change type, task/session id formats, client attempts to supply Git or server fields.
- **Unit — store (M1/M2, implemented):** migration idempotency (v1 and v2 upgrades to v3), atomic migration rollback, newer-version rejection, full-section round trip, normalized rows mirroring the canonical payload, `seq` monotonicity per task, correction history, append-only UPDATE/DELETE on every table, one-time `superseded_by`, self-link and cross-task link rejection, sessions `last_seen_at`-only updates, normalized evidence reads exposing `superseded_by` and `capture_id`, and an injected mid-write failure leaving no checkpoint rows or consumed sequence.
- **Unit — git (M2, implemented):** temp repo; clean capture, dirty tree, unborn repo, non-repo, missing path, commit existence/distance, scoped diff and status; timeouts on the first command and on `status` return error snapshots instead of raising. No writes to Git metadata (`GIT_OPTIONAL_LOCKS=0`).
- **Unit — evidence (M2, implemented):** SHA-256 scope hashing, exclusion of `.git`/`.stateledger`, incomplete coverage for missing/empty/exact-missing scopes, and the §7 matrix: fresh, modified, deleted, unrelated commit, dirty in/out of scope, new file in scope, missing repo, non-repo, rewritten history, `scope_only`, empty recorded file set, failed-stays-failed, failed-can-be-stale, error-stays-error; a `commit_distance` failure with clean diff/status still yields `git_unavailable`.
- **Regression — capture vs save (M2 review, implemented):** result recorded at A, tree changed to B, checkpoint saved at B; the evidence keeps A's commit and hashes and classifies as `potentially_stale`; a capture taken at B classifies `fresh`; saving with a capture never calls `hash_scope`; unknown, cross-task, duplicate, and >50 capture ids are rejected.
- **Failure policy — Git (M2 review, implemented):** a raising Git capture is converted to a `capture_error` snapshot, checkpoint saving still succeeds, and classifier Git-query failures produce `git_unavailable` (stale), never fresh.
- **Integration — store + Git (M2, implemented):** record → attach → classify preserves the original snapshot and status.
- **Integration — protocol (M3):** spawn the actual server with `mcp.client.stdio`, run `initialize`/`list_tools`/`call_tool`, restart the process against the same DB, and assert the checkpoint survives. This is the proof that the transport works, independent of Alan.
- **CLI — fallback (M4):** `python -m stateledger restore --repo <tmp>` prints the pack and exits 0 with no MCP client.
- **Performance (non-blocking):** 100 checkpoints, `restore_context` p95 < 500 ms locally; failure is a warning, not a demo blocker.
- **Manual — integration (M4):** add the server to an isolated OpenCode config, run `opencode mcp list`, then repeat against the real config path on this machine. Record the observed output in the PR, not in tests.

Nothing in the suite requires network access. All tests use `tmp_path` and a temp ledger.

## 12. Acceptance criteria

| ID | Criterion | Verified by |
| --- | --- | --- |
| AC1 | A checkpoint saved with all sections returns identical structured fields on restore | `test_store.py` round trip (M1); repeated across a process restart in M3 |
| AC2 | Checkpoint, decision, evidence, and capture rows cannot be updated or deleted; only `sessions.last_seen_at` and a one-time `evidence.superseded_by` link are mutable | trigger tests expecting `sqlite3.IntegrityError` |
| AC3 | A passing check whose scope file is modified after capture becomes `potentially_stale` with reason `file_modified`, while `status` remains `passed` | `test_evidence.py` |
| AC4 | A failed check remains `status=failed` whether fresh or stale; it is never reported as passing | matrix tests |
| AC5 | A commit that does not touch the evidence scope leaves it `fresh`, and reports `ahead_by > 0` | matrix test |
| AC6 | After a full server process restart, `restore_context` returns the prior checkpoint | two-process stdio integration test (M3) |
| AC7 | `tools/list` exposes exactly the five documented tools with JSON schemas; every call is Pydantic-validated | protocol test (M3) |
| AC8 | Missing/non-Git repo saves successfully with `git.capture_error` and freshness `unknown`; no crash | `test_gitinfo.py`, `test_evidence.py` |
| AC9 | Server writes no non-MCP bytes to stdout and leaks no secrets/paths outside the repo in logs | stdout-capture assertion (M3) |
| AC10 | `pytest` passes offline on Windows with Python 3.11; M1/M2 need only Pydantic, and the pinned `mcp==2.3.0` enters the runtime at M3 | CI/local run |
| AC11 | CLI fallback prints the same context pack without an MCP client | CLI test (M4) |
| AC12 | The demo script shows record → save → edit → stale → re-record → fresh in under 90 seconds | `scripts/demo.py` (M4) |
| AC13 | Missing, empty, or duplicate scope entries are rejected at validation; coverage is `files` only when capture-time hashing completes, otherwise `scope_only` | `test_models.py`, `test_store.py`, `test_evidence.py` |
| AC14 | A `scope_only` or otherwise incomplete evidence row can never be classified `fresh` | classifier tests (implemented) |
| AC15 | Evidence cannot supersede itself or link to evidence in another task, at both the database and store-API layers | `test_store.py` trigger + API tests |
| AC16 | Every evidence report reads `superseded_by` and `capture_id` from normalized tables; the immutable payload never carries `superseded_by` | `test_list_evidence_exposes_normalized_superseded_by` |
| AC17 | `record_verification` captures Git state + scope hashes at report time; `append_checkpoint` copies the capture and performs no hashing | `test_append_does_not_hash_the_tree`, capture tests |
| AC18 | A result captured at A and attached to a checkpoint saved at B keeps A's commit/hashes and is `potentially_stale`; a capture taken at B is `fresh` | `test_late_checkpoint_does_not_recapture_old_result`, `test_capture_after_change_is_fresh` |
| AC19 | Git capture timeouts/failures do not crash recording or saving, and Git query failures (including `commit_distance` returning `None`) never yield `fresh` | `test_gitinfo.py`, `test_evidence.py` failure tests |

## 13. Risks

| ID | Risk | Impact | Mitigation / early signal |
| --- | --- | --- | --- |
| R1 | Cloud Alan session cannot register arbitrary MCP servers (no tool exists; only local engine verified) | Demo cannot rely on cloud agent | Lead with CLI + standalone client; treat local Alan config as verified only after M4 manual check; state the limitation in README |
| R2 | MCP 2.x API churn (`FastMCP` removal already observed) | Build breaks after an upgrade | Pin `mcp==2.3.0`; single import boundary; protocol test in CI |
| R3 | Windows stdio encoding/path issues (CRLF, absolute paths, spaces) | Server fails to start for Alan | UTF-8 everywhere, absolute command paths, temp-repo test with spaces, stdout discipline test |
| R4 | False "fresh" when evidence coverage is incomplete or was re-hashed later | Overconfidence — worst failure mode | Capture-time hashes; complete coverage is a precondition for `fresh`; checkpoint saving cannot hash; scope_only/incomplete ⇒ stale/unknown |
| R5 | SQLite locking across concurrent sessions | Lost writes or hangs | WAL, `busy_timeout=5000`, `BEGIN IMMEDIATE` for seq allocation, single-writer transaction test |
| R6 | Dirty tree vs captured commit | Wrong staleness verdicts | Hashes catch working-tree drift; Git diff catches committed drift; `ahead_by` is context only |
| R7 | Git history rewrite (rebase/amend) | Evidence tied to a vanished commit | `cat-file -e` check ⇒ `unknown` with `history_rewritten`; never guess |
| R8 | Scope creep into UI/vector search | Core demo unfinished | Non-goals documented; M6 gated behind M4 |
| R9 | Agent misreports a test result | False evidence | `trust=agent_reported` recorded; hashes bound to the capture revision; the server never claims the command ran |
| R10 | The ledger's own files perturb Git state or hashes | Every checkpoint looks dirty / stale | `.stateledger` excluded from status/diff parsing and scope hashing; test repos ignore it; append still verifies `dirty=False` |
| R11 | Hashing a huge scope stalls a capture | Slow record call or memory pressure | 2,000-file, 5 MB/file, 50 MB-total caps; exceeding any cap downgrades to `scope_only`, which can never be fresh |
| R12 | Agent records a result late (after the tree changed) | A new capture could wrongly vouch for later code | API is explicitly "record now"; `captured_at` and the capture commit expose when it happened; docs tell agents to call it immediately; the server cannot verify the command ran |
| R13 | Git becomes unavailable between capture and classification | Temporary unknown/stale, or a crashed save | Capture never raises; classifier maps failures (including commit-distance) to `git_unavailable`; a checkpoint save with a Git error still succeeds |

## 14. Exact next milestone

**M1 — Ledger core (complete).** Contract, append-only store, atomic migrations, and focused tests. The M1 model/store subset totals 66 passing tests: 61 from the original review plus supersession, reporting, and migration-upgrade fixes.

**M2 + review fix — Git capture and evidence freshness (complete).** Added read-only Git inspection (`gitinfo.py`), a dedicated `record_verification` capture API, SHA-256 scope hashing, the freshness classifier (`evidence.py`), migrations 002–003, self/cross-task supersession guards, and the normalized evidence read model.

The full suite now runs **106 tests**, all passing offline.

What is now proven by tests:

- Capture and save are separate: a result recorded at A and attached to a checkpoint saved at B keeps A's commit and hashes, and B is `potentially_stale`, never "verified by A".
- A capture taken at B after the change is `fresh`.
- `append_checkpoint` never hashes the tree; only `record_verification` does.
- A save captures the checkpoint's commit with `dirty=False` despite the ledger file living in the repo.
- Current revision ⇒ `fresh`; modified or deleted file ⇒ `potentially_stale`; unrelated commit ⇒ `fresh` with `ahead_by`; dirty scope ⇒ stale; dirty out of scope ⇒ fresh.
- Missing repo, non-repo, and rewritten history ⇒ `unknown`; `scope_only` and empty recorded file sets ⇒ `potentially_stale`.
- `status` (passed/failed/error) is copied untouched; a failed check can be fresh or stale.
- Git timeouts/failures do not crash capture or saving; classification never produces `fresh` when any Git query fails, including commit distance.
- Evidence cannot supersede itself or cross tasks at DB or API level; reporting reads `superseded_by` and `capture_id` from normalized tables.

Still absent: MCP server, context pack, CLI, demo, frontend, vector search.

**Next after review — M3 — MCP server + context pack.** Wrap the store in five tools over stdio (`record_verification`, `save_checkpoint`, `restore_context`, `list_checkpoints`, `check_evidence`), render the Markdown context pack, and prove a fresh-process restore through an SDK client. Do not start M4 (demo/Alan wiring) or any frontend/vector work until M3 is reviewed.
