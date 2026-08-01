"""Tests for the code-authoring example evals: checkers, harness, fixtures.

These run the real checker executables as subprocesses, exactly as
smevals grade would. Anything needing node skips when node is absent.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

SUITE = pathlib.Path(__file__).parent.parent / "examples" / "code-authoring"
CHECKERS = SUITE / "checkers"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH"
)


def run_checker(name, cwd, run_dir, check=None, task=None):
    """Execute a Checker per the documented contract; return (proc, result).

    result is the parsed JSON from stdout, or None if stdout was empty.
    """
    check = {"checker": name, **(check or {})}
    env = os.environ | {
        "SMEVALS_RUN_DIR": str(run_dir),
        "SMEVALS_CHECK": json.dumps(check),
        "SMEVALS_TASK": (task or {}).get("name", "test-task"),
    }
    for key, value in check.items():
        if isinstance(value, (str, int, float, bool)):
            env[f"SMEVALS_CHECK_{key.upper()}"] = str(value)
    proc = subprocess.run(
        [str(CHECKERS / name)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    result = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc, result


def make_run(tmp_path, output):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.txt").write_text(output)
    workspace = tmp_path / "grade"
    workspace.mkdir()
    return run_dir, workspace


def test_extract_ts_takes_last_ts_fence(tmp_path):
    run_dir, ws = make_run(
        tmp_path,
        "Here is a sketch:\n```ts\nconst draft = 1;\n```\n"
        "And the final version:\n```typescript\nexport const x: number = 2;\n```\nDone.",
    )
    proc, result = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode == 0
    assert (ws / "solution.ts").read_text().strip() == "export const x: number = 2;"
    assert "extracted" in result["notes"]


def test_extract_ts_falls_back_to_any_fence(tmp_path):
    run_dir, ws = make_run(tmp_path, "```\nexport const y = 3;\n```")
    proc, _ = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode == 0
    assert "y = 3" in (ws / "solution.ts").read_text()


def test_extract_ts_falls_back_to_whole_output(tmp_path):
    run_dir, ws = make_run(tmp_path, "export const z = 4;\n")
    proc, _ = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode == 0
    assert "z = 4" in (ws / "solution.ts").read_text()


def test_extract_ts_fails_on_empty_output(tmp_path):
    run_dir, ws = make_run(tmp_path, "   \n")
    proc, _ = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode != 0


def test_extract_ts_honors_creates(tmp_path):
    run_dir, ws = make_run(tmp_path, "```ts\nexport const q = 5;\n```")
    proc, _ = run_checker("extract-ts", ws, run_dir, check={"creates": "code.ts"})
    assert proc.returncode == 0
    assert (ws / "code.ts").exists()
