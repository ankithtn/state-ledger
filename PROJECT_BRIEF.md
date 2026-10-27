# StateLedger — Project Brief

**Status:** Phase 1 brief (approved); Phase 2 M1–M5 implemented (ledger, Git/evidence capture, context pack, five-tool MCP server, CLI, demo, local OpenCode wiring, final hardening). M6 not started. See `README.md` for current state.
**Date:** 2026-10-26
**Event:** OpenFire Hackathon

---

## 1. One-line summary

StateLedger is a local-first MCP server that gives coding agents durable, append-only task checkpoints and tells them which saved verification evidence is still trustworthy after the repository changes.

---

## 2. Problem

Coding agents lose working state between sessions. Three failures repeat:

1. **Context loss.** A new session starts with the repository but not the reasoning: what the goal was, which approaches were rejected, what is blocked.
2. **Re-litigation.** Without a recorded decision trail, agents retry discarded approaches and re-ask questions the user already answered.
3. **Stale confidence.** A test that passed three commits ago is treated as proof that the current code works. The opposite error also happens: a known failure is forgotten, or a historical failure is reported as if it were still current.

Chat history is not a durable artifact. Notes in `TODO.md` are unstructured, not queryable, and carry no link to the exact Git revision or file contents they describe.

## 3. Target users

- **Individual developers** driving one or more agents (Alan Code, Claude Code, Cursor, Codex) on a multi-session feature.
- **Small hackathon teams** where agents and humans alternate on the same repository.
- **Agent builders** who need a storage contract they can call over MCP instead of inventing a new notes format per project.

Primary user in Phase 1: the developer running an agent locally against a Git repository, who wants the next session to resume in seconds.

## 4. Solution

A single Python process, launched by the agent as a **stdio MCP server**, backed by one **SQLite** file inside the repository (`.stateledger/ledger.db`).

It exposes a deliberately small tool surface:

| Tool | Purpose |
| --- | --- |
| `record_verification` | Snapshot a reported result now: Git state plus SHA-256 hashes of the declared scope, with `trust = "agent_reported"` |
| `save_checkpoint` | Append task state and attach existing verification captures by id; capture checkpoint Git without re-hashing evidence |
| `restore_context` | Return a compact, prioritized context pack for a new session |
| `list_checkpoints` | Show the append-only history for a task |
| `check_evidence` | Report each saved verification snapshot as fresh, potentially stale, or unknown |

Design constraints: no daemon, no network calls, no vector database, no ORM. Git is read through the `git` CLI, so the ledger never rewrites history or touches the working tree.

## 5. Differentiator

Most "agent memory" tools store prose or embeddings and answer similarity queries. StateLedger stores **engineering state with provenance**:

- **Evidence is bound to a revision.** Every verification row records the commit, the command, the exit status, and SHA-256 hashes of the files it covers.
- **Freshness is computed, not remembered.** On restore, the server re-derives whether the evidence still applies.
- **Pass/fail and fresh/stale are separate axes.** A failed check is a fact at its commit; it can still be stale (possibly fixed). Staleness never masquerades as a failure, and a pass never masquerades as current proof.
- **History is append-only.** Corrections add a checkpoint; they do not edit the past, so resume decisions are auditable.

## 6. Scope

### In scope for v1

- One local repository, one SQLite ledger.
- Four MCP tools over **stdio**.
- Checkpoint schema: goal, decisions, completed steps, changed files, blockers, next actions, Git revision, verification evidence.
- Server-side Git capture (`HEAD`, branch, dirty state) and content hashing.
- Freshness classification with explicit reasons.
- Markdown context-pack rendering and a CLI fallback (`python -m stateledger restore`) that works without any MCP client.
- Pytest suite covering schema, store invariants, Git capture, freshness rules, and a real stdio protocol round trip.

### Explicit non-goals

- No frontend, dashboard, or web UI in v1.
- No vector search, embeddings, or semantic retrieval.
- No cloud sync, accounts, or multi-tenant auth.
- No automatic execution of test commands by the server (the agent re-runs; the server records and classifies).
- No IDE plugins or per-language integrations.
- No replacement for Git, issue trackers, or CI.
- No code understanding: StateLedger records what was claimed and measured, not whether the claim is logically complete.

## 7. Hackathon demo (success story)

A 90-second, scripted demo:

1. Agent saves a checkpoint: goal, two decisions, one passing `pytest` run, Git commit A.
2. Agent (or demo script) edits a source file covered by that test; no new test run.
3. A fresh session calls `restore_context`. The pack shows the goal, open blockers, next actions, and the saved pass marked **potentially stale** with reason `file_modified`, plus the exact re-run command.
4. Re-running the test and saving a new checkpoint flips the evidence to **fresh**; the old row stays in history as superseded.

The demo fails if any step requires manual database editing or an unverified integration.

## 8. What "done" means for Phase 1

- Workspace and toolchain inspected, not assumed.
- MCP Python SDK version, transport, and API verified by execution.
- At least one evidenced path (or documented fallback) for connecting to Alan.
- Schema, recovery semantics, staleness rules, acceptance criteria, risks, and milestones written down.
- Three documents exist: this brief, `PLAN.md`, `README.md`. No application code.

## 9. Assumptions and open questions

- **A1:** The agent can be trusted to report its own command exit status. Provenance limits this: the server captures Git state and file hashes, so an agent cannot silently claim evidence for files it did not have at that revision.
- **A2:** Users run agents inside (or pointed at) a Git repository. Non-Git directories are supported but downgrade evidence freshness to `unknown`.
- **A3:** A single developer/session writes at a time. SQLite WAL plus `busy_timeout` covers occasional concurrency; multi-writer coordination is out of scope.
- **Q1:** Should checkpoint scope default to the whole repository or to the changed-file set? Current plan: whole-repo hash manifest for changed files, plus optional explicit scope for evidence.
- **Q2:** Is a user-editable Alan config entry sufficient in a live desktop session? Locally verified engine behavior says yes; the real desktop launch path is not yet exercised.