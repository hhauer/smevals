"""Tests for `smevals run` and the Runner contract."""

from datetime import datetime, timezone

import smevals.cli
from conftest import read_yaml, run_dirs, write_run
from smevals.cli import compute_remaining


def test_run_records_output_and_run_yaml(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("run", eval_dir)

    dirs = run_dirs(eval_dir)
    assert len(dirs) == 1
    run_dir = dirs[0]
    # runs/<task>/<config>/<model-slug>/<timestamp>/
    assert run_dir.relative_to(eval_dir / "runs").parts[:3] == (
        "first",
        "default",
        "test-model",
    )
    assert (run_dir / "output.txt").read_text() == "model=test-model\nSay hello\n"
    assert not (run_dir / "stderr.txt").exists()

    record = read_yaml(run_dir / "run.yaml")
    assert record["task"] == {"name": "first", "prompt": "Say hello"}
    assert record["config"]["name"] == "default"
    assert record["config"]["model"] == "test-model"
    assert record["config"]["runner"].endswith("run-llm")
    assert record["exit_code"] == 0
    assert isinstance(record["duration_seconds"], float)
    datetime.fromisoformat(record["started"])  # parseable timestamp
    assert "ok" in result.output


def test_prompt_env_only_set_when_task_has_one(invoke, make_eval):
    runner = """\
#!/bin/sh
printf '%s\\n' "${SMEVALS_PROMPT-unset}"
printf '%s\\n' "$SMEVALS_TASK_PAYLOAD"
printf '%s\\n' "$SMEVALS_TASK"
"""
    eval_dir = make_eval(tasks={"data-task": {"payload": "abc"}}, runner=runner)
    invoke("run", eval_dir)
    output = (run_dirs(eval_dir)[0] / "output.txt").read_text()
    assert output == "unset\nabc\ndata-task\n"


def test_stderr_captured_when_present(invoke, make_eval):
    eval_dir = make_eval(runner="#!/bin/sh\necho warned >&2\necho hello\n")
    invoke("run", eval_dir)
    assert (run_dirs(eval_dir)[0] / "stderr.txt").read_text() == "warned\n"


def test_failing_runner_marks_run_failed_and_exits_nonzero(invoke, make_eval):
    eval_dir = make_eval(runner="#!/bin/sh\necho partial\nexit 3\n")
    result = invoke("run", eval_dir, expect_exit=1)
    assert "1 run(s) failed" in result.output

    run_dir = run_dirs(eval_dir)[0]
    assert read_yaml(run_dir / "run.yaml")["exit_code"] == 3
    # The run is still recorded, output included
    assert (run_dir / "output.txt").read_text() == "partial\n"


def test_model_options_override_config_and_are_slugified(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir, "-m", "My Model", "-m", "other")
    model_dirs = {d.relative_to(eval_dir / "runs").parts[2] for d in run_dirs(eval_dir)}
    assert model_dirs == {"My-Model", "other"}
    # The exact (unslugified) model name is preserved in run.yaml
    models = {read_yaml(d / "run.yaml")["config"]["model"] for d in run_dirs(eval_dir)}
    assert models == {"My Model", "other"}


def test_task_selection(invoke, make_eval):
    eval_dir = make_eval(
        tasks={"first": {"prompt": "one"}, "second": {"prompt": "two"}}
    )
    invoke("run", eval_dir, "-t", "second")
    dirs = run_dirs(eval_dir)
    assert len(dirs) == 1
    assert dirs[0].relative_to(eval_dir / "runs").parts[0] == "second"


def test_unknown_task_error_lists_available(invoke, make_eval):
    eval_dir = make_eval(
        tasks={"first": {"prompt": "one"}, "second": {"prompt": "two"}}
    )
    result = invoke("run", eval_dir, "-t", "nope", expect_exit=1)
    assert "No such task(s): nope" in result.output
    assert "first, second" in result.output


def test_unknown_config_error_lists_available(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("run", eval_dir, "-c", "prod", expect_exit=1)
    assert "No config named 'prod'" in result.output
    assert "default" in result.output


def test_runner_must_be_executable(invoke, make_eval):
    eval_dir = make_eval(runner=None)
    (eval_dir / "run-llm").write_text("#!/bin/sh\necho hi\n")  # not chmod +x
    result = invoke("run", eval_dir, expect_exit=1)
    assert "is not an executable file" in result.output


def test_not_an_eval_error(invoke, tmp_path):
    result = invoke("run", tmp_path, expect_exit=1)
    assert "no eval.yaml found" in result.output


def test_invalid_grader_fails_before_any_run(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("run", eval_dir, "-g", "nosuch", expect_exit=1)
    assert "No grader named 'nosuch'" in result.output
    assert not (eval_dir / "runs").exists()


def test_grade_flag_grades_each_run_immediately(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("run", eval_dir, "-g")
    assert "grade: pass" in result.output
    grade = read_yaml(run_dirs(eval_dir)[0] / "grades" / "default" / "grade.yaml")
    assert grade["outcome"] == "pass"


def test_grade_flag_failure_exits_nonzero(invoke, make_eval):
    eval_dir = make_eval(
        graders={
            "default": {"checks": [{"checker": "contains", "value": "zzz-absent"}]}
        }
    )
    result = invoke("run", eval_dir, "-g", expect_exit=1)
    assert "grade: fail" in result.output
    assert "1 run(s) graded as fail" in result.output


def test_runs_dir_option_namespaces_by_eval_name(invoke, make_eval, tmp_path):
    eval_dir = make_eval(name="my-eval")
    external = tmp_path / "external-runs"
    invoke("run", eval_dir, "--runs-dir", external)
    assert not (eval_dir / "runs").exists()
    dirs = run_dirs(external / "my-eval")
    assert len(dirs) == 1


def test_same_second_runs_get_numeric_suffix(invoke, make_eval, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(smevals.cli, "datetime", FrozenDatetime)
    eval_dir = make_eval()
    invoke("run", eval_dir)
    invoke("run", eval_dir)
    names = sorted(d.name for d in run_dirs(eval_dir))
    assert names == ["2026-01-01T00-00-00Z", "2026-01-01T00-00-00Z-2"]


# --- -n/--repeat: target sample size -------------------------------------


def test_repeat_tops_up_to_target(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)  # one pre-existing run
    invoke("run", eval_dir, "-n", "3")
    assert len(run_dirs(eval_dir)) == 3


def test_repeat_is_idempotent(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir, "-n", "3")
    assert len(run_dirs(eval_dir)) == 3
    result = invoke("run", eval_dir, "-n", "3")
    assert len(run_dirs(eval_dir)) == 3
    assert "already have 3 run(s)" in result.output


def test_failed_runs_do_not_count_toward_target(invoke, make_eval, tmp_path):
    # A Run whose Runner exited non-zero is a harness failure, not
    # evidence - re-running the command executes replacements for it
    flag = tmp_path / "api-is-back"
    runner = f'#!/bin/sh\n[ -e "{flag}" ] || exit 7\necho hello\n'
    eval_dir = make_eval(runner=runner)
    invoke("run", eval_dir, "-n", "2", expect_exit=1)
    # Bounded: the shortfall is attempted once per invocation, no retry loop
    assert len(run_dirs(eval_dir)) == 2
    flag.write_text("")
    invoke("run", eval_dir, "-n", "2")
    dirs = run_dirs(eval_dir)
    assert len(dirs) == 4  # the failed runs stay on disk, untouched
    good = [d for d in dirs if read_yaml(d / "run.yaml")["exit_code"] == 0]
    assert len(good) == 2
    result = invoke("run", eval_dir, "-n", "2")  # target met by good runs
    assert "already have 2 run(s)" in result.output
    assert len(run_dirs(eval_dir)) == 4


def test_grade_flag_skips_failed_runs(invoke, make_eval):
    eval_dir = make_eval(runner="#!/bin/sh\necho oops\nexit 1\n")
    result = invoke("run", eval_dir, "-g", expect_exit=1)
    assert "grade: skipped (run failed)" in result.output
    assert "graded as fail" not in result.output
    assert not (run_dirs(eval_dir)[0] / "grades").exists()


def test_repeat_counts_per_model(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir, "-m", "m-a", "-n", "2")
    invoke("run", eval_dir, "-m", "m-a", "-m", "m-b", "-n", "2")
    by_model = {}
    for d in run_dirs(eval_dir):
        model = d.relative_to(eval_dir / "runs").parts[2]
        by_model[model] = by_model.get(model, 0) + 1
    assert by_model == {"m-a": 2, "m-b": 2}


def test_repeat_with_grade_only_grades_new_runs(invoke, make_eval):
    eval_dir = make_eval()
    invoke("run", eval_dir)  # existing, left ungraded
    invoke("run", eval_dir, "-n", "2", "-g")
    dirs = run_dirs(eval_dir)
    graded = [d for d in dirs if (d / "grades" / "default" / "grade.yaml").exists()]
    assert len(dirs) == 2
    assert len(graded) == 1


def test_repeat_runs_in_full_passes(invoke, make_eval, tmp_path):
    # Repeat index outermost: pass over every task, then repeat - so an
    # interrupted session leaves balanced samples, not 3 of one task
    log = tmp_path / "order.log"
    runner = f'#!/bin/sh\necho "$SMEVALS_TASK" >> {log}\necho hello\n'
    eval_dir = make_eval(
        tasks={"aa": {"prompt": "x"}, "bb": {"prompt": "y"}}, runner=runner
    )
    invoke("run", eval_dir, "-n", "2")
    assert log.read_text().splitlines() == ["aa", "bb", "aa", "bb"]


def test_repeat_must_be_at_least_one(invoke, make_eval):
    eval_dir = make_eval()
    result = invoke("run", eval_dir, "-n", "0", expect_exit=2)
    assert "Invalid value" in result.output


# --- compute_remaining: the shortfall logic `run` delegates to ------------


def test_compute_remaining_without_target_is_one_per_pair(tmp_path):
    task_docs = [{"name": "aa"}, {"name": "bb"}]
    remaining = compute_remaining(tmp_path, task_docs, ["m-1", "m-2"], "default", None)
    assert remaining == {
        ("aa", "m-1"): 1,
        ("aa", "m-2"): 1,
        ("bb", "m-1"): 1,
        ("bb", "m-2"): 1,
    }


def test_compute_remaining_counts_only_successful_runs(tmp_path):
    write_run(tmp_path, task="aa", model="m-1")
    write_run(tmp_path, task="aa", model="m-1", exit_code=1)
    remaining = compute_remaining(tmp_path, [{"name": "aa"}], ["m-1"], "default", 3)
    assert remaining == {("aa", "m-1"): 2}


def test_compute_remaining_met_or_exceeded_target_is_zero(tmp_path):
    for _ in range(3):
        write_run(tmp_path, task="aa", model="m-1")
    remaining = compute_remaining(tmp_path, [{"name": "aa"}], ["m-1"], "default", 2)
    assert remaining == {("aa", "m-1"): 0}


def test_compute_remaining_is_per_config(tmp_path):
    write_run(tmp_path, task="aa", model="m-1", config="other")
    remaining = compute_remaining(tmp_path, [{"name": "aa"}], ["m-1"], "default", 2)
    assert remaining == {("aa", "m-1"): 2}
