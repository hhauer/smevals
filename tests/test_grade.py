"""Tests for `smevals grade`: built-in and custom Checkers, the Checker
contract, scoring rules, and grade skip/stale/regrade behavior."""

from conftest import python_script, read_yaml, run_dirs, write_run

# A reusable checker driven entirely by its Check configuration:
# score: emit that score, tags: emit those tags, exit: exit with that code
EMIT_CHECKER = python_script("""\
    import json, os, sys

    info = {}
    if "SMEVALS_CHECK_SCORE" in os.environ:
        info["score"] = float(os.environ["SMEVALS_CHECK_SCORE"])
    if "SMEVALS_CHECK_TAGS" in os.environ:
        info["tags"] = os.environ["SMEVALS_CHECK_TAGS"].split()
    if info:
        print(json.dumps(info))
    sys.exit(int(os.environ.get("SMEVALS_CHECK_EXIT", "0")))
    """)

# Writes files named in `write:` (space separated) with `content:`
WRITER_CHECKER = python_script("""\
    import os, pathlib
    content = os.environ.get("SMEVALS_CHECK_CONTENT", "made")
    for name in os.environ.get("SMEVALS_CHECK_WRITE", "").split():
        pathlib.Path(name).write_text(content)
    """)


def emit(**config):
    return {"checker": "../checkers/emit"} | config


def graded(
    invoke, make_eval, grader_doc, *, checkers=None, expect_exit=0, **eval_kwargs
):
    "Scaffold an eval with one grader, run it, grade it, return the Grade"
    checkers = {"emit": EMIT_CHECKER, "writer": WRITER_CHECKER} | (checkers or {})
    eval_dir = make_eval(
        graders={"default": grader_doc}, checkers=checkers, **eval_kwargs
    )
    invoke("run", eval_dir)
    invoke("grade", eval_dir, expect_exit=expect_exit)
    grade_file = run_dirs(eval_dir)[0] / "grades" / "default" / "grade.yaml"
    return read_yaml(grade_file)


# --- built-in checkers ---------------------------------------------------


def test_contains_passes_when_value_in_output(invoke, make_eval):
    grade = graded(
        invoke, make_eval, {"checks": [{"checker": "contains", "value": "hello"}]}
    )
    assert grade["outcome"] == "pass"
    assert grade["checks"] == [{"checker": "contains", "ok": True}]


def test_contains_fails_with_notes(invoke, make_eval):
    grade = graded(
        invoke,
        make_eval,
        {"checks": [{"checker": "contains", "value": "zzz"}]},
        expect_exit=1,
    )
    assert grade["outcome"] == "fail"
    assert grade["checks"][0]["ok"] is False
    assert "does not contain 'zzz'" in grade["checks"][0]["notes"]


def test_xml_valid_against_run_dir_file(invoke, make_eval):
    runner = """\
#!/bin/sh
printf '<a><b/></a>' > doc.xml
printf '<broken' > bad.xml
echo hello
"""
    for file, outcome, note in [
        ("doc.xml", "pass", None),
        ("bad.xml", "fail", "XML parse error"),
        ("nope.xml", "fail", "no such file: nope.xml"),
    ]:
        grade = graded(
            invoke,
            make_eval,
            {"checks": [{"checker": "xml-valid", "file": file}]},
            runner=runner,
            name=f"xml-{file.split('.')[0]}",
            expect_exit=0 if outcome == "pass" else 1,
        )
        assert grade["outcome"] == outcome, file
        if note:
            assert note in grade["checks"][0]["notes"]


def test_xml_valid_prefers_grade_workspace_over_run_dir(invoke, make_eval):
    # The run dir has a broken doc.xml; an earlier Check writes a valid
    # one into the shared workspace, which must win the lookup
    grade = graded(
        invoke,
        make_eval,
        {
            "checks": [
                {
                    "checker": "../checkers/writer",
                    "write": "doc.xml",
                    "content": "<ok/>",
                },
                {"checker": "xml-valid", "file": "doc.xml"},
            ]
        },
        runner="#!/bin/sh\nprintf '<broken' > doc.xml\necho hello\n",
    )
    assert grade["outcome"] == "pass"
    assert [c["ok"] for c in grade["checks"]] == [True, True]


# --- the Checker contract ------------------------------------------------


