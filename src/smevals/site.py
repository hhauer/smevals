"""Shared data layer for the HTML report, plus the live server and
static site builder.

Both `smevals serve` and `smevals build` produce the same site shape:

    index.html            the app (single self-contained file)
    index.json            site manifest: one entry per eval
    evals/<slug>/eval.json    everything about one eval
    evals/<slug>/runs/...     run + grade artifacts

serve generates the JSON per request straight from the eval
directories (mtime-cached), so the page updates live as Runs and
Grades land. build writes the same JSON to disk and copies artifacts,
producing a self-contained static site; each build call adds or
refreshes ONE eval and merges into an existing site's index.json.
"""

import json
import mimetypes
import shutil
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

import yaml

from .cli import grade_matches_grader, mean_stderr, run_failed

_yaml_cache = {}


def cached_yaml(path):
    key = str(path)
    mtime = path.stat().st_mtime_ns
    hit = _yaml_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    data = yaml.safe_load(path.read_text())
    _yaml_cache[key] = (mtime, data)
    return data


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def collect_eval(eval_path, default_grader="default"):
    "Everything the app needs to render one eval, as one JSON document"
    doc = cached_yaml(eval_path / "eval.yaml")
    tasks = [cached_yaml(p) for p in sorted((eval_path / "tasks").glob("*.yaml"))]
    configs = [cached_yaml(p) for p in sorted((eval_path / "configs").glob("*.yaml"))]
    grader_names = sorted(p.stem for p in (eval_path / "graders").glob("*.yaml"))
    graders = {
        name: cached_yaml(eval_path / "graders" / f"{name}.yaml")
        for name in grader_names
    }
    if default_grader not in graders:
        default_grader = grader_names[0] if grader_names else None

    rows = []
    runs_root = eval_path / "runs"
    if runs_root.exists():
        for run_file in sorted(runs_root.rglob("run.yaml")):
            run_dir = run_file.parent
            run = cached_yaml(run_file)
            task = run.get("task")
            config = run.get("config", {})
            grades = {}
            for name in grader_names:
                grade_file = run_dir / "grades" / name / "grade.yaml"
                if not grade_file.exists():
                    continue
                grade = cached_yaml(grade_file)
                grades[name] = {
                    "outcome": grade.get("outcome"),
                    "score": grade.get("score"),
                    "graded": grade.get("graded"),
                    "tags": grade.get("tags") or [],
                    "checks": grade.get("checks") or [],
                    "files": sorted(
                        p.name
                        for p in grade_file.parent.iterdir()
                        if p.is_file() and p.name != "grade.yaml"
                    ),
                }
            rows.append(
                {
                    "run": str(run_dir.relative_to(runs_root)),
                    "task": task.get("name") if isinstance(task, dict) else task,
                    "config": config.get("name"),
                    "model": config.get("model"),
                    "started": run.get("started"),
                    "duration": run.get("duration_seconds"),
                    "exit_code": run.get("exit_code"),
                    "imported_from": run.get("imported_from"),
                    "files": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
                    "grades": grades,
                }
            )

    return {
        "eval": {
            "name": doc.get("name") or eval_path.name,
            "description": doc.get("description", ""),
            "tasks": tasks,
            "configs": configs,
            "graders": graders,
            "default_grader": default_grader,
        },
        "rows": rows,
        "generated": now_iso(),
    }


def eval_summary(slug, data):
    "The index.json entry for one eval, scored by its default grader"
    rows = data["rows"]
    grader = data["eval"]["default_grader"]
    grades = [row["grades"][grader] for row in rows if grader in row["grades"]]
    best = None
    groups = {}
    for row in rows:
        grade = row["grades"].get(grader) or {}
        if grade.get("score") is not None:
            groups.setdefault((row["config"], row["model"]), []).append(grade["score"])
    for (config, model), scores in groups.items():
        mean = sum(scores) / len(scores)
        if best is None or mean > best["score"]:
            best = {
                "config": config,
                "model": model,
                "score": round(mean, 3),
                "runs": len(scores),
            }
    return {
        "slug": slug,
        "name": data["eval"]["name"],
        "description": data["eval"]["description"],
        "runs": len(rows),
        "graded": len(grades),
        "fails": sum(1 for g in grades if g.get("outcome") == "fail"),
        "graders": grader_list(data),
        "best": best,
        "updated": data["generated"],
    }


def grader_list(data):
    return sorted(data["eval"]["graders"])


# --- results aggregation --------------------------------------------------
#
# Reporter semantics (adopted from the scratchpad summarize.py prototype):
# a failed Run (non-zero exit_code) is a harness error, not evidence, and
# is excluded from every mean; a Grade's staleness is checked against the
# Eval's current grader spec; an Eval's target sample size is inferred as
# the highest non-failed run count seen for any (config, model) pair, and
# a model whose every Run failed is still shown (at n=0) rather than
# silently vanishing. group_stats/eval_results/results_matrix are pure
# functions over collect_eval's output - no I/O beyond what collect_eval
# and grade_matches_grader already do.


