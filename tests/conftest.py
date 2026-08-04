"""Shared fixtures: scaffold Evals in a tmp dir and fabricate Runs/Grades.

Runners and Checkers are real executables (sh or Python scripts written
with the current interpreter's shebang), so these tests exercise the
documented subprocess contracts, not internal shortcuts.
"""

import itertools
import json
import os
import sys
import textwrap

import pytest
import yaml
from click.testing import CliRunner

from smevals.cli import cli, slugify

ECHO_RUNNER = """\
#!/bin/sh
printf 'model=%s\\n' "$SMEVALS_MODEL"
printf '%s\\n' "${SMEVALS_PROMPT-<no prompt>}"
"""

# A fake `lms` CLI for sweep tests: records every invocation's argv to
# lms-argv.log beside itself, lists two local LLMs on `ls`, and fails
# `load` for the model named in FAKE_LMS_FAIL_LOAD. The `ls` output
# mirrors the REAL tool's format verbatim-style (see the ground-truth
# capture in tests/real-lms-ls.txt): a summary line, single-token LLM /
# EMBEDDING section headers, " (N variant)" suffixes, DEVICE and
# "✓ LOADED" columns, and an embedding model that must never be treated
# as a loadable chat model. There is no machine-readable ls, so the
# whole sweep suite exercises the parser against this real shape.
FAKE_LMS = """\
import json, os, pathlib, sys

here = pathlib.Path(__file__).resolve().parent
with (here / "lms-argv.log").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["ls"]:
    print("")
    print("You have 3 models, taking up 21.08 GB of disk space.")
    print("")
    print(
        "LLM                                   PARAMS      ARCH         "
        "SIZE        DEVICE            "
    )
    print(
        "local-alpha (1 variant)               27B         qwen3        "
        "16.00 GB    Local     \\u2713 LOADED"
    )
    print(
        "local-beta                            8B          llama        "
        "5.00 GB     Local             "
    )
    print("")
    print(
        "EMBEDDING                               PARAMS    ARCH          "
        "SIZE        DEVICE    "
    )
    print(
        "fake-embedding-model                              Nomic BERT    "
        "84.11 MB    Local"
    )
if sys.argv[1:2] == ["load"] and os.environ.get("FAKE_LMS_FAIL_LOAD") == sys.argv[2]:
    print("model not found", file=sys.stderr)
    sys.exit(1)
"""


def lms_calls(log_path):
    "Every argv the fake lms recorded, in call order"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines()]


@pytest.fixture
def fake_lms(monkeypatch, tmp_path):
    """A fake `lms` executable first on PATH, plus a redirected HOME so
    sweep.lms_path can never fall back to a real ~/.lmstudio/bin/lms -
    sweep tests must NEVER touch the real LM Studio on this machine.
    Returns the argv log path (see lms_calls).
    """
    bin_dir = tmp_path / "fake-lms-bin"
    bin_dir.mkdir()
    home = tmp_path / "fake-home"
    home.mkdir()
    write_executable(bin_dir / "lms", python_script(FAKE_LMS))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    return bin_dir / "lms-argv.log"


def python_script(body):
    "An executable script body using the same interpreter as the tests"
    return f"#!{sys.executable}\n" + textwrap.dedent(body)


def write_executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def read_yaml(path):
    return yaml.safe_load(path.read_text())


def run_dirs(root):
    "Every Run directory under an eval or runs root, in sorted order"
    if (root / "runs").exists():
        root = root / "runs"
    return sorted(p.parent for p in root.rglob("run.yaml"))


@pytest.fixture
def invoke():
    runner = CliRunner()

    def _invoke(*args, expect_exit=0):
        result = runner.invoke(cli, [str(a) for a in args], catch_exceptions=False)
        assert result.exit_code == expect_exit, result.output
        return result

    return _invoke


@pytest.fixture
def make_eval(tmp_path):
    def _make(
        name="demo",
        *,
        tasks=None,
        configs=None,
        graders=None,
        runner=ECHO_RUNNER,
        checkers=None,
        root=None,
    ):
        eval_dir = (root or tmp_path) / name
        (eval_dir / "tasks").mkdir(parents=True)
        (eval_dir / "configs").mkdir()
        (eval_dir / "graders").mkdir()
        (eval_dir / "eval.yaml").write_text(
            yaml.safe_dump({"name": name, "description": f"The {name} eval"})
        )
        if runner is not None:
            write_executable(eval_dir / "run-llm", runner)
        if tasks is None:
            tasks = {"first": {"prompt": "Say hello"}}
        for stem, doc in tasks.items():
            (eval_dir / "tasks" / f"{stem}.yaml").write_text(
                yaml.safe_dump({"name": stem} | doc)
            )
        if configs is None:
            configs = {"default": {"runner": "../run-llm", "model": "test-model"}}
        for stem, doc in configs.items():
            (eval_dir / "configs" / f"{stem}.yaml").write_text(
                yaml.safe_dump({"name": stem} | doc)
            )
        if graders is None:
            graders = {
                "default": {"checks": [{"checker": "contains", "value": "hello"}]}
            }
        for stem, doc in graders.items():
            (eval_dir / "graders" / f"{stem}.yaml").write_text(
                yaml.safe_dump({"name": stem} | doc)
            )
        for rel, body in (checkers or {}).items():
            write_executable(eval_dir / "checkers" / rel, body)
        return eval_dir

    return _make


# --- fabricated runs and grades, for report/site tests -------------------

_stamp = itertools.count()


def write_run(
    runs_root,
    *,
    task="first",
    config="default",
    model="test-model",
    output="hello world\n",
    exit_code=0,
):
    "Fabricate one Run directory in the documented on-disk format"
    n = next(_stamp)
    run_dir = (
        runs_root
        / task
        / config
        / slugify(model)
        / f"2026-01-01T{n // 3600:02d}-{n // 60 % 60:02d}-{n % 60:02d}Z"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "output.txt").write_text(output)
    (run_dir / "run.yaml").write_text(
        yaml.safe_dump(
            {
                "task": {"name": task},
                "config": {"name": config, "runner": "run-llm", "model": model},
                "started": "2026-01-01T00:00:00+00:00",
                "duration_seconds": 0.5,
                "exit_code": exit_code,
            },
            sort_keys=False,
        )
    )
    return run_dir


def write_grade(
    run_dir,
    snapshot_doc,
    *,
    grader="default",
    outcome="pass",
    score=None,
    tags=(),
    checks=(),
):
    "Fabricate a Grade whose snapshot matches snapshot_doc"
    grade_dir = run_dir / "grades" / grader
    grade_dir.mkdir(parents=True)
    (grade_dir / "grader.yaml").write_text(yaml.safe_dump(snapshot_doc))
    (grade_dir / "grade.yaml").write_text(
        yaml.safe_dump(
            {
                "grader": grader,
                "graded": "2026-01-01T00:00:01+00:00",
                "outcome": outcome,
                "score": score,
                "tags": list(tags),
                "checks": list(checks),
            },
            sort_keys=False,
        )
    )
    return grade_dir
