# StateLedger

**Evidence-backed memory for AI coding agents.**

StateLedger saves structured task checkpoints and restores context
across agent sessions. It also tracks whether previously recorded
verification evidence still applies after the code changes.

**The core idea:** a test that passed before a code change is not proof
that the current code passes.

Local-first and repository-scoped. Built with Python, SQLite, Git, and
the Model Context Protocol (MCP).

## Why StateLedger?

AI coding agents can lose track of goals, decisions, blockers, and next
steps between sessions. They can also trust an old test result after the
code it tested has changed.

StateLedger connects saved task context to the repository state and
verification evidence that existed when a result was recorded. When code
changes, it can flag that evidence as potentially stale instead of
treating an old pass as current proof.

## What it does

-   **Save checkpoints** with goals, decisions, completed steps, changed
    files, blockers, and next actions.
-   **Capture verification evidence** at the time the result is
    reported, including the Git revision and hashes of the declared file
    scope.
-   **Check evidence freshness** against the current repository state.
-   **Restore a context pack** as readable Markdown for a new agent
    session.
-   **Preserve history** with append-only checkpoints and explicit
    evidence supersession.
-   **Integrate through MCP** using five tools over stdio.
-   **Run without an agent integration** using the CLI restore command.

### Status and freshness are different

  -----------------------------------------------------------------------
  Status                  Freshness               Meaning
  ----------------------- ----------------------- -----------------------
  `passed`                `fresh`                 The saved evidence
                                                  passes StateLedger's
                                                  freshness checks for
                                                  the current repository
                                                  state.

  `passed`                `potentially_stale`     The check passed
                                                  earlier, but relevant
                                                  code or Git state
                                                  changed. Re-run it
                                                  before relying on it.

  `failed`                `fresh`                 The check failed at the
                                                  current state.

  `failed`                `potentially_stale`     The earlier failure may
                                                  no longer describe the
                                                  current code.

  Any status              `unknown`               StateLedger cannot
                                                  reliably determine
                                                  freshness.
  -----------------------------------------------------------------------

Freshness checks do not prove that a command genuinely ran or that a
test is comprehensive. Verification status is agent-reported;
StateLedger records the reported result and captures repository evidence
around it.

## See the demo

Run the end-to-end demo:

``` powershell
.venv\Scripts\python.exe scripts\demo.py
```

The script creates a disposable Git repository and demonstrates the full
lifecycle:

1.  Run a check and record a passing result against version A.
2.  Change and commit the scoped file as version B.
3.  Restore context and see the old result marked `potentially_stale`.
4.  Re-run the check and record new evidence against version B.
5.  Restore again: the new result is `fresh`, while the old result
    remains in history.

The demo uses a temporary repository and ledger, then cleans them up. It
does not modify the repository from which you launch it.

## Quick start

### Requirements

-   Python 3.11 or newer
-   Git
-   [uv](https://docs.astral.sh/uv/) for the development setup

From the project root, create an environment, install the development
dependencies, and run the tests:

``` powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

Run the demo:

``` powershell
.venv\Scripts\python.exe scripts\demo.py
```

Restore a task's context from the CLI:

``` powershell
.venv\Scripts\python.exe -m stateledger restore --repo C:\path\to\project --task-id my-task
```

Replace `C:\path\to\project` with the target Git repository and
`my-task` with the task ID used when saving checkpoints. The CLI
requires a task ID; task discovery is not currently implemented.

## MCP integration

StateLedger exposes five tools through an MCP stdio server:

  -----------------------------------------------------------------------
  Tool                                Purpose
  ----------------------------------- -----------------------------------
  `record_verification`               Capture a reported check result and
                                      the repository state at that time.

  `save_checkpoint`                   Save structured task context and
                                      attach previously captured
                                      evidence.

  `restore_context`                   Return the context pack and
                                      evidence freshness information.

  `list_checkpoints`                  List recent checkpoints for a task.

  `check_evidence`                    Inspect verification results,
                                      freshness, and reasons for
                                      warnings.
  -----------------------------------------------------------------------

Start the server for a specific repository:

``` powershell
.venv\Scripts\python.exe -m stateledger.server --repo C:\path\to\project
```

The server is bound to one repository. It communicates over stdio:
stdout is reserved for MCP protocol messages, and diagnostics go to
stderr. The server does not execute tests or other verification commands
itself.

**Integration note:** local OpenCode/Alan-engine MCP wiring was tested
using an isolated configuration. Cloud Alan custom-MCP registration was
not available for verification and is not claimed.

## How it works

``` text
AI agent / MCP client
        |
        | MCP over stdio
        v
StateLedger server
        |
        +--> Context pack builder
        +--> Pydantic models and validation
        +--> SQLite ledger and migrations
        +--> Git state and scoped-file hashing
        |
        v
<repository>/.stateledger/ledger.db
```

Verification capture and checkpoint saving are deliberately separate:

1.  `record_verification` captures the reported result, Git revision,
    and file hashes when the result is recorded.
2.  `save_checkpoint` attaches the capture by ID. It does not re-hash
    the repository to make old evidence look current.
3.  `restore_context` compares captured evidence with the repository's
    current state and reports freshness and drift warnings.

The ledger is local to the repository. Git is queried read-only;
StateLedger does not rewrite Git history.

## Tests

Run the complete test suite:

``` powershell
.venv\Scripts\python.exe -m pytest
```

The latest recorded run on Windows (October 27, 2026) completed with
**139 passed**. The suite covers persistence and migrations,
capture-time evidence, freshness classification, context restoration,
MCP stdio behavior, CLI behavior, demo cleanup, and packaging checks.

Compile the source, tests, and scripts:

``` powershell
.venv\Scripts\python.exe -m compileall -q src tests scripts
```

Build a wheel with:

``` powershell
uv build --wheel
```

## Current limitations

-   The MCP server uses stdio and is bound to one repository; there is
    no HTTP transport.
-   The CLI requires a task ID; task discovery is not implemented.
-   Verification status is agent-reported. StateLedger captures Git
    state and hashes but cannot independently prove that a command ran.
-   If `record_verification` is called after the check actually ran, the
    capture reflects the later repository state; the captured timestamp
    and revision make that visible.
-   Context output is capped for large histories, with truncation
    warnings.
-   No frontend or semantic/vector search is included.
-   Cloud Alan custom-MCP integration has not been verified.

## Project structure

-   `src/stateledger/` --- models, SQLite store, Git/evidence logic,
    context builder, CLI, MCP server, and packaged migrations
-   `tests/` --- automated tests
-   `scripts/demo.py` --- end-to-end stale-evidence demo
-   `scripts/alan_smoke.py` --- isolated local OpenCode/Alan MCP smoke
    test
-   `PROJECT_BRIEF.md` --- product scope and design rationale
-   `PLAN.md` --- implementation plan and deeper technical details