def grade_metrics(grade):
    "Merge every Check's metrics dict into one, like cli.collect_grade_rows does"
    metrics = {}
    for check in grade.get("checks") or []:
        metrics.update(check.get("metrics") or {})
    return metrics


def group_stats(rows, grader_name):
    """Group collect_eval rows by (config, model) into per-group score
    stats: {config, model, n, scored_n, mean, stderr, pass_rate,
    fail_count, tag_counts, metrics}, sorted by mean descending then
    model (reporter's ranking rule).

    A failed Run never reaches a group; a non-failed Run with no Grade
    from grader_name is not counted here either (see eval_results'
    "ungraded" for that gap - group_stats only summarizes what was
    graded). metrics summarizes each key exactly as
    cli.render_model_blocks does: a key whose every value is a bool
    becomes a true-rate, anything else becomes mean +- stderr.
    """
    groups = {}
    for row in rows:
        if run_failed(row):
            continue
        grade = row["grades"].get(grader_name)
        if grade is None:
            continue
        groups.setdefault((row["config"], row["model"]), []).append(grade)

    stats = []
    for (config, model), grades in groups.items():
        n = len(grades)
        scores = [g["score"] for g in grades if g.get("score") is not None]
        mean, stderr = mean_stderr(scores) if scores else (None, None)
        pass_rate = sum(g.get("outcome") == "pass" for g in grades) / n if n else 0.0
        fail_count = sum(g.get("outcome") != "pass" for g in grades)

        tag_counts = {}
        for grade in grades:
            for tag in grade.get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        metrics_per_grade = [grade_metrics(g) for g in grades]
        metrics = {}
        for key in sorted({k for m in metrics_per_grade for k in m}):
            values = [m[key] for m in metrics_per_grade if key in m]
            if all(isinstance(v, bool) for v in values):
                metrics[key] = {
                    "type": "bool",
                    "true_rate": sum(values) / len(values),
                    "n": len(values),
                }
            else:
                m_mean, m_stderr = mean_stderr([float(v) for v in values])
                metrics[key] = {
                    "type": "numeric",
                    "mean": m_mean,
                    "stderr": m_stderr,
                    "n": len(values),
                }

        stats.append(
            {
                "config": config,
                "model": model,
                "n": n,
                "scored_n": len(scores),
                "mean": mean,
                "stderr": stderr,
                "pass_rate": pass_rate,
                "fail_count": fail_count,
                "tag_counts": tag_counts,
                "metrics": metrics,
            }
        )
    stats.sort(
        key=lambda s: (-(s["mean"] if s["mean"] is not None else -1.0), s["model"])
    )
    return stats


def eval_results(eval_path, grader_name="default", task=None):
    """One Eval's Results-tab document: {grader, graders, task, groups,
    tags, total, excluded_failed, ungraded, stale, generated}. grader_name
    falls back exactly like collect_eval/serve do when it names a grader
    the Eval doesn't have. task, when given, scopes every figure to that
    Task's Runs only - the Results tab's per-task leaderboard (Batch 3
    item 6) rides this rather than a second, client-side aggregation path.
    """
    data = collect_eval(eval_path, grader_name)
    grader_name = data["eval"]["default_grader"]
    rows = data["rows"]
    if task is not None:
        rows = [row for row in rows if row["task"] == task]

    if grader_name is None:
        return {
            "grader": None,
            "graders": grader_list(data),
            "task": task,
            "groups": [],
            "tags": {},
            "total": 0,
            "excluded_failed": sum(1 for row in rows if run_failed(row)),
            "ungraded": 0,
            "stale": 0,
            "generated": data["generated"],
        }

    groups = group_stats(rows, grader_name)
    tags = {}
    for group in groups:
        for tag, count in group["tag_counts"].items():
            tags[tag] = tags.get(tag, 0) + count

    grader_spec = data["eval"]["graders"][grader_name]
    excluded_failed = ungraded = stale = 0
    for row in rows:
        if run_failed(row):
            excluded_failed += 1
            continue
        grade = row["grades"].get(grader_name)
        if grade is None:
            ungraded += 1
            continue
        grade_dir = eval_path / "runs" / row["run"] / "grades" / grader_name
        if not grade_matches_grader(grade_dir, grader_spec):
            stale += 1

    return {
        "grader": grader_name,
        "graders": grader_list(data),
        "task": task,
        "groups": groups,
        "tags": tags,
        "total": sum(g["n"] for g in groups),
        "excluded_failed": excluded_failed,
        "ungraded": ungraded,
        "stale": stale,
        "generated": data["generated"],
    }


