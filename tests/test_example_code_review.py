"""Tests for the code-review example eval: parse-findings and match-findings checkers.

These run the real checker executables as subprocesses, exactly as
smevals grade would.
"""

import json
import os
import pathlib
import subprocess

import pytest
import yaml

SUITE = pathlib.Path(__file__).parent.parent / "examples" / "code-review"
CHECKERS = SUITE / "checkers"


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


def test_parse_findings_fenced_json(tmp_path):
    """Fenced JSON is extracted."""
    run_dir, ws = make_run(
        tmp_path,
        "Here is the review:\n```json\n"
        '{"findings": [{"line": 5, "description": "type error"}]}\n'
        "```\nDone.",
    )
    proc, result = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode == 0
    findings = json.loads((ws / "findings.json").read_text())
    assert len(findings["findings"]) == 1
    assert findings["findings"][0]["line"] == 5
    assert findings["findings"][0]["description"] == "type error"
    assert "1 finding" in result["notes"]


def test_parse_findings_bare_json(tmp_path):
    """Bare JSON (no fence) is extracted from last brace block."""
    run_dir, ws = make_run(
        tmp_path,
        'Some text before.\n{"findings": [{"line": 3, "description": "unused var"}]}',
    )
    proc, result = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode == 0
    findings = json.loads((ws / "findings.json").read_text())
    assert len(findings["findings"]) == 1
    assert findings["findings"][0]["line"] == 3


def test_parse_findings_prose_wrapped_last_wins(tmp_path):
    """Last fenced JSON block wins when multiple blocks present."""
    run_dir, ws = make_run(
        tmp_path,
        "Initial review:\n"
        "```json\n"
        '{"findings": [{"line": 10, "description": "draft issue"}]}\n'
        "```\n\n"
        "Corrected review:\n"
        "```json\n"
        '{"findings": [{"line": 42, "description": "actual bug"}]}\n'
        "```",
    )
    proc, result = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode == 0
    findings = json.loads((ws / "findings.json").read_text())
    # Should contain the LAST block's finding (line 42), not the first (line 10)
    assert findings["findings"][0]["line"] == 42
    assert findings["findings"][0]["description"] == "actual bug"


def test_parse_findings_missing_findings_key_fails(tmp_path):
    """Missing findings key causes failure."""
    run_dir, ws = make_run(tmp_path, '{"review": "no findings key", "data": []}')
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_line_0_fails(tmp_path):
    """Line number 0 is invalid and fails."""
    run_dir, ws = make_run(
        tmp_path, '{"findings": [{"line": 0, "description": "invalid line"}]}'
    )
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_empty_output_fails(tmp_path):
    """Empty output fails."""
    run_dir, ws = make_run(tmp_path, "   \n")
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_honors_creates(tmp_path):
    """SMEVALS_CHECK_CREATES environment variable is honored."""
    run_dir, ws = make_run(
        tmp_path, '{"findings": [{"line": 7, "description": "bug"}]}'
    )
    proc, _ = run_checker(
        "parse-findings", ws, run_dir, check={"creates": "review.json"}
    )
    assert proc.returncode == 0
    assert (ws / "review.json").exists()
    findings = json.loads((ws / "review.json").read_text())
    assert findings["findings"][0]["line"] == 7


def test_parse_findings_empty_description_fails(tmp_path):
    """Empty description string is invalid."""
    run_dir, ws = make_run(tmp_path, '{"findings": [{"line": 5, "description": ""}]}')
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_whitespace_only_description_fails(tmp_path):
    """Whitespace-only description is invalid."""
    run_dir, ws = make_run(
        tmp_path, '{"findings": [{"line": 5, "description": "   "}]}'
    )
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_multiple_findings(tmp_path):
    """Multiple findings are all extracted."""
    run_dir, ws = make_run(
        tmp_path,
        '{"findings": ['
        '{"line": 1, "description": "first"},'
        '{"line": 5, "description": "second"},'
        '{"line": 10, "description": "third"}'
        "]}",
    )
    proc, result = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode == 0
    findings = json.loads((ws / "findings.json").read_text())
    assert len(findings["findings"]) == 3
    assert "3 finding" in result["notes"]


def test_parse_findings_missing_line_key_fails(tmp_path):
    """Missing line key fails."""
    run_dir, ws = make_run(tmp_path, '{"findings": [{"description": "no line"}]}')
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_non_integer_line_fails(tmp_path):
    """Non-integer line value fails."""
    run_dir, ws = make_run(
        tmp_path, '{"findings": [{"line": "5", "description": "string line"}]}'
    )
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