def test_checker_env_and_cwd_contract(invoke, make_eval):
    envdump = python_script("""\
        import json, os, pathlib
        check = json.loads(os.environ["SMEVALS_CHECK"])
        print(json.dumps({"details": {
            "cwd": pathlib.Path.cwd().name,
            "check_value": os.environ["SMEVALS_CHECK_VALUE"],
            "check_json_checker": check["checker"],
            "task": os.environ["SMEVALS_TASK"],
            "task_extra": os.environ["SMEVALS_TASK_EXTRA"],
            "output_text": (
                pathlib.Path(os.environ["SMEVALS_RUN_DIR"]) / "output.txt"
            ).read_text(),
        }}))
        """)
    grade = graded(
        invoke,
        make_eval,
        {"checks": [{"checker": "../checkers/envdump", "value": "hi"}]},
        checkers={"envdump": envdump},
        tasks={"first": {"prompt": "Say hello", "extra": "widget"}},
    )
    details = grade["checks"][0]["details"]
    assert details["cwd"] == "default"  # the grade workspace dir
    assert details["check_value"] == "hi"
    assert details["check_json_checker"] == "../checkers/envdump"
    assert details["task"] == "first"
    assert details["task_extra"] == "widget"
    assert details["output_text"] == "model=test-model\nSay hello\n"


def test_checker_full_json_output_is_normalized(invoke, make_eval):
    full = python_script("""\
        import json
        print(json.dumps({
            "score": 0.75,
            "metrics": {"lines": 3, "valid": True},
            "tags": ["Wearing A Hat", "ok!"],
            "notes": "looks good",
            "details": {"expected": [1, 2]},
            "custom": "extra",
        }))
        """)
    grade = graded(
        invoke,
        make_eval,
        {"checks": [{"checker": "../checkers/full"}]},
        checkers={"full": full},
    )
    check = grade["checks"][0]
    assert check["ok"] is True
    assert check["score"] == 0.75
    assert check["metrics"] == {"lines": 3, "valid": True}
    assert check["tags"] == ["ok", "wearing_a_hat"]
    assert check["notes"] == "looks good"
    # Unknown keys fold into details rather than landing at the top level
    assert check["details"] == {"expected": [1, 2], "custom": "extra"}
    assert grade["score"] == 0.75
    assert grade["tags"] == ["ok", "wearing_a_hat"]


def test_checker_non_json_stdout_kept_as_details_output(invoke, make_eval):
    plain = python_script('print("plain words")\n')
    grade = graded(
        invoke,
        make_eval,
        {"checks": [{"checker": "../checkers/plain"}]},
        checkers={"plain": plain},
    )
    assert grade["checks"][0]["details"] == {"output": "plain words"}


def test_failing_checker_stderr_becomes_notes(invoke, make_eval):
    noisy = python_script(
        'import sys\nsys.stderr.write("boom happened")\nsys.exit(1)\n'
    )
    grade = graded(
        invoke,
        make_eval,
        {"checks": [{"checker": "../checkers/noisy"}]},
        checkers={"noisy": noisy},
        expect_exit=1,
    )
    assert grade["checks"][0]["ok"] is False
    assert grade["checks"][0]["notes"] == "boom happened"


def test_missing_checker_fails_the_check(invoke, make_eval):
    grade = graded(
        invoke,
        make_eval,
        {"checks": [{"checker": "../checkers/nope"}]},
        expect_exit=1,
    )
    assert grade["outcome"] == "fail"
    assert "is not an executable file" in grade["checks"][0]["notes"]


# --- required, creates, scoring ------------------------------------------


def test_required_failure_halts_and_skips_the_rest(invoke, make_eval):
    grade = graded(
        invoke,
        make_eval,
        {"checks": [emit(exit=1, required=True), emit(score=0.9)]},
        expect_exit=1,
    )
    assert grade["outcome"] == "fail"
    assert grade["checks"][0]["ok"] is False
    assert grade["checks"][1] == {"checker": "../checkers/emit", "skipped": True}
    assert grade["score"] is None


def test_non_required_failure_continues(invoke, make_eval):
    grade = graded(
        invoke,
        make_eval,
        {"checks": [emit(exit=1, score=0.2), emit(score=0.7)]},
        expect_exit=1,
    )
    assert grade["outcome"] == "fail"
    assert [c["ok"] for c in grade["checks"]] == [False, True]
    # Every failure scored itself, so the last score stands
    assert grade["score"] == 0.7


