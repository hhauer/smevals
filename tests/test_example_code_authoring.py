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


GOOD_TS = "export function double(x: number): number {\n  return x * 2;\n}\n"
BAD_TS = 'export function double(x: number): number {\n  return "nope";\n}\n'


@requires_node
def test_tsc_check_passes_valid_ts(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(GOOD_TS)
    proc, result = run_checker(
        "tsc-check", ws, run_dir, check={"typescript_version": "5.9"}
    )
    assert proc.returncode == 0, proc.stderr
    assert "5.9" in result["notes"]


@requires_node
def test_tsc_check_fails_type_error_with_notes(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(BAD_TS)
    proc, result = run_checker(
        "tsc-check", ws, run_dir, check={"typescript_version": "5.9"}
    )
    assert proc.returncode != 0
    assert "TS2322" in result["notes"]


TOY_CASES = """
export const cases = [
  { group: "math", name: "doubles", run: (m: any) => m.double(2), expect: 4 },
  { group: "math", name: "zero", run: (m: any) => m.double(0), expect: 0 },
  { group: "shape", name: "object", run: (m: any) => ({ v: m.double(3) }), expect: { v: 6 } },
];
"""


def toy_check(tmp_path, cases_src=TOY_CASES):
    (tmp_path / "cases.ts").write_text(cases_src)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)
    return {"cases": rel}


@requires_node
def test_run_tests_all_pass(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(GOOD_TS)
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert result["score"] == 1.0
    assert result["metrics"]["math"] is True
    assert result["metrics"]["cases_total"] == 3
    assert result["tags"] == []


@requires_node
def test_run_tests_partial_credit_and_group_tags(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(
        "export function double(x: number): number { return x === 0 ? 1 : x * 2; }\n"
    )
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode != 0
    assert result["score"] == pytest.approx(2 / 3)
    assert result["metrics"]["math"] is False
    assert result["metrics"]["shape"] is True
    assert "fails_math" in result["tags"]
    failure = result["details"]["failures"][0]
    assert (
        failure["name"] == "zero" and failure["expected"] == 0 and failure["got"] == 1
    )


@requires_node
def test_run_tests_throwing_solution(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(
        'export function double(x: number): number { throw new Error("boom"); }\n'
    )
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode != 0
    assert result["score"] == 0.0
    assert "throws_at_runtime" in result["tags"]
    assert "boom" in result["details"]["failures"][0]["error"]


@requires_node
def test_run_tests_unimportable_solution(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text("export const = broken syntax(((\n")
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode != 0
    assert result["score"] == 0.0
    assert "import_error" in result["tags"]


@requires_node
def test_run_tests_timeout_scores_against_true_total(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    timeout_cases = """
export const cases = [
  { group: "main", name: "quick", run: (m: any) => m.quick(), expect: 1 },
  { group: "main", name: "slow", run: (m: any) => { while (true) {} }, expect: 2 },
  { group: "main", name: "second_quick", run: (m: any) => m.quick(), expect: 1 },
];
"""
    (tmp_path / "cases.ts").write_text(timeout_cases)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)

    (ws / "solution.ts").write_text("export function quick(): number { return 1; }\n")
    proc, result = run_checker(
        "run-tests", ws, run_dir, check={"cases": rel, "timeout_ms": 2000}
    )
    assert proc.returncode != 0
    assert "timeout" in result["tags"]
    assert (
        result["metrics"]["cases_total"] == 3
    ), f"Expected cases_total=3, got {result['metrics']['cases_total']}"
    assert result["score"] < 1.0, f"Score should be < 1.0, got {result['score']}"


@requires_node
def test_run_tests_hang_at_import_scores_zero(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    hang_cases = """
export const cases = [
  { group: "math", name: "doubles", run: (m: any) => m.double(2), expect: 4 },
  { group: "math", name: "zero", run: (m: any) => m.double(0), expect: 0 },
];
"""
    (tmp_path / "cases.ts").write_text(hang_cases)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)

    (ws / "solution.ts").write_text(
        "while (true) {}\n\n"
        "export function double(x: number): number {\n"
        "  return x * 2;\n"
        "}\n"
    )
    proc, result = run_checker(
        "run-tests", ws, run_dir, check={"cases": rel, "timeout_ms": 2000}
    )
    assert proc.returncode != 0
    assert (
        result["score"] == 0.0
    ), f"Expected score 0.0 (not None), got {result['score']}"
    assert "timeout" in result["tags"]
    assert (
        result["metrics"]["cases_total"] == 2
    ), f"Expected cases_total=2, got {result['metrics']['cases_total']}"


@requires_node
def test_run_tests_group_named_like_reserved_key(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    reserved_group_cases = """
export const cases = [
  { group: "cases_passed", name: "test1", run: (m: any) => m.double(2), expect: 4 },
  { group: "cases_passed", name: "test2", run: (m: any) => m.double(3), expect: 6 },
  { group: "normal", name: "test3", run: (m: any) => m.double(4), expect: 8 },
];
"""
    (tmp_path / "cases.ts").write_text(reserved_group_cases)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)

    (ws / "solution.ts").write_text(GOOD_TS)
    proc, result = run_checker("run-tests", ws, run_dir, check={"cases": rel})
    assert proc.returncode == 0
    # metrics["cases_passed"] should still be the integer count
    assert result["metrics"]["cases_passed"] == 3
    # metrics["cases_total"] should still be the integer count
    assert result["metrics"]["cases_total"] == 3
    # metrics["group_cases_passed"] should be the boolean for the group
    assert result["metrics"]["group_cases_passed"] is True
    # metrics["normal"] should be the boolean for the normal group
    assert result["metrics"]["normal"] is True


@requires_node
def test_run_tests_nan_does_not_equal_null(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    nan_cases = """
export const cases = [
  { group: "math", name: "nan_test", run: (m: any) => 0/0, expect: null },
];
"""
    (tmp_path / "cases.ts").write_text(nan_cases)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)

    (ws / "solution.ts").write_text(
        "export function double(x: number): number { return x * 2; }\n"
    )
    proc, result = run_checker("run-tests", ws, run_dir, check={"cases": rel})
    assert proc.returncode != 0, "Expected non-zero exit code"
    assert (
        result["score"] < 1.0
    ), f"NaN should not equal null; score should be < 1.0, got {result['score']}"
    assert "fails_math" in result["tags"]


@requires_node
def test_run_tests_exposes_solution_path_via_env(tmp_path):
    """A case's run() must be able to locate the solution source file

    (e.g. to read it for a source-inspection check) via
    process.env.SMEVALS_SOLUTION, without hardcoding "solution.ts".
    """
    run_dir, ws = make_run(tmp_path, "unused")
    env_cases = """
export const cases = [
  {
    group: "env",
    name: "SMEVALS_SOLUTION points at the resolved solution file",
    run: (_m: any) =>
      typeof process.env.SMEVALS_SOLUTION === "string" &&
      process.env.SMEVALS_SOLUTION.endsWith("solution.ts"),
    expect: true,
  },
];
"""
    (tmp_path / "cases.ts").write_text(env_cases)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)

    (ws / "solution.ts").write_text(GOOD_TS)
    proc, result = run_checker("run-tests", ws, run_dir, check={"cases": rel})
    assert proc.returncode == 0, proc.stderr
    assert result["score"] == 1.0, result["details"]


def fixture_check(eval_name):
    return {"cases": f"{eval_name}/tests/cases.ts"}


def grade_fixture(tmp_path, eval_name, solution_filename):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / eval_name / "reference" / solution_filename
    (ws / "solution.ts").write_text(src.read_text())
    return run_checker("run-tests", ws, run_dir, check=fixture_check(eval_name))


@requires_node
def test_interval_set_reference_scores_1(tmp_path):
    proc, result = grade_fixture(tmp_path, "interval-set", "solution.ts")
    assert result["score"] == 1.0, result["details"]
    assert proc.returncode == 0


@requires_node
def test_interval_set_reference_typechecks(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / "interval-set" / "reference" / "solution.ts"
    (ws / "solution.ts").write_text(src.read_text())
    proc, _ = run_checker("tsc-check", ws, run_dir, check={"typescript_version": "5.9"})
    assert proc.returncode == 0


@requires_node
def test_interval_set_bug_merges_open_touching(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "interval-set", "bug-merges-open-touching.ts"
    )
    assert result["score"] < 1.0
    assert "fails_no_merge_open_touching" in result["tags"]


@requires_node
def test_interval_set_bug_remove_keeps_boundary(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "interval-set", "bug-remove-keeps-boundary.ts"
    )
    assert result["score"] < 1.0
    assert "fails_remove_splitting" in result["tags"]


@requires_node
def test_usage_billing_reference_scores_1(tmp_path):
    proc, result = grade_fixture(tmp_path, "usage-billing", "solution.ts")
    assert result["score"] == 1.0, result["details"]
    assert proc.returncode == 0


@requires_node
def test_usage_billing_reference_typechecks(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / "usage-billing" / "reference" / "solution.ts"
    (ws / "solution.ts").write_text(src.read_text())
    proc, _ = run_checker("tsc-check", ws, run_dir, check={"typescript_version": "5.9"})
    assert proc.returncode == 0


@requires_node
def test_usage_billing_bug_exclusive_tier_bound(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "usage-billing", "bug-exclusive-tier-bound.ts"
    )
    assert result["score"] < 1.0
    assert "fails_tier_bounds" in result["tags"]


@requires_node
def test_usage_billing_bug_credits_before_tax(tmp_path):
    proc, result = grade_fixture(tmp_path, "usage-billing", "bug-credits-before-tax.ts")
    assert result["score"] < 1.0
    assert "fails_credits" in result["tags"]


CONFIG_LEXER_GROUPS = [
    "basics",
    "numbers",
    "string_escapes",
    "raw_strings",
    "nested_comments",
    "positions",
    "error_recovery",
    "eof_edges",
]


def assert_fails_only(metrics, failing_group, groups=CONFIG_LEXER_GROUPS):
    """Every group metric is a bool; only failing_group should be False."""
    for group in groups:
        expected = group != failing_group
        assert (
            metrics[group] is expected
        ), f"group {group!r}: expected metric {expected}, got {metrics.get(group)!r}"


@requires_node
def test_config_lexer_reference_scores_1(tmp_path):
    proc, result = grade_fixture(tmp_path, "config-lexer", "solution.ts")
    assert result["score"] == 1.0, result["details"]
    assert proc.returncode == 0


@requires_node
def test_config_lexer_reference_typechecks(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / "config-lexer" / "reference" / "solution.ts"
    (ws / "solution.ts").write_text(src.read_text())
    proc, _ = run_checker("tsc-check", ws, run_dir, check={"typescript_version": "5.9"})
    assert proc.returncode == 0


@requires_node
def test_config_lexer_bug_comments_dont_nest(tmp_path):
    proc, result = grade_fixture(tmp_path, "config-lexer", "bug-comments-dont-nest.ts")
    assert result["score"] < 1.0
    assert "fails_nested_comments" in result["tags"]
    assert_fails_only(result["metrics"], "nested_comments")


@requires_node
def test_config_lexer_bug_col_counts_utf16(tmp_path):
    proc, result = grade_fixture(tmp_path, "config-lexer", "bug-col-counts-utf16.ts")
    assert result["score"] < 1.0
    assert "fails_positions" in result["tags"]
    assert_fails_only(result["metrics"], "positions")


REFACTOR_PRESERVE_GROUPS = [
    "core_paths",
    "quirk_fallthrough",
    "quirk_discount_order",
    "quirk_nan",
    "duplication",
]


@requires_node
def test_refactor_preserve_reference_scores_1(tmp_path):
    proc, result = grade_fixture(tmp_path, "refactor-preserve", "solution.ts")
    assert result["score"] == 1.0, result["details"]
    assert proc.returncode == 0


@requires_node
def test_refactor_preserve_reference_typechecks(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / "refactor-preserve" / "reference" / "solution.ts"
    (ws / "solution.ts").write_text(src.read_text())
    proc, _ = run_checker("tsc-check", ws, run_dir, check={"typescript_version": "5.9"})
    assert proc.returncode == 0


@requires_node
def test_refactor_preserve_bug_fixed_the_quirk(tmp_path):
    # An otherwise-excellent refactor that "fixes" the fall-through
    # must fail exactly quirk_fallthrough - proving the trap works.
    proc, result = grade_fixture(
        tmp_path, "refactor-preserve", "bug-fixed-the-quirk.ts"
    )
    assert result["score"] < 1.0
    assert "fails_quirk_fallthrough" in result["tags"]
    assert_fails_only(result["metrics"], "quirk_fallthrough", REFACTOR_PRESERVE_GROUPS)


@requires_node
def test_refactor_preserve_legacy_verbatim(tmp_path):
    # The unrefactored original passes every behavior group (it IS the
    # behavior every case was derived from) but fails duplication -
    # proving a model cannot pass by parroting the input.
    proc, result = grade_fixture(tmp_path, "refactor-preserve", "legacy-verbatim.ts")
    assert result["score"] < 1.0
    assert "fails_duplication" in result["tags"]
    assert_fails_only(result["metrics"], "duplication", REFACTOR_PRESERVE_GROUPS)
