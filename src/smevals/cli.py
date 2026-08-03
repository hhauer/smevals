import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml


@click.group()
@click.version_option()
def cli():
    """Run evals against LLMs and agent harnesses.

    An Eval is a directory with an eval.yaml describing what is being
    evaluated, tasks/ for the exercises, configs/ for how to attempt
    them and graders/ for how to score the results. Runs and their
    Grades are recorded as plain files in runs/.

    A typical workflow:

    \b
      smevals run examples/my-eval -m gpt-4.1-mini -g
      smevals report examples/my-eval
      smevals serve examples/
    """


@cli.command()
def docs():
    "Output the smevals documentation"
    from importlib.metadata import metadata

    meta = metadata("smevals")
    click.echo(meta.get_payload() or meta.get("Description", ""))


def slugify(text):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")


def load_yaml(path):
    return yaml.safe_load(path.read_text())


def load_eval(eval_path):
    eval_file = eval_path / "eval.yaml"
    if not eval_file.exists():
        raise click.ClickException(f"{eval_path} is not an Eval - no eval.yaml found")
    return load_yaml(eval_file)


def load_grader(eval_path, grader_name):
    grader_path = eval_path / "graders" / f"{grader_name}.yaml"
    if not grader_path.exists():
        available = sorted(p.stem for p in (eval_path / "graders").glob("*.yaml"))
        raise click.ClickException(
            f"No grader named {grader_name!r} - available graders: "
            + (", ".join(available) or "(none)")
        )
    return grader_path, load_yaml(grader_path)


def resolve_runs_root(eval_path, eval_doc, runs_dir):
    if runs_dir is None:
        return eval_path / "runs"
    # An external runs dir may hold runs from many evals
    return runs_dir / slugify(eval_doc.get("name") or eval_path.name)


runs_dir_option = click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Runs directory, if kept outside the eval (namespaced by eval name)",
)


def scalar_env_vars(prefix, mapping):
    "Scalar mapping values as env vars, e.g. submission -> SMEVALS_TASK_SUBMISSION"
    return {
        prefix + re.sub(r"[^A-Za-z0-9]+", "_", key).upper(): str(value)
        for key, value in mapping.items()
        if isinstance(value, (str, int, float, bool))
    }


def normalize_tag(tag):
    return re.sub(r"[^a-z0-9]+", "_", str(tag).lower()).strip("_")


def normalize_check_info(info):
    """Apply the check result contract to checker-emitted fields.

    Checkers own five keys: score (float 0-1), metrics (dict of
    name -> number|bool), tags (list of slugs), notes (str), details
    (dict). Anything else is folded into details rather than trusted
    at the top level, so core-owned keys can never be clobbered.
    """
    out = {}
    if info.get("score") is not None:
        out["score"] = float(info["score"])
    if isinstance(info.get("metrics"), dict):
        out["metrics"] = info["metrics"]
    if isinstance(info.get("tags"), list):
        out["tags"] = sorted({normalize_tag(t) for t in info["tags"] if str(t).strip()})
    if info.get("notes"):
        out["notes"] = str(info["notes"])
    details = info.get("details") if isinstance(info.get("details"), dict) else {}
    extras = {
        key: value
        for key, value in info.items()
        if key not in ("score", "metrics", "tags", "notes", "details")
    }
    if details or extras:
        out["details"] = details | extras
    return out


