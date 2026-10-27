from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SMOKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "alan_smoke.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("stateledger_alan_smoke", SMOKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_config_uses_absolute_python_and_repo(tmp_path):
    smoke = _load_smoke()
    repo = tmp_path / "repo"
    repo.mkdir()

    config = smoke.build_config(repo, sys.executable)

    server = config["mcp"]["stateledger"]
    assert server["type"] == "local"
    assert server["enabled"] is True
    assert server["command"][:3] == [
        str(Path(sys.executable).resolve()),
        "-m",
        "stateledger.server",
    ]
    assert server["command"][-2:] == ["--repo", str(repo.resolve())]


def test_write_isolated_config_does_not_touch_real_config(tmp_path):
    smoke = _load_smoke()
    repo = tmp_path / "repo"
    repo.mkdir()
    config_home = tmp_path / "isolated"

    path = smoke.write_isolated_config(config_home, repo, sys.executable)

    assert path == config_home.resolve() / "opencode" / "opencode.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcp"]["stateledger"]["type"] == "local"
    assert "stateledger" in payload["mcp"]