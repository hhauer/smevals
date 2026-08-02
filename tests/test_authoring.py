"""Tests for smevals.authoring: pure Eval scaffolding and validation.

No HTTP, no subprocesses beyond what the fixtures themselves execute -
these exercise scaffold_eval and validate_eval directly against real
files in tmp_path, the repo's usual style.
"""

import os
import pathlib

import pytest
import yaml

from conftest import python_script, write_executable
from smevals.authoring import FILE_SCHEMAS, scaffold_eval, validate_eval

REPO_ROOT = pathlib.Path(__file__).parent.parent


def read_yaml(path):
    return yaml.safe_load(path.read_text())


# --- scaffold_eval ---------------------------------------------------------


class TestScaffoldEval:
    def test_creates_canonical_layout(self, tmp_path):
        eval_dir = scaffold_eval(tmp_path, "My Eval", "What it measures")

        assert eval_dir == tmp_path / "my-eval"
        assert read_yaml(eval_dir / "eval.yaml") == {
            "name": "My Eval",
            "description": "What it measures",
        }
        task = read_yaml(eval_dir / "tasks" / "example.yaml")
        assert task["name"] == "example"
        assert task["prompt"]  # a non-empty placeholder
        assert read_yaml(eval_dir / "configs" / "default.yaml") == {
            "name": "default",
            "runner": "../run-llm",
            "model": "gpt-4.1-mini",
        }
        assert read_yaml(eval_dir / "graders" / "default.yaml") == {
            "name": "default",
            "checks": [{"checker": "contains", "value": "", "required": True}],
            "scoring": {"pass_threshold": 1.0},
        }
        assert (eval_dir / ".gitignore").read_text() == "runs\n"

    def test_run_llm_matches_code_review_example(self, tmp_path):
        # The starter shipped in the package must be byte-identical to the
        # documented guarded runner example, and scaffolding copies it in.
        starter = (REPO_ROOT / "src" / "smevals" / "starter-run-llm").read_text()
        example = (REPO_ROOT / "examples" / "code-review" / "run-llm").read_text()
        assert starter == example

        eval_dir = scaffold_eval(tmp_path, "demo", "")
        assert (eval_dir / "run-llm").read_text() == starter

    def test_run_llm_is_executable(self, tmp_path):
        eval_dir = scaffold_eval(tmp_path, "demo", "")
        runner = eval_dir / "run-llm"
        assert os.access(runner, os.X_OK)

    def test_slug_lowercases_and_replaces_spaces(self, tmp_path):
        eval_dir = scaffold_eval(tmp_path, "Regex Golf", "")
        assert eval_dir.name == "regex-golf"

    def test_slug_strips_disallowed_characters(self, tmp_path):
        eval_dir = scaffold_eval(tmp_path, "Weird!! Name??", "")
        assert eval_dir.name == "weird-name"

    def test_raises_on_existing_directory(self, tmp_path):
        (tmp_path / "demo").mkdir()
        with pytest.raises(ValueError):
            scaffold_eval(tmp_path, "demo", "")

    def test_raises_on_empty_name(self, tmp_path):
        with pytest.raises(ValueError):
            scaffold_eval(tmp_path, "", "")

    def test_raises_on_whitespace_only_name(self, tmp_path):
        with pytest.raises(ValueError):
            scaffold_eval(tmp_path, "   ", "")


# --- validate_eval -----------------------------------------------------


