from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import LATEST_PROTOCOL_VERSION

from stateledger.models import CheckpointDraft
from stateledger.server import (
    StateLedgerToolset,
    TOOL_NAMES,
    create_server,
)

WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src"


def _source(git_repo):
    git_repo.write("src/app.py", "print('one')\n")
    git_repo.commit("feat: source")


def _capture(toolset, *, status="passed", exit_code=0, command="pytest -q"):
    return toolset.record_verification(
        task_id="task-a",
        check_key="tests:unit",
        kind="test",
        command=command,
        status=status,
        exit_code=exit_code,
        scope=["src/"],
    )


def _env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    return env


def _structured(result):
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    if value is not None:
        return value
    for content in result.content:
        if getattr(content, "type", None) == "text":
            return json.loads(content.text)
    raise AssertionError("tool returned no structured content")


# ------------------------------------------------------------------ schemas


def test_server_exposes_exactly_five_tools(git_repo):
    server = create_server(git_repo.root)
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    assert all(tool.output_schema is not None for tool in tools)

    schemas = {tool.name: tool.input_schema for tool in tools}
    assert set(schemas["record_verification"]["required"]) == {
        "task_id",
        "check_key",
        "kind",
        "command",
        "status",
        "scope",
    }
    assert {"task_id", "goal"} <= set(schemas["save_checkpoint"]["required"])
    assert "verification_capture_ids" in schemas["save_checkpoint"]["properties"]
    assert "task_id" in schemas["restore_context"]["required"]
    assert "history" in schemas["restore_context"]["properties"]
    assert "task_id" in schemas["list_checkpoints"]["required"]
    assert "task_id" in schemas["check_evidence"]["required"]


# -------------------------------------------------------------------- tools


def test_toolset_record_and_save(git_store, git_repo):
    _source(git_repo)
    tools = StateLedgerToolset(git_store)

    capture = _capture(tools)
    assert capture.trust == "agent_reported"
    assert capture.coverage == "files"
    assert capture.file_count == 1
    assert capture.git_commit is not None

    saved = tools.save_checkpoint(
        task_id="task-a",
        goal="Ship M3",
        decisions=[],
        completed_steps=["Built the server"],
        blockers=["Review pending"],
        next_actions=["Run integration tests"],
        verification_capture_ids=[capture.capture_id],
    )

    assert saved.seq == 1
    assert saved.task_id == "task-a"
    assert saved.verification_count == 1
    assert saved.verification_capture_ids == [capture.capture_id]
    assert saved.git is not None
    assert saved.git.commit is not None


def test_toolset_restore_context_and_no_state(git_store, git_repo):
    _source(git_repo)
    tools = StateLedgerToolset(git_store)
    capture = _capture(tools)
    tools.save_checkpoint(
        task_id="task-a",
        goal="Restore me",
        verification_capture_ids=[capture.capture_id],
    )

    restored = tools.restore_context("task-a")
    assert "Restore me" in restored.markdown
    assert restored.checkpoint is not None
    assert restored.checkpoint.seq == 1
    assert len(restored.evidence_report) == 1
    assert restored.evidence_report[0].check_key == "tests:unit"
    assert restored.evidence_report[0].status == "passed"

    missing = tools.restore_context("missing-task")
    assert missing.checkpoint is None
    assert "No state found" in missing.markdown


def test_toolset_list_checkpoints(git_store, git_repo):
    _source(git_repo)
    tools = StateLedgerToolset(git_store)
    tools.save_checkpoint(task_id="task-a", goal="First")
    tools.save_checkpoint(task_id="task-a", goal="Second")

    listed = tools.list_checkpoints("task-a", limit=2)
    assert [item.seq for item in listed.checkpoints] == [2, 1]
    assert listed.checkpoints[0].goal == "Second"


def test_toolset_check_evidence_and_filters(git_store, git_repo):
    _source(git_repo)
    tools = StateLedgerToolset(git_store)

    passed_capture = _capture(tools, status="passed", exit_code=0)
    tools.save_checkpoint(
        task_id="task-a",
        goal="Passed result",
        verification_capture_ids=[passed_capture.capture_id],
    )
    failed_capture = _capture(tools, status="failed", exit_code=1)
    tools.save_checkpoint(
        task_id="task-a",
        goal="Failed result",
        verification_capture_ids=[failed_capture.capture_id],
    )

    all_evidence = tools.check_evidence("task-a")
    assert [item.status for item in all_evidence.evidence] == ["failed", "passed"]

    only_passed = tools.check_evidence("task-a", include_failed=False)
    assert [item.status for item in only_passed.evidence] == ["passed"]

    hidden = tools.check_evidence("task-a", include_superseded=False)
    assert len(hidden.evidence) == 2


def test_check_evidence_cap_prefers_newest_and_reports_truncation(
    git_store, git_repo, seed_evidence_rows
):
    _source(git_repo)
    checkpoint = git_store.append_checkpoint(
        "task-a", CheckpointDraft(goal="Evidence cap")
    )
    seed_evidence_rows(git_store, checkpoint, 1001)
    tools = StateLedgerToolset(git_store)

    result = tools.check_evidence("task-a")

    assert result.total == 1001
    assert result.returned == 1000
    assert result.truncated is True
    assert result.evidence[0].check_key == "check:1000"
    assert result.evidence[-1].check_key == "check:1"
    assert any("Evidence truncated" in warning for warning in result.warnings)