@cli.command()
@click.argument(
    "eval_path", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "models",
    "-m",
    "--model",
    multiple=True,
    help="Model(s) to run against, defaults to the model in the config",
)
@click.option(
    "-c",
    "--config",
    "config_name",
    default="default",
    show_default=True,
    help="Name of the config to use",
)
@click.option(
    "tasks",
    "-t",
    "--task",
    multiple=True,
    help="Task(s) to run, defaults to every task in the eval",
)
@click.option(
    "-n",
    "--repeat",
    "repeat",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Ensure at least N Runs are recorded per task/model pair, "
    "executing only the shortfall - re-running the same command is a "
    "no-op once every pair has N Runs. Without -n, exactly one new "
    "Run is executed per pair.",
)
@click.option(
    "-g",
    "--grader",
    "grader_name",
    is_flag=False,
    flag_value="default",
    default=None,
    help="Grade each Run the moment it finishes; -g alone uses the "
    "grader named 'default'",
)
@runs_dir_option
def run(eval_path, models, config_name, tasks, repeat, grader_name, runs_dir):
    "Execute the Tasks in an Eval, recording each Run"
    eval_doc = load_eval(eval_path)
    runs_root = resolve_runs_root(eval_path, eval_doc, runs_dir)

    # Validate the grader before any model gets called
    grader_path = grader = None
    if grader_name:
        grader_path, grader = load_grader(eval_path, grader_name)

    config_path = eval_path / "configs" / f"{config_name}.yaml"
    if not config_path.exists():
        available = sorted(p.stem for p in (eval_path / "configs").glob("*.yaml"))
        raise click.ClickException(
            f"No config named {config_name!r} - available configs: "
            + (", ".join(available) or "(none)")
        )
    config = load_yaml(config_path)

    runner = (config_path.parent / config["runner"]).resolve()
    if not (runner.is_file() and os.access(runner, os.X_OK)):
        raise click.ClickException(f"Runner {runner} is not an executable file")

    task_files = sorted((eval_path / "tasks").glob("*.yaml"))
    if tasks:
        available = {p.stem: p for p in task_files}
        missing = [t for t in tasks if t not in available]
        if missing:
            raise click.ClickException(
                f"No such task(s): {', '.join(missing)} - available tasks: "
                + ", ".join(sorted(available))
            )
        task_files = [available[t] for t in tasks]
    if not task_files:
        raise click.ClickException(f"No tasks found in {eval_path / 'tasks'}")

    models = list(models) or [config["model"]]
    task_docs = [load_yaml(task_file) for task_file in task_files]

    # New Runs each task/model pair still needs: exactly one without -n,
    # otherwise the shortfall against the target sample size
    remaining = {}
    for task in task_docs:
        for model in models:
            if repeat is None:
                remaining[(task["name"], model)] = 1
                continue
            existing = count_existing_runs(runs_root, task["name"], config_name, model)
            remaining[(task["name"], model)] = max(0, repeat - existing)
            if existing >= repeat:
                click.echo(
                    f"{task['name']} / {config_name} / {model}: "
                    f"already have {existing} run(s)"
                )

    failures = grade_failures = 0
    # Repeat index outermost: full passes over every pair, so an
    # interrupted session leaves balanced samples across tasks and models
    while any(remaining.values()):
        for task in task_docs:
            for model in models:
                if not remaining[(task["name"], model)]:
                    continue
                remaining[(task["name"], model)] -= 1
                ok, run_dir = execute_run(runs_root, task, config_name, runner, model)
                failures += not ok
                if grader:
                    # A failed Run is a harness error, not evidence - there
                    # is nothing meaningful to grade
                    if not ok:
                        click.echo("    grade: skipped (run failed)")
                        continue
                    grade_dir = run_dir / "grades" / grader_name
                    record = grade_run(run_dir, grade_dir, grader, grader_path)
                    grade_failures += record["outcome"] != "pass"
                    score = record["score"]
                    score_display = "" if score is None else f" score={score}"
                    click.echo(f"    grade: {record['outcome']}{score_display}")
    problems = []
    if failures:
        problems.append(f"{failures} run(s) failed")
    if grade_failures:
        problems.append(f"{grade_failures} run(s) graded as fail")
    if problems:
        raise click.ClickException(", ".join(problems))


def run_failed(run):
    "A failed Run is one whose Runner exited non-zero: a harness error"
    # Missing exit_code (e.g. imported runs) is treated as success
    return (run.get("exit_code") or 0) != 0


def count_existing_runs(runs_root, task_name, config_name, model):
    "Successful Runs already recorded for one task/config/model combination"
    parent = runs_root / task_name / config_name / slugify(model)
    return sum(
        1 for p in parent.glob("*/run.yaml") if not run_failed(load_yaml(p))
    )


