"""Unit tests for smevals.sweep: the lms CLI integration helpers, grader
treatment defaults, SweepSpec validation and sweep planning.

The orchestrator's full lifecycle (run_sweep driving real runs under the
studio server) is covered in test_studio.py. Every test here that could
invoke `lms` points PATH *and* HOME (lms_path falls back to
~/.lmstudio/bin/lms) at scratch directories, so the real LM Studio on
this machine is never touched.
"""

import pytest

from conftest import FAKE_LMS, lms_calls, python_script, write_executable, write_run
from smevals import sweep


@pytest.fixture
def isolated_lms_env(monkeypatch, tmp_path):
    "A PATH holding only a scratch bin dir, and a HOME with no .lmstudio"
    bin_dir = tmp_path / "lms-bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    return bin_dir


# --- lms_path -------------------------------------------------------------


def test_lms_path_finds_lms_on_path(isolated_lms_env):
    write_executable(isolated_lms_env / "lms", python_script(FAKE_LMS))
    assert sweep.lms_path() == isolated_lms_env / "lms"


def test_lms_path_falls_back_to_lmstudio_bin(isolated_lms_env, tmp_path):
    home_lms = tmp_path / "home" / ".lmstudio" / "bin" / "lms"
    write_executable(home_lms, python_script(FAKE_LMS))
    assert sweep.lms_path() == home_lms


def test_lms_path_none_when_absent(isolated_lms_env):
    assert sweep.lms_path() is None


# --- lms_inventory --------------------------------------------------------


def test_lms_inventory_scrapes_ls_output(isolated_lms_env):
    write_executable(isolated_lms_env / "lms", python_script(FAKE_LMS))
    assert sweep.lms_inventory() == {"local-alpha", "local-beta"}


def test_lms_inventory_empty_without_lms(isolated_lms_env):
    assert sweep.lms_inventory() == set()


def test_lms_inventory_empty_on_nonzero_exit(isolated_lms_env):
    write_executable(
        isolated_lms_env / "lms", python_script("import sys\nsys.exit(1)\n")
    )
    assert sweep.lms_inventory() == set()


# --- lms_load / lms_unload_all --------------------------------------------


def test_lms_load_passes_context_length_and_yes(isolated_lms_env):
    write_executable(isolated_lms_env / "lms", python_script(FAKE_LMS))
    ok, message = sweep.lms_load("local-alpha", 32768)
    assert ok
    assert lms_calls(isolated_lms_env / "lms-argv.log") == [
        ["load", "local-alpha", "-c", "32768", "-y"]
    ]


def test_lms_load_default_context_length_is_32768(isolated_lms_env):
    # The JIT-load default of 8192 tokens starves thinking-heavy models
    # into empty output - the sweep always loads with the spec'd default
    write_executable(isolated_lms_env / "lms", python_script(FAKE_LMS))
    ok, _ = sweep.lms_load("local-alpha")
    assert ok
    assert lms_calls(isolated_lms_env / "lms-argv.log") == [
        ["load", "local-alpha", "-c", "32768", "-y"]
    ]


def test_lms_load_failure_returns_message(isolated_lms_env, monkeypatch):
    write_executable(isolated_lms_env / "lms", python_script(FAKE_LMS))
    monkeypatch.setenv("FAKE_LMS_FAIL_LOAD", "local-alpha")
    ok, message = sweep.lms_load("local-alpha")
    assert not ok
    assert "model not found" in message


def test_lms_load_without_lms_fails_with_message(isolated_lms_env):
    ok, message = sweep.lms_load("local-alpha")
    assert not ok
    assert "lms" in message


def test_lms_unload_all(isolated_lms_env):
    write_executable(isolated_lms_env / "lms", python_script(FAKE_LMS))
    sweep.lms_unload_all()
    assert lms_calls(isolated_lms_env / "lms-argv.log") == [["unload", "--all"]]


