"""Pure Eval scaffolding and validation - no HTTP, no subprocesses.

`scaffold_eval` creates the canonical layout the README tutorial walks
through; `validate_eval` re-parses an Eval's files and reports the same
problems `smevals run`/`grade` would hit, so Studio (and tests) can call
these directly without a live server. `FILE_SCHEMAS` describes the known
fields of each YAML kind, driving Studio's form view - arbitrary extra
keys are always permitted alongside them.
"""

import os
import re
from importlib.resources import files

import yaml

from .cli import BUILTIN_CHECKERS

EXAMPLE_TASK_PROMPT = "TODO: write a prompt for the model to respond to."


def scaffold_slug(name):
    "Directory-safe slug for a new Eval: lowercase, spaces to dashes, else stripped"
    slug = name.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", slug)


def starter_run_llm():
    "The guarded llm-CLI Runner shipped as a starting point for new Evals"
    return (files("smevals") / "starter-run-llm").read_text()


def write_yaml(path, doc):
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def scaffold_eval(parent, name, description):
    "Create the canonical Eval layout from the README tutorial, return its directory"
    if not name or not name.strip():
        raise ValueError("name is required")
    slug = scaffold_slug(name)
    if not slug:
        raise ValueError(f"name {name!r} has no characters usable in a directory slug")
    eval_dir = parent / slug
    if eval_dir.exists():
        raise ValueError(f"{eval_dir} already exists")

    (eval_dir / "tasks").mkdir(parents=True)
    (eval_dir / "configs").mkdir()
    (eval_dir / "graders").mkdir()

    write_yaml(eval_dir / "eval.yaml", {"name": name, "description": description})
    write_yaml(
        eval_dir / "tasks" / "example.yaml",
        {"name": "example", "prompt": EXAMPLE_TASK_PROMPT},
    )
    write_yaml(
        eval_dir / "configs" / "default.yaml",
        {"name": "default", "runner": "../run-llm", "model": "gpt-4.1-mini"},
    )
    write_yaml(
        eval_dir / "graders" / "default.yaml",
        {
            "name": "default",
            "checks": [{"checker": "contains", "value": "", "required": True}],
            "scoring": {"pass_threshold": 1.0},
        },
    )
    runner = eval_dir / "run-llm"
    runner.write_text(starter_run_llm())
    runner.chmod(0o755)
    (eval_dir / ".gitignore").write_text("runs\n")
    return eval_dir


# --- validate_eval -------------------------------------------------------

KIND_DIRS = {"task": "tasks", "config": "configs", "grader": "graders"}


def rel_path(path, eval_dir):
    return str(path.relative_to(eval_dir))


def problem(path, eval_dir, key_path, message):
    return {"file": rel_path(path, eval_dir), "path": key_path, "problem": message}


def parse_yaml(path):
    "Parse a YAML file, returning (doc, error). doc is always a dict; error is None on success"
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as ex:
        return {}, str(ex)
    return doc if isinstance(doc, dict) else {}, None


def missing_key_problem(doc, path, eval_dir, key):
    if not doc.get(key):
        return [problem(path, eval_dir, key, f"missing required key: {key}")]
    return []


def validate_eval_doc(path, eval_dir, doc):
    return missing_key_problem(doc, path, eval_dir, "name")


def validate_task_doc(path, eval_dir, doc):
    problems = missing_key_problem(doc, path, eval_dir, "name")
    if not doc.get("prompt"):
        # Not every Task is a single prompt (some carry other data instead),
        # so a missing prompt is a nudge, not an error
        problems.append(
            problem(
                path,
                eval_dir,
                "prompt",
                "warning: no prompt - the Runner will not receive SMEVALS_PROMPT",
            )
        )
    return problems


def validate_config_doc(path, eval_dir, doc):
    problems = missing_key_problem(doc, path, eval_dir, "name")
    problems += missing_key_problem(doc, path, eval_dir, "runner")
    runner = doc.get("runner")
    if runner:
        resolved = (path.parent / runner).resolve()
        if not resolved.is_file():
            problems.append(
                problem(
                    path,
                    eval_dir,
                    "runner",
                    f"runner does not resolve to a file: {runner}",
                )
            )
        elif not os.access(resolved, os.X_OK):
            problems.append(
                problem(path, eval_dir, "runner", f"runner is not executable: {runner}")
            )
    return problems


def validate_check(path, eval_dir, index, check):
    key_path = f"checks.{index}.checker"
    checker = check.get("checker") if isinstance(check, dict) else None
    if not checker:
        return [problem(path, eval_dir, key_path, "missing required key: checker")]
    if checker in BUILTIN_CHECKERS:
        return []
    resolved = (path.parent / checker).resolve()
    if not resolved.is_file():
        return [
            problem(
                path,
                eval_dir,
                key_path,
                f"checker is not a built-in and does not resolve to a file: {checker}",
            )
        ]
    if not os.access(resolved, os.X_OK):
        return [
            problem(path, eval_dir, key_path, f"checker is not executable: {checker}")
        ]
    return []