def execute_run(runs_root, task, config_name, runner, model, echo=click.echo):
    """Execute a single Run and record it, returning (ok, run_dir)

    echo defaults to click.echo for the CLI; Studio passes a quiet or
    log-collecting callable instead so this stays usable in-process.
    """
    started = datetime.now(timezone.utc)
    timestamp = started.strftime("%Y-%m-%dT%H-%M-%SZ")
    parent = runs_root / task["name"] / config_name / slugify(model)
    run_dir = parent / timestamp
    # Repeat runs in the same second get a numeric suffix
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = parent / f"{timestamp}-{suffix}"
    run_dir.mkdir(parents=True)

    echo(f"{task['name']} / {config_name} / {model} ... ", nl=False)
    env = (
        os.environ
        | scalar_env_vars("SMEVALS_TASK_", task)
        | {
            "SMEVALS_MODEL": model,
            "SMEVALS_TASK": task["name"],
            "SMEVALS_RUN_DIR": str(run_dir.resolve()),
        }
    )
    # Not every Task is a single prompt - some carry other data instead
    if "prompt" in task:
        env["SMEVALS_PROMPT"] = task["prompt"]
    t0 = time.monotonic()
    result = subprocess.run(
        [str(runner)], cwd=run_dir, env=env, capture_output=True, text=True
    )
    duration = time.monotonic() - t0

    (run_dir / "output.txt").write_text(result.stdout)
    if result.stderr:
        (run_dir / "stderr.txt").write_text(result.stderr)
    # run.yaml is written last: its presence marks a complete Run.
    # The full task is embedded so the Run stays self-describing.
    record = {
        "task": task,
        "config": {
            "name": config_name,
            "runner": str(runner),
            "model": model,
        },
        "started": started.isoformat(),
        "duration_seconds": round(duration, 2),
        "exit_code": result.returncode,
    }
    (run_dir / "run.yaml").write_text(yaml.safe_dump(record, sort_keys=False))

    ok = result.returncode == 0
    status = "ok" if ok else f"FAILED (exit {result.returncode})"
    relative = os.path.relpath(run_dir)
    display = relative if len(relative) < len(str(run_dir)) else str(run_dir)
    echo(f"{status} ({duration:.1f}s) -> {display}")
    return ok, run_dir


@cli.command()
@click.argument(
    "eval_path", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "-g",
    "--grader",
    "grader_name",
    default="default",
    show_default=True,
    help="Name of the grader to apply",
)
@click.option(
    "--regrade",
    is_flag=True,
    help="Also grade runs that already have a Grade from this grader",
)
@runs_dir_option
def grade(eval_path, grader_name, regrade, runs_dir):
    "Apply a Grader to each Run in an Eval, producing Grades"
    eval_doc = load_eval(eval_path)
    runs_root = resolve_runs_root(eval_path, eval_doc, runs_dir)

    grader_path, grader = load_grader(eval_path, grader_name)

    run_files = sorted(runs_root.rglob("run.yaml"))
    if not run_files:
        raise click.ClickException(f"No runs found in {runs_root}")

    graded = skipped = stale = failures = failed_runs = 0
    for run_file in run_files:
        run_dir = run_file.parent
        # A failed Run is a harness error, not evidence - never grade it
        if run_failed(load_yaml(run_file)):
            failed_runs += 1
            continue
        grade_dir = run_dir / "grades" / grader_name
        if (grade_dir / "grade.yaml").exists() and not regrade:
            if grade_matches_grader(grade_dir, grader):
                skipped += 1
            else:
                stale += 1
            continue
        click.echo(f"{run_dir.relative_to(runs_root)} ... ", nl=False)
        record = grade_run(run_dir, grade_dir, grader, grader_path)
        graded += 1
        failures += record["outcome"] != "pass"
        score = record["score"]
        score_display = "" if score is None else f" score={score}"
        click.echo(f"{record['outcome']}{score_display}")

    if skipped:
        click.echo(f"Skipped {skipped} up-to-date grade(s)")
    if failed_runs:
        click.echo(f"Skipped {failed_runs} failed run(s)")
    if stale:
        click.echo(
            f"{stale} existing grade(s) came from an older version of "
            f"grader {grader_name!r} - use --regrade to discard and redo them"
        )
    if failures:
        raise click.ClickException(f"{failures} of {graded} run(s) graded as fail")


