"""Tests for `smevals report`, against fabricated Runs and Grades in the
documented on-disk format."""

import json

from conftest import read_yaml, write_grade, write_run


def reported_eval(make_eval):
    "An eval scaffold plus its parsed default grader doc (for snapshots)"
    eval_dir = make_eval(runner=None)
    grader_doc = read_yaml(eval_dir / "graders" / "default.yaml")
    return eval_dir, grader_doc


def leaderboard_ranks(output):
    "model name -> rank, parsed from the leaderboard section"
    ranks = {}
    lines = output.split("## Leaderboard")[1].split("##")[0].splitlines()
    for line in lines:
        line = line.strip()
        if not line or "." not in line:
            continue
        rank = int(line.split(".")[0])
        model = line.split("(")[0].split()[-1]
        ranks[model] = rank
    return ranks


def test_leaderboard_orders_by_mean_and_shares_tied_ranks(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    for model, scores in [
        ("model-a", [1.0, 0.8]),
        ("model-b", [0.6]),
        ("model-c", [0.6]),
        ("model-d", [0.5]),
    ]:
        for score in scores:
            run_dir = write_run(runs_root, model=model)
            write_grade(run_dir, grader_doc, score=score)

    result = invoke("report", eval_dir)
    assert leaderboard_ranks(result.output) == {
        "model-a": 1,
        "model-b": 2,
        "model-c": 2,  # tied displayed score shares the rank
        "model-d": 4,  # next rank skips, competition style
    }
    # Multiple runs show mean ± standard error
    assert "0.90 ±0.10" in result.output


def test_header_counts_fails_and_ungraded(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root), grader_doc, score=1.0)
    write_grade(write_run(runs_root), grader_doc, outcome="fail", score=0.0)
    write_run(runs_root)  # never graded
    result = invoke("report", eval_dir)
    assert "Graded: 2 runs (1 failed, 1 ungraded)" in result.output
    assert "2 runs, 1 fail" in result.output


def test_stale_grades_warning(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    run_dir = write_run(eval_dir / "runs")
    stale_snapshot = {"name": "default", "checks": []}
    write_grade(run_dir, stale_snapshot, score=1.0)
    result = invoke("report", eval_dir)
    assert "1 of 1 grades came from an older version" in result.output


def test_tag_shares_section(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root), grader_doc, tags=["hat", "bike"])
    write_grade(write_run(runs_root), grader_doc, tags=["hat"])
    result = invoke("report", eval_dir)
    assert "## Tags" in result.output
    tags_section = result.output.split("## Tags")[1].split("##")[0]
    assert "2/2 (100%)  hat" in tags_section
    assert "1/2  (50%)  bike" in tags_section


def test_metrics_aggregation_numbers_and_booleans(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    write_grade(
        write_run(runs_root),
        grader_doc,
        score=1.0,
        checks=[
            {
                "checker": "c",
                "ok": True,
                "metrics": {"latency": 2.0, "status_correct": True},
            }
        ],
    )
    write_grade(
        write_run(runs_root),
        grader_doc,
        score=1.0,
        checks=[
            {
                "checker": "c",
                "ok": True,
                "metrics": {"latency": 4.0, "status_correct": False},
            }
        ],
    )
    result = invoke("report", eval_dir)
    assert "- latency: 3.00 ±1.00" in result.output  # mean ± stderr
    assert "- status_correct: 50%" in result.output  # booleans as rates


def test_metrics_aggregation_ignores_null_values(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    write_grade(
        write_run(runs_root),
        grader_doc,
        checks=[{"checker": "c", "ok": True, "metrics": {"latency": None}}],
    )
    write_grade(
        write_run(runs_root),
        grader_doc,
        checks=[{"checker": "c", "ok": True, "metrics": {"latency": 2.0}}],
    )

    result = invoke("report", eval_dir)
    assert "- latency: 2.00" in result.output


def test_by_task_breakdown(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, task="alpha"), grader_doc, score=1.0)
    write_grade(write_run(runs_root, task="beta"), grader_doc, score=0.5)

    plain = invoke("report", eval_dir)
    assert "scores by task" not in plain.output

    result = invoke("report", eval_dir, "--by-task")
    section = result.output.split("scores by task:")[1]
    assert "- alpha: 1.00" in section
    assert "- beta: 0.50" in section


def test_json_output(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    run_dir = write_run(eval_dir / "runs", task="alpha", model="m-1")
    write_grade(
        run_dir,
        grader_doc,
        score=0.9,
        tags=["hat"],
        checks=[{"checker": "c", "ok": True, "metrics": {"latency": 2.0}}],
    )
    result = invoke("report", eval_dir, "--json")
    doc = json.loads(result.output)
    assert doc["eval"] == "demo"
    assert doc["grader"] == "default"
    assert len(doc["grader_version"]) == 7
    assert doc["rows"] == [
        {
            "task": "alpha",
            "config": "default",
            "model": "m-1",
            "outcome": "pass",
            "score": 0.9,
            "tags": ["hat"],
            "metrics": {"latency": 2.0},
            "run_dir": str(run_dir),
        }
    ]


def test_report_without_grades_errors(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    write_run(eval_dir / "runs")
    result = invoke("report", eval_dir, expect_exit=1)
    assert "No grades from grader 'default'" in result.output


def test_failed_runs_excluded_from_report(invoke, make_eval):
    eval_dir, grader_doc = reported_eval(make_eval)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root), grader_doc, score=1.0)
    failed = write_run(runs_root, exit_code=1, output="")
    # Even a grade recorded against a failed run (e.g. graded before the
    # runner failure was noticed) is excluded from the stats
    write_grade(failed, grader_doc, outcome="fail", score=0.0)
    result = invoke("report", eval_dir)
    assert "Excluded: 1 run(s) (runner failed)" in result.output
    assert "Graded: 1 runs (0 failed, 0 ungraded)" in result.output

    as_json = invoke("report", eval_dir, "--json")
    doc = json.loads(as_json.output)
    assert len(doc["rows"]) == 1
    assert doc["excluded_failed_runs"] == 1