def test_lms_unload_all_without_lms_is_a_noop(isolated_lms_env):
    sweep.lms_unload_all()  # must not raise


# --- grader treatment defaults --------------------------------------------


def test_default_treatments_defer_model_keyed_graders(make_eval):
    eval_dir = make_eval(
        graders={
            "default": {"checks": [{"checker": "contains", "value": "x"}]},
            "judge": {
                "checks": [{"checker": "contains", "value": "x", "model": "gpt-4.1"}]
            },
        }
    )
    assert sweep.default_treatments(eval_dir) == {
        "default": "inline",
        "judge": "deferred",
    }


def test_default_treatments_tolerate_broken_grader_yaml(make_eval):
    eval_dir = make_eval()
    (eval_dir / "graders" / "broken.yaml").write_text("checks: [unterminated\n")
    treatments = sweep.default_treatments(eval_dir)
    assert treatments["broken"] == "inline"


# --- validate_spec --------------------------------------------------------


def test_validate_spec_fills_defaults(make_eval):
    eval_dir = make_eval(name="demo")
    spec = sweep.validate_spec({"models": ["m-1"]}, {"demo": eval_dir})
    assert spec == {
        "evals": ["demo"],
        "models": ["m-1"],
        "n": 1,
        "graders": {"demo": {"default": "inline"}},
        "context_length": 32768,
    }


def test_validate_spec_applies_grader_overrides(make_eval):
    eval_dir = make_eval(name="demo")
    spec = sweep.validate_spec(
        {"models": ["m-1"], "graders": {"demo": {"default": "skip"}}},
        {"demo": eval_dir},
    )
    assert spec["graders"] == {"demo": {"default": "skip"}}


@pytest.mark.parametrize(
    "body,fragment",
    [
        ({"models": ["m"], "evals": ["nope"]}, "no such eval"),
        ({"models": ["m"], "evals": []}, "evals"),
        ({"models": []}, "models"),
        ({}, "models"),
        ({"models": ["m", "m"]}, "duplicate"),
        ({"models": ["m"], "n": 0}, "n must be"),
        ({"models": ["m"], "n": "three"}, "n must be"),
        ({"models": ["m"], "context_length": 0}, "context_length"),
        ({"models": ["m"], "graders": {"nope": {}}}, "no such eval"),
        ({"models": ["m"], "graders": {"demo": {"nope": "inline"}}}, "no grader"),
        ({"models": ["m"], "graders": {"demo": {"default": "later"}}}, "treatment"),
    ],
)
def test_validate_spec_rejects_bad_specs(make_eval, body, fragment):
    eval_dir = make_eval(name="demo")
    with pytest.raises(ValueError, match=fragment):
        sweep.validate_spec(body, {"demo": eval_dir})


def test_validate_spec_plan_mode_allows_missing_models(make_eval):
    eval_dir = make_eval(name="demo")
    spec = sweep.validate_spec({}, {"demo": eval_dir}, require_models=False)
    assert spec["models"] == []
    assert spec["graders"] == {"demo": {"default": "inline"}}


# --- plan_preview ---------------------------------------------------------


def test_plan_preview_reports_shortfall_per_cell(make_eval):
    eval_dir = make_eval(name="demo")
    write_run(eval_dir / "runs", task="first", model="m-1")
    write_run(eval_dir / "runs", task="first", model="m-1")
    write_run(eval_dir / "runs", task="first", model="m-1", exit_code=1)

    spec = sweep.validate_spec({"models": ["m-1", "m-2"], "n": 3}, {"demo": eval_dir})
    plan = sweep.plan_preview(spec, {"demo": eval_dir})
    assert plan["cells"] == {
        "demo\x00m-1": {"target": 3, "have": 2, "remaining": 1},
        "demo\x00m-2": {"target": 3, "have": 0, "remaining": 3},
    }
    assert plan["graders"] == {"demo": {"default": "inline"}}
    assert plan["n"] == 3