def test_toolset_invalid_inputs_are_actionable(git_store, git_repo, tmp_path):
    _source(git_repo)
    tools = StateLedgerToolset(git_store)

    with pytest.raises(ToolError, match="task_id"):
        tools.record_verification(
            task_id="Bad Task",
            check_key="x",
            kind="test",
            command="pytest -q",
            status="passed",
            scope=["src/"],
        )
    with pytest.raises(ToolError, match="scope"):
        tools.record_verification(
            task_id="task-a",
            check_key="x",
            kind="test",
            command="pytest -q",
            status="passed",
            scope=[],
        )
    with pytest.raises(ToolError, match="capture"):
        tools.save_checkpoint(
            task_id="task-a",
            goal="Missing capture",
            verification_capture_ids=["vc_missing"],
        )
    with pytest.raises(ToolError, match="goal"):
        tools.save_checkpoint(task_id="task-a", goal="")
    with pytest.raises(ToolError, match="limit"):
        tools.list_checkpoints("task-a", limit=0)
    with pytest.raises(ToolError, match="history"):
        tools.restore_context("task-a", history=0)
    with pytest.raises(ToolError, match="bound"):
        tools.restore_context("task-a", repo_root=str(tmp_path / "other"))


# ---------------------------------------------------------------- stdio


def _server_command(repo):
    return [
        sys.executable,
        "-m",
        "stateledger.server",
        "--repo",
        str(repo),
    ]


def _read_response(proc, request_id):
    while True:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout before responding")
        message = json.loads(line)  # any non-JSON output fails here
        if message.get("id") == request_id:
            return message


def test_stdout_contains_only_protocol_frames(git_repo):
    proc = subprocess.Popen(
        _server_command(git_repo.root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=_env(),
    )
    try:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stateledger-test", "version": "0"},
            },
        }
        proc.stdin.write(json.dumps(initialize) + "\n")
        proc.stdin.flush()
        first = _read_response(proc, 1)
        assert "result" in first

        proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            + "\n"
        )
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )
        proc.stdin.flush()
        second = _read_response(proc, 2)
        assert len(second["result"]["tools"]) == 5
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        try:
            remaining, _stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            remaining, _stderr = proc.communicate(timeout=10)

    for line in remaining.splitlines():
        if line.strip():
            json.loads(line)


def _async_round_trip(repo):
    async def flow():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "stateledger.server", "--repo", str(repo)],
            env=_env(),
            cwd=str(WORKSPACE),
        )

        capture_id = None
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == list(TOOL_NAMES)

                invalid = await session.call_tool(
                    "record_verification",
                    {
                        "task_id": "Bad Task",
                        "check_key": "tests:unit",
                        "kind": "test",
                        "command": "pytest -q",
                        "status": "passed",
                        "scope": ["src/"],
                    },
                )
                assert invalid.is_error is True
                error_text = " ".join(
                    content.text
                    for content in invalid.content
                    if getattr(content, "type", None) == "text"
                )
                assert "task_id" in error_text

                recorded = _structured(
                    await session.call_tool(
                        "record_verification",
                        {
                            "task_id": "task-a",
                            "check_key": "tests:unit",
                            "kind": "test",
                            "command": "pytest -q",
                            "status": "passed",
                            "exit_code": 0,
                            "scope": ["src/"],
                        },
                    )
                )
                capture_id = recorded["capture_id"]
                assert recorded["trust"] == "agent_reported"

                saved = _structured(
                    await session.call_tool(
                        "save_checkpoint",
                        {
                            "task_id": "task-a",
                            "goal": "Persist across server restart",
                            "decisions": [{"text": "Use five MCP tools"}],
                            "verification_capture_ids": [capture_id],
                            "blockers": ["none"],
                            "next_actions": ["restore"],
                        },
                    )
                )
                assert saved["seq"] == 1

                restored = _structured(
                    await session.call_tool(
                        "restore_context", {"task_id": "task-a"}
                    )
                )
                assert "Persist across server restart" in restored["markdown"]
                assert "Use five MCP tools" in restored["markdown"]
                assert "tests:unit" in restored["markdown"]

        # First process is gone here. Start a second process against the same DB.
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == list(TOOL_NAMES)

                restored = _structured(
                    await session.call_tool(
                        "restore_context", {"task_id": "task-a"}
                    )
                )
                assert restored["checkpoint"]["seq"] == 1
                assert "Persist across server restart" in restored["markdown"]
                assert "Use five MCP tools" in restored["markdown"]
                assert "tests:unit" in restored["markdown"]

                evidence = _structured(
                    await session.call_tool(
                        "check_evidence", {"task_id": "task-a"}
                    )
                )
                assert len(evidence["evidence"]) == 1
                assert evidence["evidence"][0]["status"] == "passed"

    asyncio.run(flow())


def test_stdio_integration_and_restart(git_repo):
    _source(git_repo)
    _async_round_trip(git_repo.root)