def test_parse_findings_negative_line_fails(tmp_path):
    """Negative line number fails."""
    run_dir, ws = make_run(
        tmp_path, '{"findings": [{"line": -1, "description": "negative"}]}'
    )
    proc, _ = run_checker("parse-findings", ws, run_dir)
    assert proc.returncode != 0


# match-findings tests


def make_match_findings_env(tmp_path, answers_data):
    """Create a temporary answers directory with test-task.yaml."""
    import yaml

    answers_dir = tmp_path / "answers"
    answers_dir.mkdir()
    (answers_dir / "test-task.yaml").write_text(yaml.dump(answers_data))
    return str(answers_dir)


def run_match_findings(tmp_path, findings_data, answers_data):
    """Helper to run match-findings checker with test setup."""
    answers_dir = make_match_findings_env(tmp_path, answers_data)
    ws = tmp_path / "grade"
    ws.mkdir()
    (ws / "findings.json").write_text(json.dumps({"findings": findings_data}))

    env = os.environ.copy()
    env["SMEVALS_CHECK_ANSWERS_DIR"] = answers_dir
    env["SMEVALS_TASK"] = "test-task"

    proc = subprocess.run(
        [str(CHECKERS / "match-findings")],
        cwd=ws,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    result = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc, result


def test_match_findings_exact_line_correct_description(tmp_path):
    """Exact line match with correct description scores 1.0 and exits 0."""
    findings = [{"line": 10, "description": "variable is shadowed"}]
    answers = {"line": 10, "window": 2, "must_mention": ["shadow"]}
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 0
    assert result["score"] == 1.0
    assert "found_planted" in result["tags"]
    assert result["metrics"]["found_planted"] is True
    assert result["metrics"]["false_positives"] == 0
    assert result["metrics"]["findings_total"] == 1


def test_match_findings_window_edge_still_found(tmp_path):
    """Line at window edge (line+window) still matches."""
    findings = [{"line": 12, "description": "shadow issue"}]
    answers = {"line": 10, "window": 2, "must_mention": ["shadow"]}
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 0
    assert result["score"] == 1.0
    assert "found_planted" in result["tags"]


def test_match_findings_second_regex_alternative(tmp_path):
    """Description matching second regex alternative is found."""
    findings = [{"line": 15, "description": "variable shadowing bug"}]
    answers = {
        "line": 15,
        "window": 1,
        "must_mention": ["off-by-one", "shadow"],
    }
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 0
    assert result["score"] == 1.0
    assert "found_planted" in result["tags"]


def test_match_findings_right_line_wrong_diagnosis(tmp_path):
    """Right line but wrong diagnosis scores 0.5."""
    findings = [{"line": 20, "description": "this is a comment"}]
    answers = {"line": 20, "window": 3, "must_mention": ["buffer overflow"]}
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 1
    assert result["score"] == 0.5
    assert "right_line_wrong_diagnosis" in result["tags"]
    assert result["metrics"]["found_planted"] is False
    assert result["metrics"]["false_positives"] == 0


def test_match_findings_all_wrong_decoy(tmp_path):
    """Finding outside window scores 0.0 as missed_planted."""
    findings = [{"line": 1, "description": "unused variable"}]
    answers = {"line": 50, "window": 2, "must_mention": ["unused"]}
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 1
    assert result["score"] == 0.0
    assert "missed_planted" in result["tags"]
    assert result["metrics"]["found_planted"] is False
    assert result["metrics"]["false_positives"] == 1


def test_match_findings_empty_findings_silent_pass(tmp_path):
    """Empty findings list adds silent_pass tag."""
    findings = []
    answers = {"line": 10, "window": 2, "must_mention": ["error"]}
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 1
    assert result["score"] == 0.0
    assert "silent_pass" in result["tags"]
    assert result["metrics"]["findings_total"] == 0


def test_match_findings_noisy_review(tmp_path):
    """More than 3 false positives adds noisy_review tag."""
    findings = [
        {"line": 5, "description": "off-topic 1"},
        {"line": 6, "description": "off-topic 2"},
        {"line": 7, "description": "off-topic 3"},
        {"line": 8, "description": "off-topic 4"},
        {"line": 50, "description": "correct finding"},
    ]
    answers = {"line": 50, "window": 1, "must_mention": ["correct"]}
    proc, result = run_match_findings(tmp_path, findings, answers)

    assert proc.returncode == 0
    assert result["score"] == 1.0
    assert "found_planted" in result["tags"]
    assert "noisy_review" in result["tags"]
    assert result["metrics"]["false_positives"] == 4