def grade_matches_grader(grade_dir, grader):
    "Was this Grade produced by the current version of the grader spec?"
    snapshot = grade_dir / "grader.yaml"
    if not snapshot.exists():
        return False
    # Parsed comparison: comment and formatting edits don't count
    return yaml.safe_load(snapshot.read_text()) == grader


def run_checks(run_dir, workspace, grader, checker_base_dir, task):
    """Execute every Check in a Grader's pipeline against a Run, returning
    (results, halted). Checks write their artifacts into workspace - the
    real grades/<name> dir for a persisted Grade, or a scratch directory
    for Studio's dry-run.
    """
    results = []
    halted = False
    for check in grader["checks"]:
        name = check["checker"]
        if halted:
            results.append({"checker": name, "skipped": True})
            continue
        if name in BUILTIN_CHECKERS:
            ok, info = BUILTIN_CHECKERS[name](check, run_dir, workspace)
        else:
            ok, info = execute_checker_program(
                check, run_dir, workspace, checker_base_dir, task
            )
        info = normalize_check_info(info)
        promised = check.get("creates")
        if ok and promised:
            names = [promised] if isinstance(promised, str) else promised
            missing = [name for name in names if not (workspace / name).exists()]
            if missing:
                ok = False
                info["notes"] = "did not create promised file(s): " + ", ".join(missing)
        # normalize_check_info guarantees info can't clobber core keys
        results.append({"checker": name, "ok": ok} | info)
        if not ok and check.get("required"):
            halted = True
    return results, halted


def score_and_outcome(results, halted, grader):
    "The score and pass/fail outcome for a Grade, from its Check results"
    # The score for the Grade is the last score any check produced - but
    # a check that failed without scoring leaves the Grade unscored, so a
    # stale score from an earlier check can't stand in for it
    unscored_failure = any(
        not r["ok"] and r.get("score") is None for r in results if "ok" in r
    )
    score = (
        None
        if unscored_failure
        else next(
            (r["score"] for r in reversed(results) if r.get("score") is not None),
            None,
        )
    )
    threshold = (grader.get("scoring") or {}).get("pass_threshold")
    if halted or not all(r["ok"] for r in results if "ok" in r):
        outcome = "fail"
    elif score is not None and threshold is not None:
        outcome = "pass" if score >= threshold else "fail"
    else:
        outcome = "pass"
    return score, outcome


def grade_run(run_dir, grade_dir, grader, grader_path):
    "Apply every Check in a Grader to one Run, writing grade.yaml"
    # Discarded grades are really discarded - no stale artifacts survive
    if grade_dir.exists():
        shutil.rmtree(grade_dir)
    grade_dir.mkdir(parents=True)
    # Snapshot the grader spec so each Grade records exactly how it
    # was produced, even after the grader is later edited
    (grade_dir / "grader.yaml").write_text(grader_path.read_text())
    # Older run.yaml files recorded just the task name, newer the full task
    task = load_yaml(run_dir / "run.yaml").get("task")
    results, halted = run_checks(run_dir, grade_dir, grader, grader_path.parent, task)
    score, outcome = score_and_outcome(results, halted, grader)

    record = {
        "grader": grader["name"],
        "graded": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "score": score,
        # Union of every check's tags, for cheap filtering and faceting
        "tags": sorted({t for r in results for t in r.get("tags", [])}),
        "checks": results,
    }
    (grade_dir / "grade.yaml").write_text(yaml.safe_dump(record, sort_keys=False))
    return record