class TestValidateEval:
    def test_valid_eval_has_no_problems(self, make_eval):
        eval_dir = make_eval()
        assert validate_eval(eval_dir) == []

    def test_yaml_syntax_error(self, make_eval):
        eval_dir = make_eval()
        (eval_dir / "tasks" / "first.yaml").write_text("foo: [1, 2\n")

        problems = validate_eval(eval_dir)
        assert any(
            p["file"] == "tasks/first.yaml"
            and p["path"] == ""
            and p["problem"].startswith("invalid YAML:")
            for p in problems
        )

    def test_eval_missing_name(self, make_eval):
        eval_dir = make_eval()
        (eval_dir / "eval.yaml").write_text(
            yaml.safe_dump({"description": "no name here"})
        )

        problems = validate_eval(eval_dir)
        assert {
            "file": "eval.yaml",
            "path": "name",
            "problem": "missing required key: name",
        } in problems

    def test_task_missing_name(self, make_eval):
        eval_dir = make_eval()
        (eval_dir / "tasks" / "first.yaml").write_text(yaml.safe_dump({"prompt": "hi"}))

        problems = validate_eval(eval_dir)
        assert {
            "file": "tasks/first.yaml",
            "path": "name",
            "problem": "missing required key: name",
        } in problems

    def test_task_missing_prompt_is_a_warning(self, make_eval):
        eval_dir = make_eval(tasks={"noprompt": {}})

        problems = validate_eval(eval_dir)
        assert {
            "file": "tasks/noprompt.yaml",
            "path": "prompt",
            "problem": "warning: no prompt - the Runner will not receive SMEVALS_PROMPT",
        } in problems

    def test_config_missing_name(self, make_eval):
        eval_dir = make_eval(
            configs={"default": {"runner": "../run-llm", "model": "m"}}
        )
        (eval_dir / "configs" / "default.yaml").write_text(
            yaml.safe_dump({"name": "", "runner": "../run-llm", "model": "m"})
        )

        problems = validate_eval(eval_dir)
        assert {
            "file": "configs/default.yaml",
            "path": "name",
            "problem": "missing required key: name",
        } in problems

    def test_config_missing_runner(self, make_eval):
        eval_dir = make_eval(configs={"default": {"model": "m"}})

        problems = validate_eval(eval_dir)
        assert {
            "file": "configs/default.yaml",
            "path": "runner",
            "problem": "missing required key: runner",
        } in problems

    def test_config_runner_does_not_resolve(self, make_eval):
        eval_dir = make_eval(
            configs={"default": {"runner": "../ghost-runner", "model": "m"}}
        )

        problems = validate_eval(eval_dir)
        matches = [
            p
            for p in problems
            if p["file"] == "configs/default.yaml" and p["path"] == "runner"
        ]
        assert len(matches) == 1
        assert "does not resolve" in matches[0]["problem"]

    def test_config_runner_not_executable(self, make_eval):
        eval_dir = make_eval(
            configs={"default": {"runner": "../not-exec", "model": "m"}}
        )
        (eval_dir / "not-exec").write_text("echo hi\n")  # deliberately not chmod'd

        problems = validate_eval(eval_dir)
        matches = [
            p
            for p in problems
            if p["file"] == "configs/default.yaml" and p["path"] == "runner"
        ]
        assert len(matches) == 1
        assert "not executable" in matches[0]["problem"]

    def test_grader_missing_name(self, make_eval):
        eval_dir = make_eval(
            graders={
                "default": {
                    "name": "",
                    "checks": [{"checker": "contains", "value": "x"}],
                }
            }
        )

        problems = validate_eval(eval_dir)
        assert {
            "file": "graders/default.yaml",
            "path": "name",
            "problem": "missing required key: name",
        } in problems

    def test_grader_missing_checks(self, make_eval):
        eval_dir = make_eval(graders={"default": {}})

        problems = validate_eval(eval_dir)
        assert {
            "file": "graders/default.yaml",
            "path": "checks",
            "problem": "missing required key: checks",
        } in problems

    def test_grader_checker_not_builtin_or_resolvable(self, make_eval):
        eval_dir = make_eval(
            graders={"default": {"checks": [{"checker": "totally-bogus"}]}}
        )

        problems = validate_eval(eval_dir)
        matches = [
            p
            for p in problems
            if p["file"] == "graders/default.yaml" and p["path"] == "checks.0.checker"
        ]
        assert len(matches) == 1
        assert "does not resolve" in matches[0]["problem"]

    def test_grader_checker_custom_executable_is_valid(self, make_eval):
        eval_dir = make_eval(
            graders={"default": {"checks": [{"checker": "../checkers/foo"}]}},
            checkers={"foo": python_script("import sys\nsys.exit(0)\n")},
        )

        assert validate_eval(eval_dir) == []

    def test_grader_checker_custom_not_executable(self, make_eval):
        eval_dir = make_eval(
            graders={"default": {"checks": [{"checker": "../checkers/bar"}]}}
        )
        (eval_dir / "checkers").mkdir()
        (eval_dir / "checkers" / "bar").write_text("#!/bin/sh\necho hi\n")

        problems = validate_eval(eval_dir)
        matches = [
            p
            for p in problems
            if p["file"] == "graders/default.yaml" and p["path"] == "checks.0.checker"
        ]
        assert len(matches) == 1
        assert "not executable" in matches[0]["problem"]

    def test_bad_pass_threshold_out_of_range(self, make_eval):
        eval_dir = make_eval(
            graders={
                "default": {
                    "checks": [{"checker": "contains", "value": "x"}],
                    "scoring": {"pass_threshold": 1.5},
                }
            }
        )

        problems = validate_eval(eval_dir)
        matches = [p for p in problems if p["path"] == "scoring.pass_threshold"]
        assert len(matches) == 1

    def test_bad_pass_threshold_non_numeric(self, make_eval):
        eval_dir = make_eval(
            graders={
                "default": {
                    "checks": [{"checker": "contains", "value": "x"}],
                    "scoring": {"pass_threshold": "high"},
                }
            }
        )

        problems = validate_eval(eval_dir)
        matches = [p for p in problems if p["path"] == "scoring.pass_threshold"]
        assert len(matches) == 1

    def test_duplicate_task_names(self, make_eval):
        eval_dir = make_eval(
            tasks={
                "a": {"prompt": "hi"},
                "b": {"name": "a", "prompt": "yo"},
            }
        )

        problems = validate_eval(eval_dir)
        matches = [p for p in problems if p["file"] == "tasks/b.yaml"]
        assert len(matches) == 1
        assert matches[0]["path"] == "name"
        assert "duplicate task name 'a'" in matches[0]["problem"]
        assert "tasks/a.yaml" in matches[0]["problem"]

    def test_duplicate_grader_names(self, make_eval):
        eval_dir = make_eval(
            graders={
                "default": {"checks": [{"checker": "contains", "value": "x"}]},
                "other": {
                    "name": "default",
                    "checks": [{"checker": "contains", "value": "y"}],
                },
            }
        )

        problems = validate_eval(eval_dir)
        matches = [p for p in problems if p["file"] == "graders/other.yaml"]
        assert len(matches) == 1
        assert "duplicate grader name 'default'" in matches[0]["problem"]


