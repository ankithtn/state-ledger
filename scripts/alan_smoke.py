"""Isolated local OpenCode/MCP verification for StateLedger.

This does not modify the user's real OpenCode config. It writes a disposable
config under a temporary XDG_CONFIG_HOME, asks the installed engine to list MCP
servers, then uses an MCP client session to confirm the five tools and invoke a
read-only restore against a disposable repository.

    .venv\\Scripts\\python.exe scripts/alan_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stateledger.models import CheckpointDraft
from stateledger.store import LedgerStore, default_db_path

TOOL_NAMES = {
    "record_verification",
    "save_checkpoint",
    "restore_context",
    "list_checkpoints",
    "check_evidence",
}


@dataclass(frozen=True)
class AlanSmokeResult:
    config_path: Path
    mcp_list: str
    tool_names: tuple[str, ...]
    restore_markdown: str
    temp_dir: Path
    temp_removed: bool
    duration_seconds: float


def default_engine() -> Path:
    env = os.environ.get("ALAN_CODE_ENGINE_PATH")
    candidates = []
    if env:
        candidates.append(Path(env))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "Programs"
            / "@alan-aidesktop"
            / "resources"
            / "engine"
            / "opencode.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("OpenCode engine not found; set ALAN_CODE_ENGINE_PATH")


def build_config(repo: str | Path, python_exe: str | Path) -> dict:
    return {
        "mcp": {
            "stateledger": {
                "type": "local",
                "command": [
                    str(Path(python_exe).resolve()),
                    "-m",
                    "stateledger.server",
                    "--repo",
                    str(Path(repo).resolve()),
                ],
                "enabled": True,
            }
        }
    }


def write_isolated_config(
    config_home: str | Path,
    repo: str | Path,
    python_exe: str | Path,
) -> Path:
    config_path = Path(config_home).resolve() / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(build_config(repo, python_exe), indent=2) + "\n",
        encoding="utf-8",
    )
    return config_path


def _clean(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def run_mcp_list(
    engine: str | Path,
    config_home: str | Path,
    cwd: str | Path,
) -> str:
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(Path(config_home).resolve())
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env.pop("OPENCODE_CONFIG", None)
    result = subprocess.run(
        [str(engine), "mcp", "list"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    output = _clean(result.stdout + result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"opencode mcp list failed: {output.strip()}")
    return output


def _structured(result):
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    if value is not None:
        return value
    for content in result.content:
        if getattr(content, "type", None) == "text":
            return json.loads(content.text)
    raise RuntimeError("tool returned no structured content")


def _client_checks(repo: Path, command: list[str]) -> tuple[tuple[str, ...], str]:
    async def flow():
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            cwd=str(repo),
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = tuple(sorted(tool.name for tool in tools.tools))
                if set(names) != TOOL_NAMES or len(names) != 5:
                    raise RuntimeError(f"unexpected tools: {names}")
                restored = await session.call_tool(
                    "restore_context", {"task_id": "alan-smoke"}
                )
                payload = _structured(restored)
                return names, payload["markdown"]

    return asyncio.run(flow())


def _force_rmtree(path: Path) -> None:
    def on_error(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=on_error)


def _seed_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "stateledger-smoke@example.com"),
        ("config", "user.name", "StateLedger Smoke"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    (repo / ".gitignore").write_text(".stateledger/\n", encoding="utf-8")
    (repo / "README.md").write_text("Alan smoke repository\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "smoke: initial"],
        check=True,
        capture_output=True,
    )

    ledger = LedgerStore(default_db_path(repo), repo)
    ledger.initialize()
    ledger.append_checkpoint(
        "alan-smoke",
        CheckpointDraft(goal="Verify local StateLedger tools"),
    )


def run_smoke(
    *,
    engine: str | Path | None = None,
    python_exe: str | Path | None = None,
    quiet: bool = False,
) -> AlanSmokeResult:
    started = time.perf_counter()
    engine_path = Path(engine).resolve() if engine else default_engine()
    python_path = Path(python_exe or sys.executable).resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="stateledger-alan-smoke-"))
    repo = temp_root / "repo"
    config_home = temp_root / "config"

    try:
        _seed_repo(repo)
        config_path = write_isolated_config(config_home, repo, python_path)
        mcp_list = run_mcp_list(engine_path, config_home, repo)
        if "stateledger" not in mcp_list or "connected" not in mcp_list:
            raise RuntimeError(
                "engine did not report stateledger connected:\n" + mcp_list
            )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        command = config["mcp"]["stateledger"]["command"]
        tool_names, markdown = _client_checks(repo, command)

        result = AlanSmokeResult(
            config_path=config_path,
            mcp_list=mcp_list,
            tool_names=tool_names,
            restore_markdown=markdown,
            temp_dir=temp_root,
            temp_removed=False,
            duration_seconds=0.0,
        )
    finally:
        _force_rmtree(temp_root)

    result = AlanSmokeResult(
        **{
            **result.__dict__,
            "temp_removed": not temp_root.exists(),
            "duration_seconds": time.perf_counter() - started,
        }
    )
    if not quiet:
        print("=== OpenCode MCP list ===")
        print(
            result.mcp_list.strip()
            .replace("✓", "OK")
            .replace("✗", "X")
            .replace("•", "*")
        )
        print("\n=== Tools via MCP session ===")
        print(", ".join(result.tool_names))
        print("\n=== restore_context (read-only) ===")
        print(result.restore_markdown)
        print(
            f"\ndisposable config (removed): {result.config_path}\n"
            f"temp cleaned: {result.temp_removed} "
            f"elapsed: {result.duration_seconds:.2f}s"
        )
    return result


def main() -> int:
    try:
        run_smoke()
    except Exception as exc:
        print(f"alan smoke failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())