def execute_checker_program(check, run_dir, grade_dir, grader_dir, task):
    "Run a CLI Checker, returning (ok, extra result fields)"
    checker = (grader_dir / check["checker"]).resolve()
    if not (checker.is_file() and os.access(checker, os.X_OK)):
        return False, {"notes": f"Checker {checker} is not an executable file"}
    # Absolute path: checkers run with cwd set to the grade workspace.
    # Scalar check and task keys become individual env vars, for shell scripts.
    env = (
        os.environ
        | scalar_env_vars("SMEVALS_CHECK_", check)
        | {
            "SMEVALS_RUN_DIR": str(run_dir.resolve()),
            "SMEVALS_CHECK": json.dumps(check),
        }
    )
    if isinstance(task, dict):
        env |= scalar_env_vars("SMEVALS_TASK_", task)
    task_name = task.get("name") if isinstance(task, dict) else task
    if task_name:
        env["SMEVALS_TASK"] = task_name
    result = subprocess.run(
        [str(checker)], cwd=grade_dir, env=env, capture_output=True, text=True
    )
    ok = result.returncode == 0
    info = {}
    if result.stdout.strip():
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            info = {"output": result.stdout.strip()}
    if not ok and result.stderr.strip():
        info.setdefault("notes", result.stderr.strip())
    return ok, info


def check_contains(check, run_dir, grade_dir):
    "Built-in: does the Run output contain a string?"
    value = check["value"]
    if value in (run_dir / "output.txt").read_text():
        return True, {}
    return False, {"notes": f"output.txt does not contain {value!r}"}


def check_xml_valid(check, run_dir, grade_dir):
    "Built-in: is a workspace (or Run) file well-formed XML?"
    path = grade_dir / check["file"]
    if not path.exists():
        path = run_dir / check["file"]
    if not path.exists():
        return False, {"notes": f"no such file: {check['file']}"}
    try:
        ET.parse(path)
        return True, {}
    except ET.ParseError as ex:
        return False, {"notes": f"XML parse error: {ex}"}


BUILTIN_CHECKERS = {
    "contains": check_contains,
    "xml-valid": check_xml_valid,
}