def test_unscored_failure_leaves_grade_unscored(invoke, make_eval):
    # A later score must never stand in for a check that failed silently
    grade = graded(
        invoke,
        make_eval,
        {"checks": [emit(exit=1), emit(score=0.7)]},
        expect_exit=1,
    )
    assert grade["outcome"] == "fail"
    assert grade["score"] is None


def test_last_score_wins(invoke, make_eval):
    grade = graded(invoke, make_eval, {"checks": [emit(score=0.2), emit(score=0.9)]})
    assert grade["score"] == 0.9


def test_pass_threshold(invoke, make_eval):
    below = graded(
        invoke,
        make_eval,
        {"checks": [emit(score=0.4)], "scoring": {"pass_threshold": 0.5}},
        name="below",
        expect_exit=1,
    )
    assert below["outcome"] == "fail"
    at = graded(
        invoke,
        make_eval,
        {"checks": [emit(score=0.5)], "scoring": {"pass_threshold": 0.5}},
        name="at",
    )
    assert at["outcome"] == "pass"


def test_all_ok_no_score_no_threshold_passes(invoke, make_eval):
    grade = graded(invoke, make_eval, {"checks": [emit()]})
    assert grade["outcome"] == "pass"
    assert grade["score"] is None


def test_tags_are_unioned_across_checks(invoke, make_eval):
    grade = graded(invoke, make_eval, {"checks": [emit(tags="b a"), emit(tags="b c")]})
    assert grade["tags"] == ["a", "b", "c"]


def test_creates_satisfied(invoke, make_eval):
    grade = graded(
        invoke,
        make_eval,
        {
            "checks": [
                {"checker": "../checkers/writer", "write": "a.txt", "creates": "a.txt"}
            ]
        },
    )
    assert grade["outcome"] == "pass"


def test_creates_missing_file_fails_check(invoke, make_eval):
    grade = graded(
        invoke,
        make_eval,
        {
            "checks": [
                {
                    "checker": "../checkers/writer",
                    "write": "a.txt",
                    "creates": ["a.txt", "b.txt"],
                }
            ]
        },
        expect_exit=1,
    )
    assert grade["outcome"] == "fail"
    assert grade["checks"][0]["notes"] == "did not create promised file(s): b.txt"


# --- skip / stale / regrade ----------------------------------------------