def validate_grader_doc(path, eval_dir, doc):
    problems = missing_key_problem(doc, path, eval_dir, "name")
    checks = doc.get("checks")
    if not isinstance(checks, list) or not checks:
        problems.append(
            problem(path, eval_dir, "checks", "missing required key: checks")
        )
    else:
        for index, check in enumerate(checks):
            problems += validate_check(path, eval_dir, index, check)

    scoring = doc.get("scoring")
    if isinstance(scoring, dict) and "pass_threshold" in scoring:
        threshold = scoring["pass_threshold"]
        if type(threshold) not in (int, float) or not (0 <= threshold <= 1):
            problems.append(
                problem(
                    path,
                    eval_dir,
                    "scoring.pass_threshold",
                    f"pass_threshold must be a number between 0 and 1, got {threshold!r}",
                )
            )
    return problems


KIND_VALIDATORS = {
    "task": validate_task_doc,
    "config": validate_config_doc,
    "grader": validate_grader_doc,
}


def duplicate_name_problems(eval_dir, kind, named):
    "One problem per file after the first to claim a given name"
    problems = []
    first_seen = {}
    for path, name in named:
        if name in first_seen:
            problems.append(
                problem(
                    path,
                    eval_dir,
                    "name",
                    f"duplicate {kind} name {name!r} - also used by "
                    f"{rel_path(first_seen[name], eval_dir)}",
                )
            )
        else:
            first_seen[name] = path
    return problems


def validate_kind(eval_dir, kind):
    problems = []
    named = []
    for path in sorted((eval_dir / KIND_DIRS[kind]).glob("*.yaml")):
        doc, error = parse_yaml(path)
        if error:
            problems.append(problem(path, eval_dir, "", f"invalid YAML: {error}"))
            continue
        problems += KIND_VALIDATORS[kind](path, eval_dir, doc)
        if doc.get("name"):
            named.append((path, doc["name"]))
    problems += duplicate_name_problems(eval_dir, kind, named)
    return problems


def validate_eval(eval_dir):
    "Re-parse an Eval's files, returning the problems smevals run/grade would hit"
    problems = []
    eval_file = eval_dir / "eval.yaml"
    if not eval_file.exists():
        problems.append(problem(eval_file, eval_dir, "", "eval.yaml is missing"))
    else:
        doc, error = parse_yaml(eval_file)
        if error:
            problems.append(problem(eval_file, eval_dir, "", f"invalid YAML: {error}"))
        else:
            problems += validate_eval_doc(eval_file, eval_dir, doc)
    for kind in KIND_DIRS:
        problems += validate_kind(eval_dir, kind)
    return problems


# --- FILE_SCHEMAS ----------------------------------------------------------

FILE_SCHEMAS = {
    "eval": [
        {
            "key": "name",
            "label": "Name",
            "kind": "str",
            "required": True,
            "help": "The Eval's name, used to slug its directory and identify it "
            "across the CLI and Studio.",
        },
        {
            "key": "description",
            "label": "Description",
            "kind": "text",
            "required": False,
            "help": "What this Eval measures - shown on the shelf and in reports.",
        },
    ],
    "task": [
        {
            "key": "name",
            "label": "Name",
            "kind": "str",
            "required": True,
            "help": "Unique among this Eval's Tasks; used in run paths and reports.",
        },
        {
            "key": "prompt",
            "label": "Prompt",
            "kind": "text",
            "required": False,
            "help": "Sent to the Runner as SMEVALS_PROMPT. Other scalar keys become "
            "SMEVALS_TASK_<KEY> env vars.",
        },
    ],
    "config": [
        {
            "key": "name",
            "label": "Name",
            "kind": "str",
            "required": True,
            "help": "The Config's name; 'default' is used when -c is omitted.",
        },
        {
            "key": "runner",
            "label": "Runner",
            "kind": "path",
            "required": True,
            "help": "Path to an executable Runner, relative to this file.",
        },
        {
            "key": "model",
            "label": "Model",
            "kind": "str",
            "required": False,
            "help": "Model to use when -m is not given on the command line.",
        },
    ],
    "grader": [
        {
            "key": "name",
            "label": "Name",
            "kind": "str",
            "required": True,
            "help": "The Grader's name; Grades are recorded under grades/<name>/.",
        },
        {
            "key": "checks",
            "label": "Checks",
            "kind": "checks",
            "required": True,
            "help": "The pipeline of Checks applied to a Run, in order.",
        },
        {
            "key": "scoring.pass_threshold",
            "label": "Pass threshold",
            "kind": "number",
            "required": False,
            "help": "A Grade passes when its score meets this threshold (0-1).",
        },
    ],
}
