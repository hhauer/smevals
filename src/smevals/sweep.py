"""Sweep orchestration: an evals x models x n matrix run as one studio job.

An in-process port of the shell sweeps (eval-sweep.sh and
sweep-code-authoring.sh), keeping their proven semantics exactly:
model-major execution order (unload all -> `lms load <model> -c 32768`
for local models -> every selected eval for that model -> next model),
top-up via `smevals run -n`'s shortfall logic (compute_remaining), inline
grading of each successful Run, and judge-style graders deferred to one
final pass after the last model so their judge model loads once. Local
vs hosted model detection: present in the LM Studio inventory -> local;
else hosted (no lms calls). Failed Runs are recorded and excluded and a
failing pair never halts the sweep - the CLI's failure policy.

run_sweep executes in a daemon thread the studio starts, mutating its
job dict in place under the studio's jobs lock; the per-eval active_jobs
slot is held only around each individual run (hooks.acquire/release), so
bench actions on other evals - and on this eval, between runs - stay
possible. Cancellation (hooks.cancel, a threading.Event) is honored
between runs and between deferred grader passes, never mid-run.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from .cli import (
    compute_remaining,
    count_existing_runs,
    execute_run,
    grade_pending,
    grade_run,
    load_yaml,
)
from .site import collect_eval, group_stats

# Local models are loaded explicitly with a generous context length: LM
# Studio JIT loads default to an 8192-token budget, which thinking-heavy
# models exhaust on reasoning before emitting anything (finish_reason:
# length, empty output) - the lesson the shell sweeps learned.
DEFAULT_CONTEXT_LENGTH = 32768

# The sweep always runs the config named "default", as the shell sweeps do
SWEEP_CONFIG = "default"

TREATMENTS = ("inline", "deferred", "skip")

# Cell keys join slug and model with NUL - the one byte neither may contain
CELL_SEP = "\x00"

# How many log lines a sweep job retains - a tail, per the job shape
LOG_TAIL = 200

# The lock-discipline messages the spec states verbatim (and the UI shows)
SWEEP_ACTIVE_MESSAGE = "a sweep is already running"
SWEEP_HOLDS_EVAL_MESSAGE = (
    "a sweep is running this eval right now — it frees up between runs, "
    "try again in a moment"
)


def cell_key(slug, model):
    return f"{slug}{CELL_SEP}{model}"


# --- lms integration ------------------------------------------------------


def lms_path():
    "`lms` on PATH, else ~/.lmstudio/bin/lms, else None"
    found = shutil.which("lms")
    if found:
        return Path(found)
    fallback = Path.home() / ".lmstudio" / "bin" / "lms"
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return fallback
    return None


def parse_lms_ls(text):
    """Best-effort, section-aware scrape of `lms ls`'s human-oriented
    column output, verified against a verbatim capture of the real tool
    (tests/real-lms-ls.txt).

    The listing is a summary line ("You have 9 models, ...") followed by
    per-section tables whose header row's first column names the section:
    LLM, then EMBEDDING. Only LLM-section rows are loadable chat models,
    so rows are consumed strictly between the LLM header and the next
    blank line or section header. A row's first column (columns separated
    by 2+ spaces) is the model id, optionally suffixed " (N variant[s])",
    which is stripped. Anything unexpected is skipped - a garbled listing
    just means a smaller inventory, never a hard failure.
    """
    models = set()
    in_llm_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_llm_section = False
            continue
        columns = [c for c in re.split(r"\s{2,}", stripped) if c]
        if columns[0] == "LLM":
            in_llm_section = True
            continue
        if columns[0] == "EMBEDDING":
            in_llm_section = False
            continue
        if not in_llm_section or len(columns) < 2:
            continue
        model = re.sub(r"\s+\(\d+ variants?\)$", "", columns[0])
        if model and " " not in model:
            models.add(model)
    return models


def lms_inventory():
    "Model ids `lms ls` reports, empty on any failure (within 5s)"
    lms = lms_path()
    if lms is None:
        return set()
    try:
        result = subprocess.run(
            [str(lms), "ls"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return parse_lms_ls(result.stdout)


def lms_unload_all():
    "Best-effort `lms unload --all` - the shell sweeps ignore its failures too"
    lms = lms_path()
    if lms is None:
        return
    try:
        subprocess.run(
            [str(lms), "unload", "--all"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def lms_load(model, context_length=DEFAULT_CONTEXT_LENGTH):
    "(ok, message) from `lms load <model> -c <context_length> -y`"
    lms = lms_path()
    if lms is None:
        return False, "lms executable not found"
    try:
        result = subprocess.run(
            [str(lms), "load", model, "-c", str(context_length), "-y"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except OSError as ex:
        return False, str(ex)
    except subprocess.TimeoutExpired:
        return False, f"lms load {model} timed out"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        message = "\n".join(detail.splitlines()[-5:]) if detail else ""
        return False, message or f"lms load exited {result.returncode}"
    return True, "loaded"


# --- sweep planning -------------------------------------------------------


def grader_treatment(grader_doc):
    """The default treatment for one grader spec: "deferred" when any of
    its checks carries a `model` key (the judge pattern in the bundled
    examples - grading needs a model of its own, so it runs once at the
    end instead of swapping models after every run), else "inline"."""
    checks = grader_doc.get("checks") if isinstance(grader_doc, dict) else None
    for check in checks or []:
        if isinstance(check, dict) and "model" in check:
            return "deferred"
    return "inline"


def default_treatments(eval_path):
    """grader name -> default treatment for every grader in one Eval.

    A grader whose YAML doesn't parse defaults to "inline" - grading with
    it will fail loudly during the sweep, which tolerates failures, and
    hiding it here would silently drop it from the compose form.
    """
    treatments = {}
    for path in sorted((eval_path / "graders").glob("*.yaml")):
        try:
            doc = load_yaml(path)
        except yaml.YAMLError:
            doc = None
        treatments[path.stem] = grader_treatment(doc or {})
    return treatments


def validate_spec(body, evals, require_models=True):
    """Normalize a SweepSpec request body against the discovered evals,
    raising ValueError on anything invalid. Returns the full spec:
    {evals, models, n, graders: {slug: {name: treatment}}, context_length}
    with grader treatments defaulted per default_treatments and any
    caller overrides applied. The plan endpoint passes
    require_models=False so a compose form can preview treatments before
    any model is picked.
    """
    if not isinstance(body, dict):
        raise ValueError("spec must be a JSON object")

    slugs = body.get("evals")
    if slugs is None:
        slugs = sorted(evals)
    if (
        not isinstance(slugs, list)
        or not slugs
        or not all(isinstance(s, str) for s in slugs)
    ):
        raise ValueError("evals must be a non-empty list of eval slugs")
    if len(set(slugs)) != len(slugs):
        raise ValueError("evals contains duplicates")
    unknown = [s for s in slugs if s not in evals]
    if unknown:
        raise ValueError(f"no such eval(s): {', '.join(unknown)}")

    models = body.get("models")
    if models is None and not require_models:
        models = []
    if (
        not isinstance(models, list)
        or (require_models and not models)
        or not all(isinstance(m, str) and m.strip() for m in models)
    ):
        raise ValueError("models must be a non-empty list of model names")
    if len(set(models)) != len(models):
        raise ValueError("models contains duplicates")

    n = body.get("n", 1)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError("n must be an integer >= 1")

    context_length = body.get("context_length", DEFAULT_CONTEXT_LENGTH)
    if (
        not isinstance(context_length, int)
        or isinstance(context_length, bool)
        or context_length < 1
    ):
        raise ValueError("context_length must be a positive integer")

    overrides = body.get("graders") or {}
    if not isinstance(overrides, dict):
        raise ValueError("graders must be an object of {eval: {grader: treatment}}")
    unknown = [s for s in overrides if s not in slugs]
    if unknown:
        raise ValueError(f"graders names no such eval(s): {', '.join(unknown)}")
    graders = {}
    for slug in slugs:
        treatments = default_treatments(evals[slug])
        eval_overrides = overrides.get(slug) or {}
        if not isinstance(eval_overrides, dict):
            raise ValueError(f"graders[{slug!r}] must be an object")
        for name, treatment in eval_overrides.items():
            if name not in treatments:
                raise ValueError(f"{slug} has no grader named {name!r}")
            if treatment not in TREATMENTS:
                raise ValueError(
                    "grader treatment must be one of " + ", ".join(TREATMENTS)
                )
            treatments[name] = treatment
        graders[slug] = treatments

    return {
        "evals": slugs,
        "models": models,
        "n": n,
        "graders": graders,
        "context_length": context_length,
    }


def eval_tasks(eval_path):
    return [load_yaml(p) for p in sorted((eval_path / "tasks").glob("*.yaml"))]


def cell_plan(eval_path, task_docs, model, n):
    "target/have/remaining for one eval x model cell under the default config"
    runs_root = eval_path / "runs"
    remaining = compute_remaining(runs_root, task_docs, [model], SWEEP_CONFIG, n)
    have = sum(
        count_existing_runs(runs_root, task["name"], SWEEP_CONFIG, model)
        for task in task_docs
    )
    return {
        "target": n * len(task_docs),
        "have": have,
        "remaining": sum(remaining.values()),
    }


def plan_preview(spec, evals):
    "The /api/sweep/plan document: per-cell shortfall + grader treatments"
    cells = {}
    for slug in spec["evals"]:
        task_docs = eval_tasks(evals[slug])
        for model in spec["models"]:
            cells[cell_key(slug, model)] = cell_plan(
                evals[slug], task_docs, model, spec["n"]
            )
    return {
        "evals": spec["evals"],
        "models": spec["models"],
        "n": spec["n"],
        "cells": cells,
        "graders": spec["graders"],
    }


def cell_mean(eval_path, model):
    """The current mean score for (default config, model) under the
    Eval's default grader - the same group_stats counting every results
    surface uses, so the board and the Results tab always agree."""
    data = collect_eval(eval_path)
    grader_name = data["eval"]["default_grader"]
    if grader_name is None:
        return None
    for group in group_stats(data["rows"], grader_name):
        if group["config"] == SWEEP_CONFIG and group["model"] == model:
            return group["mean"]
    return None


def build_job(job_id, spec, evals, started):
    """A sweep job dict with every key pre-created, so concurrent readers
    (the polling endpoints serialize it under the jobs lock) always see
    the full stable shape."""
    cells = {}
    deferred = []
    for slug in spec["evals"]:
        task_docs = eval_tasks(evals[slug])
        for model in spec["models"]:
            plan = cell_plan(evals[slug], task_docs, model, spec["n"])
            cells[cell_key(slug, model)] = {
                "target": plan["target"],
                "have": plan["have"],
                "failed": 0,
                "done": 0,
                "mean": cell_mean(evals[slug], model),
                "state": "queued",
            }
        for name, treatment in spec["graders"][slug].items():
            if treatment == "deferred":
                deferred.append({"eval": slug, "grader": name, "state": "queued"})
    return {
        "id": job_id,
        "kind": "sweep",
        "status": "queued",
        "started": started,
        "spec": spec,
        "cells": cells,
        "current": None,
        "deferred": deferred,
        "log": [],
        "error": None,
        "cancel_requested": False,
    }


# --- the orchestrator -----------------------------------------------------


def _line_echo(log):
    "Adapt a line-oriented log function to click.echo's (message, nl=) contract"
    buffer = []

    def echo(message="", nl=True):
        buffer.append(str(message))
        if nl:
            log("".join(buffer))
            buffer.clear()

    return echo


def run_sweep(job, spec, evals, hooks):
    """Execute a sweep job in the calling (background) thread.

    evals: slug -> Eval path for at least every slug in spec. hooks
    carries the studio's concurrency plumbing: `lock` (the jobs lock -
    every job mutation happens under it), `cancel` (a threading.Event),
    `acquire(slug) -> bool` (block - polling politely - until the eval's
    active_jobs slot is claimed for this sweep; False if cancelled while
    waiting) and `release(slug)`.
    """
    lock = hooks.lock

    def log(line):
        with lock:
            job["log"].append(line)
            del job["log"][:-LOG_TAIL]

    try:
        with lock:
            job["status"] = "running"
        inventory = lms_inventory()
        local_models = [m for m in spec["models"] if m in inventory]

        cancelled = False
        for model in spec["models"]:
            if hooks.cancel.is_set():
                cancelled = True
                break
            log(f"=== model: {model} ===")
            if model in inventory:
                with lock:
                    for slug in spec["evals"]:
                        job["cells"][cell_key(slug, model)]["state"] = "loading"
                lms_unload_all()
                ok, message = lms_load(model, spec["context_length"])
                if not ok:
                    # The whole column is unrunnable; the sweep moves on
                    log(f"lms load {model} failed: {message}")
                    with lock:
                        for slug in spec["evals"]:
                            job["cells"][cell_key(slug, model)]["state"] = "failed"
                    continue
                with lock:
                    for slug in spec["evals"]:
                        job["cells"][cell_key(slug, model)]["state"] = "queued"
            for slug in spec["evals"]:
                if _sweep_cell(job, spec, slug, evals[slug], model, hooks, log):
                    cancelled = True
                    break
            if cancelled:
                break

        if not cancelled:
            # One final unload before deferred grading, so a judge model
            # has the machine to itself - pointless (and, per the hosted-
            # models-skip-lms rule, forbidden) when nothing was loaded
            if local_models:
                lms_unload_all()
            cancelled = _deferred_pass(job, spec, evals, hooks, log)

        with lock:
            job["current"] = None
            job["status"] = "cancelled" if cancelled else "done"
    except Exception as ex:  # a bug, not a failing pair - fail the job loudly
        with lock:
            job["current"] = None
            job["status"] = "failed"
            job["error"] = str(ex)


def _sweep_cell(job, spec, slug, eval_path, model, hooks, log):
    """Top up one eval x model cell, one slot-guarded run at a time,
    grading inline. Returns True if the sweep was cancelled mid-cell."""
    lock = hooks.lock
    cell = job["cells"][cell_key(slug, model)]

    def fail_cell(message):
        log(f"{slug} / {model}: {message}")
        with lock:
            cell["state"] = "failed"
        return False

    config_path = eval_path / "configs" / f"{SWEEP_CONFIG}.yaml"
    if not config_path.is_file():
        return fail_cell(f"no config named {SWEEP_CONFIG!r}")
    try:
        config = load_yaml(config_path)
        runner = (config_path.parent / config["runner"]).resolve()
    except Exception as ex:
        return fail_cell(f"invalid config: {ex}")
    if not (runner.is_file() and os.access(runner, os.X_OK)):
        return fail_cell(f"runner {runner} is not an executable file")
    try:
        task_docs = eval_tasks(eval_path)
    except Exception as ex:
        return fail_cell(f"invalid task: {ex}")
    if not task_docs:
        return fail_cell("no tasks")

    inline_graders = []
    for name, treatment in spec["graders"][slug].items():
        if treatment != "inline":
            continue
        grader_path = eval_path / "graders" / f"{name}.yaml"
        try:
            inline_graders.append((name, grader_path, load_yaml(grader_path)))
        except Exception as ex:
            log(f"{slug}: skipping unreadable grader {name!r}: {ex}")

    runs_root = eval_path / "runs"
    # Shortfall computed fresh at execution time - runs recorded by an
    # earlier (interrupted) sweep count, which is what makes sweeps
    # resumable. Attempted once per pair per sweep, mirroring `run`.
    remaining = compute_remaining(
        runs_root, task_docs, [model], SWEEP_CONFIG, spec["n"]
    )
    while any(remaining.values()):
        # Full passes over the tasks, so cancelling leaves balanced samples
        for task in task_docs:
            pair = (task["name"], model)
            if not remaining[pair]:
                continue
            if not hooks.acquire(slug):
                return True
            try:
                remaining[pair] -= 1
                with lock:
                    cell["state"] = "running"
                    job["current"] = {
                        "eval": slug,
                        "model": model,
                        "task": task["name"],
                    }
                ok = False
                try:
                    ok, run_dir = execute_run(
                        runs_root,
                        task,
                        SWEEP_CONFIG,
                        runner,
                        model,
                        echo=_line_echo(log),
                    )
                except Exception as ex:
                    log(f"{slug} / {model} / {task['name']}: {ex}")
                if ok and inline_graders:
                    with lock:
                        cell["state"] = "grading"
                    for name, grader_path, grader in inline_graders:
                        try:
                            record = grade_run(
                                run_dir, run_dir / "grades" / name, grader, grader_path
                            )
                            log(f"    grade[{name}]: {record['outcome']}")
                        except Exception as ex:
                            log(f"    grade[{name}] failed: {ex}")
                # Recount from disk - the same rules every surface uses
                have = sum(
                    count_existing_runs(runs_root, t["name"], SWEEP_CONFIG, model)
                    for t in task_docs
                )
                mean = cell_mean(eval_path, model)
                with lock:
                    cell["done" if ok else "failed"] += 1
                    cell["have"] = have
                    cell["mean"] = mean
                    job["current"] = None
            finally:
                hooks.release(slug)
            if hooks.cancel.is_set():
                return True

    with lock:
        cell["state"] = "failed" if cell["failed"] else "done"
    return False


def _deferred_pass(job, spec, evals, hooks, log):
    """grade_pending for every deferred grader, one eval-grader pass at a
    time. Returns True if cancelled between passes. A pass whose grades
    include failures is marked failed - the shell sweeps' `|| FAILURES`
    accounting - without halting the remaining passes."""
    lock = hooks.lock
    for entry in job["deferred"]:
        if hooks.cancel.is_set():
            return True
        slug, name = entry["eval"], entry["grader"]
        eval_path = evals[slug]
        with lock:
            entry["state"] = "running"
        log(f"=== deferred grading: {slug} / {name} ===")
        grader_path = eval_path / "graders" / f"{name}.yaml"
        try:
            grader = load_yaml(grader_path)
            result = grade_pending(
                eval_path / "runs", name, grader, grader_path, _line_echo(log)
            )
            state = "failed" if result["failures"] else "done"
        except Exception as ex:
            log(f"deferred grading {slug} / {name} failed: {ex}")
            state = "failed"
        with lock:
            entry["state"] = state
        # New grades may have landed - refresh the board's means
        for model in spec["models"]:
            mean = cell_mean(eval_path, model)
            with lock:
                job["cells"][cell_key(slug, model)]["mean"] = mean
    return False
