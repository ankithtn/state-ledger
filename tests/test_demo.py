from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DEMO_PATH = Path(__file__).resolve().parents[1] / "scripts" / "demo.py"

def _load_demo():
    spec = importlib.util.spec_from_file_location("stateledger_demo", DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_record_mutate_stale_rerun_fresh(capsys):
    demo = _load_demo()

    result = demo.run_demo(quiet=True)

    assert result.before_status == "passed"
    assert result.before_freshness == "potentially_stale"
    assert "file_modified" in result.before_reasons
    assert result.after_status == "passed"
    assert result.after_freshness == "fresh"
    assert result.after_reasons == ()
    assert result.old_evidence_still_present is True
    assert result.checkpoints == 2
    assert result.duration_seconds < 90
    assert result.temp_removed is True
    assert not result.temp_dir.exists()
    assert capsys.readouterr().out == ""


def test_demo_uses_the_same_python_for_check_and_record():
    demo = _load_demo()

    assert str(Path(sys.executable)) in demo.CHECK_COMMAND


def test_demo_cleans_up_on_success():
    demo = _load_demo()

    result = demo.run_demo(quiet=True)

    assert result.temp_removed is True
    assert not result.temp_dir.exists()