def run_counts_by_group(rows):
    "Non-failed run count per (config, model) - ground truth independent of grading progress"
    counts = {}
    for row in rows:
        if run_failed(row) or row["config"] is None or row["model"] is None:
            continue
        key = (row["config"], row["model"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def failed_counts_by_group(rows):
    "Failed (harness-error) run count per (config, model)"
    counts = {}
    for row in rows:
        if not run_failed(row) or row["config"] is None or row["model"] is None:
            continue
        key = (row["config"], row["model"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def primary_config(groups, run_counts):
    "The config with the most runs recorded for this Eval - almost always 'default'"
    tally = {}
    for group in groups:
        tally[group["config"]] = tally.get(group["config"], 0) + group["n"]
    if not tally:
        for (config, _model), n in run_counts.items():
            tally[config] = tally.get(config, 0) + n
    if not tally:
        return None
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def results_matrix(evals):
    """The executive summary: {evals, models, matrix: {model: {slug:
    {mean, n, target_n, incomplete}}}, generated} - default-grader mean
    score by model x eval, reporter semantics throughout. evals: slug ->
    Eval Path (e.g. discover_slugs' return value).

    Per eval, only the busiest config (the one with the most runs) is
    shown - the documented limitation the reporter prototype carries: an
    Eval genuinely running one model under two configs only shows one of
    them here.
    """
    eval_ids = sorted(evals)
    models = set()
    matrix = {}
    for slug in eval_ids:
        data = collect_eval(evals[slug])
        grader_name = data["eval"]["default_grader"]
        rows = data["rows"]
        run_counts = run_counts_by_group(rows)
        failed_counts = failed_counts_by_group(rows)
        groups = group_stats(rows, grader_name) if grader_name else []
        config = primary_config(groups, run_counts)
        target_n = max(run_counts.values(), default=0)

        for group in groups:
            if group["config"] != config:
                continue
            models.add(group["model"])
            n = run_counts.get((config, group["model"]), group["n"])
            matrix.setdefault(group["model"], {})[slug] = {
                "mean": group["mean"],
                "n": n,
                "target_n": target_n,
                "incomplete": n < target_n,
            }
        # A model whose every Run failed has zero non-failed runs and no
        # Grade, so it never reaches `groups` above - without this it
        # would vanish from the matrix instead of showing n=0
        for (cfg, model), _failed in failed_counts.items():
            if cfg != config or (cfg, model) in run_counts:
                continue
            models.add(model)
            matrix.setdefault(model, {})[slug] = {
                "mean": None,
                "n": 0,
                "target_n": target_n,
                "incomplete": 0 < target_n,
            }

    return {
        "evals": eval_ids,
        "models": sorted(models),
        "matrix": matrix,
        "generated": now_iso(),
    }


def app_html():
    return (files("smevals") / "app.html").read_text()


# --- live server ---------------------------------------------------------


def run_server(evals, grader_name, host, port):
    "evals: dict of slug -> eval directory Path"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self.reply(200, app_html().encode(), "text/html")
            elif path == "/index.json":
                entries = [
                    eval_summary(slug, collect_eval(eval_path, grader_name))
                    for slug, eval_path in evals.items()
                ]
                self.reply_json(
                    {"evals": entries, "live": True, "generated": now_iso()}
                )
            elif path.startswith("/evals/"):
                self.serve_eval(path.removeprefix("/evals/"))
            else:
                self.reply(404, b"not found", "text/plain")

        def serve_eval(self, rest):
            slug, _, tail = rest.partition("/")
            eval_path = evals.get(slug)
            if eval_path is None:
                return self.reply(404, b"no such eval", "text/plain")
            if tail == "eval.json":
                return self.reply_json(collect_eval(eval_path, grader_name))
            if tail.startswith("runs/"):
                runs_root = (eval_path / "runs").resolve()
                target = (eval_path / tail).resolve()
                if target.is_relative_to(runs_root) and target.is_file():
                    # YAML and unknown types render inline, not download
                    if target.suffix in (".yaml", ".yml"):
                        ctype = "text/plain; charset=utf-8"
                    else:
                        ctype = (
                            mimetypes.guess_type(target.name)[0]
                            or "text/plain; charset=utf-8"
                        )
                    return self.reply(200, target.read_bytes(), ctype)
            self.reply(404, b"not found", "text/plain")

        def reply_json(self, data):
            self.reply(200, json.dumps(data).encode(), "application/json")

        def reply(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


# --- static build --------------------------------------------------------


def build_eval(eval_path, site_dir, grader_name, slug):
    "Add or refresh one eval in a static site directory"
    data = collect_eval(eval_path, grader_name)

    eval_dir = site_dir / "evals" / slug
    if eval_dir.exists():
        shutil.rmtree(eval_dir)
    eval_dir.mkdir(parents=True)
    (eval_dir / "eval.json").write_text(json.dumps(data))

    runs_root = eval_path / "runs"
    if runs_root.exists():
        shutil.copytree(runs_root, eval_dir / "runs")

    index_file = site_dir / "index.json"
    entries = []
    if index_file.exists():
        try:
            entries = json.loads(index_file.read_text()).get("evals", [])
        except json.JSONDecodeError:
            entries = []
    entries = [e for e in entries if e.get("slug") != slug]
    entries.append(eval_summary(slug, data))
    entries.sort(key=lambda e: e["slug"])
    index_file.write_text(
        json.dumps({"evals": entries, "live": False, "generated": now_iso()})
    )
    (site_dir / "index.html").write_text(app_html())
    return slug, len(data["rows"])