def discover_evals(path):
    """Yield Eval directories under path.

    A directory containing eval.yaml is an Eval; any other directory is
    treated as a Suite and searched recursively. Descent stops at each
    Eval, so runs/ trees are never scanned.
    """
    if (path / "eval.yaml").exists():
        yield path
        return
    for child in sorted(path.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield from discover_evals(child)


def resolve_eval_slugs(eval_paths):
    "Map slug -> eval path, discovering Evals inside Suite dirs"
    evals = {}
    for eval_path in eval_paths:
        found = list(discover_evals(eval_path))
        if not found:
            raise click.ClickException(
                f"No Evals found in {eval_path} - an Eval directory "
                "contains an eval.yaml file"
            )
        for found_path in found:
            doc = load_eval(found_path)
            slug = slugify(doc.get("name") or found_path.name)
            if slug in evals:
                raise click.ClickException(
                    f"Duplicate eval slug {slug!r}: {evals[slug]} and {found_path}"
                )
            evals[slug] = found_path
    return evals


@cli.command()
@click.argument(
    "eval_paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("-g", "--grader", "grader_name", default="default", show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("-p", "--port", default=7001, show_default=True)
def serve(eval_paths, grader_name, host, port):
    "Serve a live HTML report over one or more Evals"
    from . import site

    evals = resolve_eval_slugs(eval_paths)
    click.echo(f"Serving {len(evals)} eval(s) on http://{host}:{port}")
    site.run_server(evals, grader_name, host, port)


@cli.command()
@click.argument(
    "eval_paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("-g", "--grader", "grader_name", default="default", show_default=True)
@click.option(
    "-o",
    "--output",
    "site_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("build"),
    show_default=True,
    help="Site directory to build into (adds to an existing site)",
)
def build(eval_paths, grader_name, site_dir):
    "Build (or add to) a static HTML report site"
    from . import site

    evals = resolve_eval_slugs(eval_paths)
    site_dir.mkdir(parents=True, exist_ok=True)
    for slug, eval_path in evals.items():
        slug, count = site.build_eval(eval_path, site_dir, grader_name, slug)
        click.echo(f"{slug}: {count} runs -> {site_dir / 'evals' / slug}")
    click.echo(f"Site: {site_dir / 'index.html'}")


@cli.command()
@click.argument(
    "root",
    metavar="[DIR]",
    required=False,
    default=Path("."),
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("-p", "--port", default=7002, show_default=True)
def studio(root, port):
    """Serve smevals Studio, a local environment for authoring Evals

    DIR defaults to the current directory (Suite semantics identical to
    serve's discovery: every Eval found under it appears on the shelf).
    Binds 127.0.0.1 only, with no auth - Studio writes files and executes
    Runners and Checkers, so it must never be exposed on the network.
    """
    from . import studio as studio_app

    click.echo(f"Studio serving {root} on http://127.0.0.1:{port}")
    studio_app.run_studio(root, port)


@cli.command()
@click.argument(
    "eval_path", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "-g",
    "--grader",
    "grader_name",
    default="default",
    show_default=True,
    help="Report on Grades produced by this grader",
)
@click.option("--by-task", is_flag=True, help="Include per-task scores in model blocks")
@click.option("as_json", "--json", is_flag=True, help="Emit raw grade rows as JSON")
@runs_dir_option
def report(eval_path, grader_name, by_task, as_json, runs_dir):
    "Aggregate an Eval's Grades into a markdown report"
    eval_doc = load_eval(eval_path)
    runs_root = resolve_runs_root(eval_path, eval_doc, runs_dir)

    grader_path, grader = load_grader(eval_path, grader_name)
    grader_version = hashlib.sha256(grader_path.read_bytes()).hexdigest()[:7]

    rows, ungraded, stale, failed = collect_grade_rows(runs_root, grader_name, grader)
    if not rows:
        raise click.ClickException(
            f"No grades from grader {grader_name!r} found in {runs_root}"
        )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "eval": eval_doc.get("name") or eval_path.name,
                    "grader": grader_name,
                    "grader_version": grader_version,
                    "excluded_failed_runs": failed,
                    "rows": rows,
                },
                indent=2,
            )
        )
        return

    fails = sum(row["outcome"] != "pass" for row in rows)
    lines = [
        f"# {eval_doc.get('name') or eval_path.name}",
        "",
        f"- Grader: {grader_name} (version {grader_version})",
        f"- Graded: {len(rows)} runs ({fails} failed, {ungraded} ungraded)",
        f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    if failed:
        lines.append(f"- Excluded: {failed} run(s) (runner failed)")
    if stale:
        lines.append(
            f"- ⚠ {stale} of {len(rows)} grades came from an older version "
            f"of grader {grader_name!r} - consider --regrade"
        )
    lines += render_leaderboard(rows)
    lines += render_tag_shares(rows)
    lines += render_model_blocks(rows, by_task)
    click.echo("\n".join(lines))


def collect_grade_rows(runs_root, grader_name, grader):
    "One row per Grade, plus counts of ungraded, stale and failed runs"
    rows = []
    ungraded = stale = failed = 0
    for run_file in sorted(runs_root.rglob("run.yaml")):
        run_dir = run_file.parent
        run = load_yaml(run_file)
        # Failed Runs are harness errors, not evidence: excluded from the
        # stats even if a Grade was recorded against one in the past
        if run_failed(run):
            failed += 1
            continue
        grade_file = run_dir / "grades" / grader_name / "grade.yaml"
        if not grade_file.exists():
            ungraded += 1
            continue
        stale += not grade_matches_grader(grade_file.parent, grader)
        grade = load_yaml(grade_file)
        task = run.get("task")
        metrics = {}
        for check in grade.get("checks", []):
            metrics.update(check.get("metrics") or {})
        rows.append(
            {
                "task": task.get("name") if isinstance(task, dict) else task,
                "config": run.get("config", {}).get("name"),
                "model": run.get("config", {}).get("model"),
                "outcome": grade.get("outcome"),
                "score": grade.get("score"),
                "tags": grade.get("tags") or [],
                "metrics": metrics,
                "run_dir": str(run_dir),
            }
        )
    return rows, ungraded, stale, failed


def mean_stderr(values):
    mean = sum(values) / len(values)
    if len(values) > 1:
        stderr = statistics.stdev(values) / math.sqrt(len(values))
        return f"{mean:.2f} ±{stderr:.2f}"
    return f"{mean:.2f}"


def group_rows(rows):
    "Group rows by (config, model), preserving a stable order"
    groups = {}
    for row in rows:
        groups.setdefault((row["config"], row["model"]), []).append(row)
    return groups


def group_summary(group):
    scores = [r["score"] for r in group if r["score"] is not None]
    fails = sum(r["outcome"] != "pass" for r in group)
    counts = f"{len(group)} run{'s' if len(group) != 1 else ''}"
    if fails:
        counts += f", {fails} fail{'s' if fails != 1 else ''}"
    return scores, counts


def render_leaderboard(rows):
    entries = []
    for (config, model), group in group_rows(rows).items():
        scores, counts = group_summary(group)
        display = mean_stderr(scores) if scores else "-"
        sort_key = sum(scores) / len(scores) if scores else -1
        entries.append((sort_key, display, model, config, counts))
    entries.sort(key=lambda e: (-e[0], e[2]))

    score_width = max(len(e[1]) for e in entries)
    model_width = max(len(e[2]) for e in entries)
    rank_width = len(str(len(entries)))
    lines = ["", "## Leaderboard", ""]
    rank = 0
    previous_display = None
    for position, (_, display, model, config, counts) in enumerate(entries, 1):
        # Standard competition ranking: tied displayed scores share a rank
        if display != previous_display:
            rank = position
            previous_display = display
        lines.append(
            f"{rank:>{rank_width}}. {display:<{score_width}}  {model:<{model_width}}"
            f"  ({config}, {counts})"
        )
    return lines


def render_tag_shares(rows):
    counts = {}
    for row in rows:
        for tag in row["tags"]:
            counts[tag] = counts.get(tag, 0) + 1
    if not counts:
        return []
    total = len(rows)
    width = len(str(total))
    lines = ["", "## Tags", ""]
    for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        share = f"({count / total:.0%})"
        lines.append(f"- {count:>{width}}/{total} {share:>6}  {tag}")
    return lines


def render_model_blocks(rows, by_task):
    lines = []
    for (config, model), group in sorted(
        group_rows(rows).items(),
        key=lambda item: -(lambda s: sum(s) / len(s) if s else -1)(
            [r["score"] for r in item[1] if r["score"] is not None]
        ),
    ):
        scores, counts = group_summary(group)
        lines += ["", f"## {model} ({config})", ""]
        score_display = mean_stderr(scores) if scores else "-"
        lines.append(f"- score: {score_display} over {counts}")
        for key in sorted({k for r in group for k in r["metrics"]}):
            values = [r["metrics"][key] for r in group if key in r["metrics"]]
            if all(isinstance(v, bool) for v in values):
                display = f"{sum(values) / len(values):.0%}"
            else:
                display = mean_stderr([float(v) for v in values])
            lines.append(f"- {key}: {display}")
        tag_counts = {}
        for row in group:
            for tag in row["tags"]:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if tag_counts:
            if len(group) == 1:
                rendered = ", ".join(sorted(tag_counts))
            else:
                rendered = ", ".join(
                    f"{tag} {count / len(group):.0%}"
                    for tag, count in sorted(
                        tag_counts.items(), key=lambda item: (-item[1], item[0])
                    )
                )
            lines.append(f"- tags: {rendered}")
        if by_task:
            tasks = {}
            for row in group:
                tasks.setdefault(row["task"], []).append(row)
            lines.append("- scores by task:")
            for task_name in sorted(tasks):
                task_scores = [
                    r["score"] for r in tasks[task_name] if r["score"] is not None
                ]
                display = mean_stderr(task_scores) if task_scores else "-"
                lines.append(f"  - {task_name}: {display}")
    return lines