def test_grade_skips_up_to_date_grades(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    result = invoke("grade", eval_dir)
    assert "Skipped 1 up-to-date grade(s)" in result.output


def test_formatting_only_grader_edit_is_not_stale(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    grader_file = eval_dir / "graders" / "default.yaml"
    grader_file.write_text("# cosmetic comment\n" + grader_file.read_text())
    result = invoke("grade", eval_dir)
    assert "Skipped 1 up-to-date grade(s)" in result.output
    assert "older version" not in result.output


def test_semantic_grader_edit_reports_stale_without_regrading(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    grade_file = run_dirs(eval_dir)[0] / "grades" / "default" / "grade.yaml"
    before = grade_file.read_text()

    (eval_dir / "graders" / "default.yaml").write_text(
        "name: default\nchecks:\n"
        "  - checker: contains\n    value: hello\n"
        "  - checker: contains\n    value: model\n"
    )
    result = invoke("grade", eval_dir)
    assert "older version of grader 'default'" in result.output
    assert "--regrade" in result.output
    assert grade_file.read_text() == before  # untouched without --regrade


def test_regrade_discards_old_grade_and_artifacts(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    grade_dir = run_dirs(eval_dir)[0] / "grades" / "default"
    (grade_dir / "stale-artifact.txt").write_text("old")

    (eval_dir / "graders" / "default.yaml").write_text(
        "name: default\nchecks:\n"
        "  - checker: contains\n    value: hello\n"
        "  - checker: contains\n    value: model\n"
    )
    invoke("grade", eval_dir, "--regrade")
    grade = read_yaml(grade_dir / "grade.yaml")
    assert len(grade["checks"]) == 2
    assert not (grade_dir / "stale-artifact.txt").exists()


def test_grade_only_grades_new_runs(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    invoke("run", eval_dir)
    result = invoke("grade", eval_dir)
    assert "Skipped 2 up-to-date grade(s)" in result.output
    assert len([d for d in run_dirs(eval_dir) if (d / "grades").exists()]) == 3


def test_grade_snapshot_is_byte_for_byte(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    snapshot = run_dirs(eval_dir)[0] / "grades" / "default" / "grader.yaml"
    assert snapshot.read_text() == (eval_dir / "graders" / "default.yaml").read_text()


def test_multiple_graders_coexist(invoke, make_eval):
    eval_dir = make_eval(
        graders={
            "default": {"checks": [{"checker": "contains", "value": "hello"}]},
            "judge": {"checks": [{"checker": "contains", "value": "model"}]},
        }
    )
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    invoke("grade", eval_dir, "-g", "judge")
    grades_dir = run_dirs(eval_dir)[0] / "grades"
    assert sorted(p.name for p in grades_dir.iterdir()) == ["default", "judge"]


def test_grade_unknown_grader_error(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("grade", eval_dir, "-g", "nope", expect_exit=1)
    assert "No grader named 'nope'" in result.output
    assert "default" in result.output


def test_grade_with_no_runs_errors(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("grade", eval_dir, expect_exit=1)
    assert "No runs found" in result.output


def test_grade_skips_failed_runs(invoke, make_eval, tmp_path):
    flag = tmp_path / "api-is-back"
    runner = f'#!/bin/sh\n[ -e "{flag}" ] || exit 7\necho hello\n'
    eval_dir = make_eval(runner=runner)
    invoke("run", eval_dir, expect_exit=1)  # failed run
    flag.write_text("")
    invoke("run", eval_dir)  # good run
    result = invoke("grade", eval_dir)
    assert "Skipped 1 failed run(s)" in result.output
    graded = [d for d in run_dirs(eval_dir) if (d / "grades").exists()]
    assert len(graded) == 1
    assert read_yaml(graded[0] / "run.yaml")["exit_code"] == 0


# --- grade_pending: the grading loop `grade` delegates to -----------------


def collecting_echo(lines):
    "An echo capturing completed lines, honoring grade_pending's nl=False use"
    buffer = []

    def echo(message="", nl=True):
        buffer.append(str(message))
        if nl:
            lines.append("".join(buffer))
            buffer.clear()

    return echo


def test_grade_pending_counts_and_echoes(invoke, make_eval):
    from smevals.cli import grade_pending

    eval_dir = make_eval()
    invoke("run", eval_dir)  # ungraded
    invoke("run", eval_dir, "-g")  # already graded, up to date
    write_run(eval_dir / "runs", exit_code=1)  # failed: never graded

    grader_path = eval_dir / "graders" / "default.yaml"
    lines = []
    result = grade_pending(
        eval_dir / "runs",
        "default",
        read_yaml(grader_path),
        grader_path,
        collecting_echo(lines),
    )
    assert result == {
        "graded": 1,
        "skipped": 1,
        "stale": 0,
        "failed_runs": 1,
        "failures": 0,
    }
    assert len(lines) == 1
    assert lines[0].endswith("pass")
    # every non-failed run now carries a grade from this grader
    graded = [
        d
        for d in run_dirs(eval_dir)
        if (d / "grades" / "default" / "grade.yaml").exists()
    ]
    assert len(graded) == 2


def test_grade_pending_counts_stale_without_regrading(invoke, make_eval):
    from smevals.cli import grade_pending

    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("grade", eval_dir)
    grader_path = eval_dir / "graders" / "default.yaml"
    grader_path.write_text(
        "name: default\nchecks:\n"
        "  - checker: contains\n    value: hello\n"
        "  - checker: contains\n    value: model\n"
    )
    lines = []
    result = grade_pending(
        eval_dir / "runs",
        "default",
        read_yaml(grader_path),
        grader_path,
        collecting_echo(lines),
    )
    assert result == {
        "graded": 0,
        "skipped": 0,
        "stale": 1,
        "failed_runs": 0,
        "failures": 0,
    }
    assert lines == []


def test_grade_pending_counts_failing_grades(invoke, make_eval):
    from smevals.cli import grade_pending

    eval_dir = make_eval(
        graders={"default": {"checks": [{"checker": "contains", "value": "zzz"}]}}
    )
    invoke("run", eval_dir)
    grader_path = eval_dir / "graders" / "default.yaml"
    result = grade_pending(
        eval_dir / "runs",
        "default",
        read_yaml(grader_path),
        grader_path,
        collecting_echo([]),
    )
    assert result["graded"] == 1
    assert result["failures"] == 1