# --- FILE_SCHEMAS ------------------------------------------------------


README_DOCUMENTED_KEYS = {
    "eval": {"name", "description"},
    "task": {"name", "prompt"},
    "config": {"name", "runner", "model"},
    "grader": {"name", "checks", "scoring.pass_threshold"},
}

VALID_FIELD_KINDS = {"str", "text", "number", "path", "checks"}


class TestFileSchemas:
    def test_covers_the_four_file_kinds(self):
        assert set(FILE_SCHEMAS) == {"task", "config", "grader", "eval"}

    def test_fields_have_the_documented_shape(self):
        for kind, fields in FILE_SCHEMAS.items():
            for field in fields:
                assert set(field) == {"key", "label", "kind", "required", "help"}
                assert field["kind"] in VALID_FIELD_KINDS
                assert isinstance(field["required"], bool)
                assert field["label"]
                assert field["help"]

    def test_covers_readme_documented_keys(self):
        for kind, expected_keys in README_DOCUMENTED_KEYS.items():
            actual_keys = {field["key"] for field in FILE_SCHEMAS[kind]}
            assert actual_keys == expected_keys, kind

    def test_name_is_required_everywhere(self):
        for kind, fields in FILE_SCHEMAS.items():
            by_key = {field["key"]: field for field in fields}
            assert by_key["name"]["required"] is True
