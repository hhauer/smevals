"""Tests for the code-review example eval: parse-findings and match-findings checkers.

These run the real checker executables as subprocesses, exactly as
smevals grade would.
"""

import json
import os
import pathlib
import re
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


def test_match_findings_fallback_answers_dir(tmp_path):
    """Tests fallback path when SMEVALS_CHECK_ANSWERS_DIR is not set.

    The checker should read from <eval root>/answers/<task>.yaml,
    which is examples/code-review/answers/<task>.yaml.
    """
    # Ensure answers directory exists
    answers_dir = SUITE / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    # Write a test answer file to the real answers directory
    task_name = "pytest-fallback-task"
    answer_file = answers_dir / f"{task_name}.yaml"
    answers_data = {"line": 15, "window": 2, "must_mention": ["buffer"]}
    answer_file.write_text(yaml.dump(answers_data))

    try:
        # Set up workspace and findings
        ws = tmp_path / "grade"
        ws.mkdir()
        findings = [{"line": 16, "description": "buffer overflow vulnerability"}]
        (ws / "findings.json").write_text(json.dumps({"findings": findings}))

        # Run checker WITHOUT setting SMEVALS_CHECK_ANSWERS_DIR (use fallback)
        env = os.environ.copy()
        # Remove the env var if it exists to ensure fallback path is used
        env.pop("SMEVALS_CHECK_ANSWERS_DIR", None)
        env["SMEVALS_TASK"] = task_name

        proc = subprocess.run(
            [str(CHECKERS / "match-findings")],
            cwd=ws,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        result = json.loads(proc.stdout) if proc.stdout.strip() else None

        # Verify correct behavior using fallback path
        assert proc.returncode == 0
        assert result["score"] == 1.0
        assert "found_planted" in result["tags"]
        assert result["metrics"]["found_planted"] is True
        assert result["metrics"]["false_positives"] == 0
    finally:
        # Clean up the test answer file
        answer_file.unlink(missing_ok=True)


# Validity tests for the six planted-defect commit-review tasks
# (tasks/*.yaml, answers/*.yaml, reference/{reference,decoy,shotgun}-*.json).
#
# These don't call any LLM - they check that the tasks are internally
# consistent: the diff actually applies to `before`, the answer key
# points inside a hunk the diff actually touches, and the three
# validity fixtures each land on their designed score when run through
# the real match-findings checker.

TASK_NAMES = [
    "boundary-shift",
    "stale-closure",
    "float-money",
    "mutated-default",
    "sort-stability",
    "swallowed-error",
]

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@$")


def apply_unified_diff(before_text, diff_text):
    """Applies a unified diff (as produced by `diff -u`) to before_text.

    A small pure-Python applier, deliberately independent of the
    `patch`/`git apply` binaries: it walks each hunk, asserts that
    context/removed lines actually match `before_text` at the position
    the hunk claims, and asserts each hunk's own line-count bookkeeping
    (old_count, new_count, new_start) stays consistent with what's been
    produced so far. Any inconsistency raises AssertionError - a diff
    that doesn't cleanly apply is a broken task.
    """
    before_lines = before_text.split("\n")
    if before_lines and before_lines[-1] == "":
        before_lines = before_lines[:-1]  # before_text ends in "\n"

    diff_lines = diff_text.split("\n")
    if diff_lines and diff_lines[-1] == "":
        diff_lines = diff_lines[:-1]  # diff_text ends in "\n"

    assert diff_lines[0].startswith("--- "), diff_lines[0]
    assert diff_lines[1].startswith("+++ "), diff_lines[1]
    idx = 2

    result = []
    cursor = 0  # 0-based index into before_lines
    while idx < len(diff_lines):
        m = HUNK_HEADER_RE.match(diff_lines[idx])
        assert m, f"expected a hunk header, got: {diff_lines[idx]!r}"
        old_start, old_count, new_start, new_count = (int(g) for g in m.groups())
        idx += 1

        # Copy the unchanged lines between the previous hunk and this one
        gap_end = old_start - 1
        assert gap_end >= cursor, "hunks out of order or overlapping"
        result.extend(before_lines[cursor:gap_end])
        cursor = gap_end

        # The diff's own claim about where this hunk starts in the
        # post-change file must match what we've actually produced -
        # this is the "hunk consistency" check.
        assert len(result) == new_start - 1, (
            f"hunk claims to start at post-change line {new_start}, "
            f"but {len(result)} post-change line(s) precede it"
        )

        old_consumed = new_produced = 0
        while old_consumed < old_count or new_produced < new_count:
            assert idx < len(diff_lines), "hunk body ran past the end of the diff"
            line = diff_lines[idx]
            idx += 1
            marker, content = line[0], line[1:]
            if marker == " ":
                assert cursor < len(before_lines), "context line past end of before"
                assert before_lines[cursor] == content, (
                    f"context mismatch at before-line {cursor + 1}: "
                    f"{before_lines[cursor]!r} != {content!r}"
                )
                result.append(content)
                cursor += 1
                old_consumed += 1
                new_produced += 1
            elif marker == "-":
                assert cursor < len(before_lines), "removal past end of before"
                assert before_lines[cursor] == content, (
                    f"removal mismatch at before-line {cursor + 1}: "
                    f"{before_lines[cursor]!r} != {content!r}"
                )
                cursor += 1
                old_consumed += 1
            elif marker == "+":
                result.append(content)
                new_produced += 1
            else:
                raise AssertionError(f"unrecognized diff line: {line!r}")

        assert (
            old_consumed == old_count
        ), f"hunk claimed {old_count} old line(s), consumed {old_consumed}"
        assert (
            new_produced == new_count
        ), f"hunk claimed {new_count} new line(s), produced {new_produced}"

    result.extend(before_lines[cursor:])
    return "\n".join(result) + "\n"


def parse_hunk_post_ranges(diff_text):
    "Returns [(first_post_line, last_post_line), ...], one per hunk."
    ranges = []
    for m in re.finditer(r"^@@ -\d+,\d+ \+(\d+),(\d+) @@$", diff_text, re.MULTILINE):
        start, count = int(m.group(1)), int(m.group(2))
        ranges.append((start, start + count - 1))
    return ranges


def load_task(name):
    return yaml.safe_load((SUITE / "tasks" / f"{name}.yaml").read_text())


def load_answer(name):
    return yaml.safe_load((SUITE / "answers" / f"{name}.yaml").read_text())


def load_fixture(kind, name):
    return json.loads((SUITE / "reference" / f"{kind}-{name}.json").read_text())


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_task_files_exist(task_name):
    assert (SUITE / "tasks" / f"{task_name}.yaml").exists()
    assert (SUITE / "answers" / f"{task_name}.yaml").exists()
    for kind in ("reference", "decoy", "shotgun"):
        assert (SUITE / "reference" / f"{kind}-{task_name}.json").exists()


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_task_shape(task_name):
    task = load_task(task_name)
    assert task["name"] == task_name
    assert 40 <= task["before"].count("\n") <= 80
    assert "```typescript" in task["prompt"]
    assert "```diff" in task["prompt"]
    assert task["before"] in task["prompt"]
    assert task["diff"] in task["prompt"]


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_diff_applies_cleanly_to_before(task_name):
    task = load_task(task_name)
    after = apply_unified_diff(task["before"], task["diff"])
    # The diff must actually change something - a no-op "commit" isn't a task
    assert after != task["before"]


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_diff_size_reads_as_one_commit(task_name):
    "15-40 changed lines: big enough to hide a defect, small enough to review."
    task = load_task(task_name)
    body_lines = [
        line
        for line in task["diff"].splitlines()
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    ]
    assert 15 <= len(body_lines) <= 40


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_answer_window_and_must_mention(task_name):
    answer = load_answer(task_name)
    assert answer["window"] <= 3
    assert len(answer["must_mention"]) >= 3
    for pattern in answer["must_mention"]:
        re.compile(pattern)  # must be a valid regex


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_defective_line_falls_inside_a_changed_hunk(task_name):
    task = load_task(task_name)
    answer = load_answer(task_name)
    ranges = parse_hunk_post_ranges(task["diff"])
    assert ranges, "diff has no hunks"
    assert any(
        start <= answer["line"] <= end for start, end in ranges
    ), f"answer line {answer['line']} is outside every changed hunk: {ranges}"


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_reference_fixture_scores_one(tmp_path, task_name):
    fixture = load_fixture("reference", task_name)
    answer = load_answer(task_name)
    proc, result = run_match_findings(tmp_path, fixture["findings"], answer)
    assert proc.returncode == 0
    assert result["score"] == 1.0
    assert "found_planted" in result["tags"]


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_decoy_fixture_scores_zero(tmp_path, task_name):
    fixture = load_fixture("decoy", task_name)
    answer = load_answer(task_name)
    proc, result = run_match_findings(tmp_path, fixture["findings"], answer)
    assert proc.returncode == 1
    assert result["score"] == 0.0
    assert "missed_planted" in result["tags"]
    assert result["metrics"]["false_positives"] >= 2


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_shotgun_fixture_scores_half(tmp_path, task_name):
    fixture = load_fixture("shotgun", task_name)
    answer = load_answer(task_name)
    proc, result = run_match_findings(tmp_path, fixture["findings"], answer)
    assert proc.returncode == 1
    assert result["score"] == 0.5
    assert "right_line_wrong_diagnosis" in result["tags"]


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_shotgun_covers_every_changed_hunk(task_name):
    """The fixture contract says shotgun flags every changed hunk, not
    just some of them - nothing else enforces that, so check it here."""
    task = load_task(task_name)
    fixture = load_fixture("shotgun", task_name)
    ranges = parse_hunk_post_ranges(task["diff"])
    lines = [finding["line"] for finding in fixture["findings"]]
    for start, end in ranges:
        assert any(start <= line <= end for line in lines), (
            f"{task_name} shotgun fixture has no finding in hunk "
            f"[{start}, {end}]: finding lines are {lines}"
        )


@pytest.mark.parametrize("task_name", TASK_NAMES)
def test_shotgun_descriptions_match_no_must_mention_pattern(task_name):
    """Proves the shotgun fixture's 0.5 score is earned honestly: its
    generic filler descriptions must not accidentally satisfy any
    must_mention regex, or landing on the right line would score 1.0
    by luck instead of being correctly downgraded to 0.5."""
    fixture = load_fixture("shotgun", task_name)
    answer = load_answer(task_name)
    patterns = [re.compile(p, re.IGNORECASE) for p in answer["must_mention"]]
    for finding in fixture["findings"]:
        for pattern in patterns:
            assert not pattern.search(finding["description"]), (
                f"{task_name} shotgun finding {finding!r} unexpectedly "
                f"matches must_mention pattern {pattern.pattern!r}"
            )
