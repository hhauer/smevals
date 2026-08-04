"""smevals studio: a local, read-write authoring environment for Evals.

`run_studio(root, port, token)` serves the single-page app plus a JSON
API over every Eval discovered under root (Suite semantics identical to
serve's discovery - see cli.discover_evals). Binds 127.0.0.1 only: studio
writes files and executes runners, so it must never be exposed on the
network - and binding loopback alone is not enough, since any web page
open in the same browser can still reach it (there is no same-origin
restriction on cross-origin GET or simple POST). Every /api/* request
must carry token in an X-Studio-Key header, generated fresh per run by
generate_token(); GET / (the HTML shell) stays unauthenticated, since it
carries no data and is useless without the token that only the CLI's
startup message shows.

Task 2 carried the read APIs: the shelf listing, one Eval's file tree +
validation, and guarded file reads. This module now also carries the
write + execute APIs (Task 3): scaffolding new Evals, atomic file writes,
running a Task and grading a Run as background jobs, dry-running a grader
against unsaved edits, and best-effort model suggestions. Runs and grades
go through cli.py's real execution/grading internals (execute_run,
run_checks, score_and_outcome) so Studio never re-implements them.

Phase 2 adds the sweep orchestrator endpoints (/api/sweep, its plan
preview and cancel) on top of the same job table: one sweep at a time,
executed by sweep.run_sweep in a background thread. The lock discipline:
a sweep holds an eval's active_jobs slot only around each individual
run, so bench actions queue politely between runs - and 409 with a
retry-inviting message while a run is in flight.
"""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import yaml

from . import sweep
from .authoring import FILE_SCHEMAS, scaffold_eval, validate_eval
from .cli import (
    discover_evals,
    execute_run,
    grade_run,
    load_eval,
    load_yaml,
    run_checks,
    run_failed,
    score_and_outcome,
    slugify,
)
from .site import (
    cached_yaml,
    collect_eval,
    eval_results,
    group_stats,
    now_iso,
    results_matrix,
)

# Directories with a fixed meaning in the canonical Eval layout; any other
# top-level file or directory groups under "other" in the file tree.
KNOWN_KINDS = ("tasks", "configs", "graders", "checkers")


def studio_html():
    return (files("smevals") / "studio.html").read_text()


def discover_slugs(root):
    """Map slug -> eval path for every Eval under root; empty root yields {}.

    Two Evals whose names slug identically are disambiguated with a
    numeric suffix (-2, -3, ...) rather than dropped: resolve_eval_slugs
    (serve/build) fails loud on the same collision, which is right for a
    batch CLI command, but Studio is a live UI - silently hiding an Eval
    because another one collided with it is worse than a numbered slug.
    """
    evals = {}
    for eval_path in discover_evals(root):
        doc = load_eval(eval_path)
        slug = base_slug = slugify(doc.get("name") or eval_path.name)
        suffix = 2
        while slug in evals:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        evals[slug] = eval_path
    return evals


def run_stats(eval_path):
    """The most recent Run's started timestamp plus total/failed counts,
    from one rglob pass over an Eval's runs/ - the shelf strip's cheap
    counts ride this walk rather than paying for a second one.
    """
    runs_root = eval_path / "runs"
    if not runs_root.exists():
        return None, 0, 0
    last = None
    total = failed = 0
    for run_file in runs_root.rglob("run.yaml"):
        run = cached_yaml(run_file)
        total += 1
        if run_failed(run):
            failed += 1
        started = run.get("started")
        if started and (last is None or started > last):
            last = started
    return last, total, failed


def best_group(eval_path):
    "Highest-mean (config, model) group under the default grader, or None - the shelf strip's best score"
    data = collect_eval(eval_path)
    grader_name = data["eval"]["default_grader"]
    if grader_name is None:
        return None
    groups = group_stats(data["rows"], grader_name)
    if not groups or groups[0]["mean"] is None:
        return None
    best = groups[0]
    return {
        "model": best["model"],
        "config": best["config"],
        "score": round(best["mean"], 3),
        "runs": best["n"],
    }


def eval_summary(slug, eval_path):
    "The /api/evals entry for one Eval"
    doc = cached_yaml(eval_path / "eval.yaml") or {}
    last_run_iso, total, failed = run_stats(eval_path)
    return {
        "slug": slug,
        "name": doc.get("name") or eval_path.name,
        "description": doc.get("description", ""),
        "counts": {
            kind: len(list((eval_path / kind).glob("*.yaml")))
            for kind in ("tasks", "configs", "graders")
        },
        "last_run_iso": last_run_iso,
        "problems": len(validate_eval(eval_path)),
        "runs": {"total": total, "failed": failed},
        "best": best_group(eval_path),
    }


def file_tree(eval_dir):
    """Every authorable file under an Eval dir, grouped by kind.

    Kinds are tasks/configs/graders/checkers (by directory convention -
    checkers/ is not a required part of the layout, but graders reference
    checker scripts there) plus other for everything else (eval.yaml, the
    runner script, .gitignore, task-specific reference data, ...). runs/
    is never listed: it is immutable and can be large.
    """
    groups = {kind: [] for kind in KNOWN_KINDS}
    groups["other"] = []
    for dirpath, dirnames, filenames in os.walk(eval_dir):
        rel_dir = Path(dirpath).relative_to(eval_dir)
        # runs/ is immutable and can be large - never walk into it
        skip = {"runs"} if rel_dir == Path(".") else set()
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        top = rel_dir.parts[0] if rel_dir.parts else None
        kind = top if top in KNOWN_KINDS else "other"
        for filename in filenames:
            path = Path(dirpath) / filename
            groups[kind].append(
                {
                    "path": str(path.relative_to(eval_dir)),
                    "executable": os.access(path, os.X_OK),
                }
            )
    for group in groups.values():
        group.sort(key=lambda entry: entry["path"])
    return groups


def eval_detail(eval_dir):
    "The /api/evals/<slug> response: file tree + the same problems run/grade would hit"
    return {"files": file_tree(eval_dir), "validation": validate_eval(eval_dir)}


def resolve_within(base_dir, rel):
    """Resolve a path relative to base_dir, guarding against traversal.

    Rejects ../ escapes, absolute paths and symlinks resolving outside
    base_dir - an is_relative_to containment check (unlike a string-prefix
    check, this can't be fooled by a sibling directory whose name merely
    starts with the same prefix), same as serve's site.py uses. A path
    carrying an embedded null byte makes Path.resolve() raise instead of
    returning - that is not-found too, not a server error. The target
    need not already exist, so callers writing a brand new file (or a
    not-yet-created scaffold parent) can use this too.
    """
    if not rel:
        return None
    base_root = base_dir.resolve()
    try:
        target = (base_dir / rel).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(base_root):
        return None
    return target


def nesting_eval_dir(root, parent_dir):
    """The Eval dir (one with an eval.yaml) at or above parent_dir, up to
    and including root - or None.

    Guards scaffolding: a new Eval must never land inside an existing
    one's directory tree. Without this, POST /api/evals's parent param
    (only root-containment checked) can target an existing Eval's runs/
    (bypassing the immutability invariant) or any other spot inside it
    (nesting an Eval discover_evals would never find, since it stops
    walking at the first eval.yaml it sees).
    """
    d = parent_dir
    while True:
        if (d / "eval.yaml").is_file():
            return d
        if d == root:
            return None
        d = d.parent


def resolve_eval_file(eval_dir, rel):
    "Resolve rel to an existing file within an Eval dir, or None (see resolve_within)"
    target = resolve_within(eval_dir, rel)
    if target is None or not target.is_file():
        return None
    return target


def resolve_run_dir(eval_dir, rel):
    "Resolve rel to a Run directory under an Eval's runs/, or None (see resolve_within)"
    target = resolve_within(eval_dir / "runs", rel)
    if target is None or not (target / "run.yaml").is_file():
        return None
    return target


def atomic_write(target, data):
    "Write bytes to target via tmp-file-plus-rename, so readers never see a partial write"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def job_echo(log_lines):
    "An execute_run-compatible echo that appends completed lines to log_lines"
    buffer = []

    def echo(message="", nl=True):
        buffer.append(str(message))
        if nl:
            log_lines.append("".join(buffer))
            buffer.clear()

    return echo


def tail_lines(text, n=20):
    "The last n lines of text, for surfacing a subprocess failure without flooding the job log"
    return "\n".join(text.splitlines()[-n:])


def config_models(root):
    "Every distinct model named in a Config across every Eval under root"
    models = set()
    for eval_path in discover_slugs(root).values():
        for config_file in (eval_path / "configs").glob("*.yaml"):
            model = (cached_yaml(config_file) or {}).get("model")
            if isinstance(model, str) and model:
                models.add(model)
    return models


def parse_llm_models_list(text):
    """Parse `llm models list`'s plain-text output into a set of model ids.

    Each line looks like "<provider label>: <model id>[ (aliases: ...)]",
    e.g. "OpenAI Chat: gpt-4o (aliases: 4o)" or, alias-less, "OpenAI Chat:
    gpt-4o-audio-preview". llm (0.31.1) has no machine-readable option for
    this command - `models list --json` doesn't exist - so this is a
    best-effort scrape of the human-oriented output: a line that doesn't
    fit the pattern is just skipped, since a garbled model list is a
    smaller suggestion list, never a hard failure.
    """
    models = set()
    for line in text.splitlines():
        _, sep, rest = line.partition(": ")
        if not sep:
            continue
        model_id = rest.split(" (aliases:", 1)[0].strip()
        if model_id:
            models.add(model_id)
    return models


def llm_models():
    "Best-effort model set from `llm models list`, empty if unavailable within 2s"
    try:
        result = subprocess.run(
            ["llm", "models", "list"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return parse_llm_models_list(result.stdout)


def generate_token():
    "A fresh random URL-safe token, unique per `smevals studio` run"
    return secrets.token_urlsafe(32)


def run_studio(root, port, token):
    "Serve smevals Studio over every Eval discovered under root, gated by token"
    root = Path(root).resolve()

    # Job table + one-active-job-per-eval bookkeeping, and the llm-models
    # cache: all scoped to this server's lifetime (one `smevals studio`
    # invocation is one process, so this doubles as the process-lifetime
    # cache the design calls for, while still giving each test its own).
    jobs = {}
    active_jobs = {}
    jobs_lock = threading.Lock()
    llm_models_cache = {}
    lms_inventory_cache = {}
    # The one reserved sweep slot beside active_jobs: the active-or-most-
    # recent sweep job plus its cancel Event, guarded by jobs_lock
    sweep_state = {"job": None, "cancel": None}

    def cached_llm_models():
        if "value" not in llm_models_cache:
            llm_models_cache["value"] = llm_models()
        return llm_models_cache["value"]

    def cached_lms_inventory():
        if "value" not in lms_inventory_cache:
            lms_inventory_cache["value"] = sweep.lms_inventory()
        return lms_inventory_cache["value"]

    def run_job(job, slug, runs_root, task, config_name, runner, model):
        "Execute a queued run job in the background, updating it in place"
        job["status"] = "running"
        log_lines = []
        try:
            ok, run_dir = execute_run(
                runs_root, task, config_name, runner, model, echo=job_echo(log_lines)
            )
        except Exception as ex:
            with jobs_lock:
                job["status"] = "failed"
                job["error"] = str(ex)
                job["log"] = log_lines
                del active_jobs[slug]
            return
        with jobs_lock:
            job["log"] = log_lines
            job["run_dir"] = str(run_dir.relative_to(runs_root))
            if ok:
                job["status"] = "done"
            else:
                job["status"] = "failed"
                stderr_file = run_dir / "stderr.txt"
                job["error"] = (
                    tail_lines(stderr_file.read_text())
                    if stderr_file.exists()
                    else "runner exited non-zero"
                )
            del active_jobs[slug]

    class Handler(BaseHTTPRequestHandler):
        # Every API error must come back as JSON {error}, per the spec -
        # never a dropped connection. do_GET/do_POST/do_PUT wrap their real
        # dispatch in a catch-all: any exception that escapes a handler
        # (a malformed on-disk file, a checker crash, ...) becomes a JSON
        # 500 instead of killing the request thread with no reply. The
        # traceback still goes to stderr, never to the client.
        def do_GET(self):
            self.safe_dispatch(self.dispatch_get)

        def do_POST(self):
            self.safe_dispatch(self.dispatch_post)

        def do_PUT(self):
            self.safe_dispatch(self.dispatch_put)

        def safe_dispatch(self, handler):
            try:
                handler()
            except Exception as ex:
                traceback.print_exc(file=sys.stderr)
                self.reply_error(500, f"internal error: {ex}")

        def authorized(self, path):
            """True if path may proceed: every /api/* route needs the
            correct X-Studio-Key header; GET / (the HTML shell) does not -
            it carries no data and is useless without the token only the
            CLI's startup message prints.
            """
            return (
                not path.startswith("/api/")
                or self.headers.get("X-Studio-Key") == token
            )

        def dispatch_get(self):
            parts = urllib.parse.urlsplit(self.path)
            if not self.authorized(parts.path):
                return self.reply_error(403, "missing or invalid X-Studio-Key header")
            if parts.path == "/":
                return self.reply(200, studio_html().encode(), "text/html")
            if parts.path == "/api/evals":
                return self.reply_json(
                    [
                        eval_summary(slug, eval_path)
                        for slug, eval_path in sorted(discover_slugs(root).items())
                    ]
                )
            if parts.path == "/api/schemas":
                return self.reply_json(FILE_SCHEMAS)
            if parts.path == "/api/models":
                local = cached_lms_inventory()
                return self.reply_json(
                    {
                        "models": sorted(
                            config_models(root) | cached_llm_models() | local
                        ),
                        "local": sorted(local),
                    }
                )
            if parts.path == "/api/results":
                return self.reply_json(results_matrix(discover_slugs(root)))
            if parts.path == "/api/sweep":
                return self.handle_get_sweep()
            if parts.path == "/api/sweep/plan":
                return self.handle_sweep_plan(parts.query)
            if parts.path.startswith("/api/jobs/"):
                return self.handle_get_job(parts.path.removeprefix("/api/jobs/"))
            if parts.path.startswith("/api/evals/"):
                return self.serve_eval_api(
                    parts.path.removeprefix("/api/evals/"), parts.query
                )
            self.reply_error(404, "not found")

        def dispatch_post(self):
            parts = urllib.parse.urlsplit(self.path)
            if not self.authorized(parts.path):
                return self.reply_error(403, "missing or invalid X-Studio-Key header")
            if parts.path == "/api/evals":
                return self.handle_scaffold()
            if parts.path == "/api/sweep":
                return self.handle_post_sweep()
            if parts.path == "/api/sweep/cancel":
                return self.handle_cancel_sweep()
            if parts.path.startswith("/api/evals/"):
                slug, _, tail = parts.path.removeprefix("/api/evals/").partition("/")
                eval_dir = self.resolve_eval(slug)
                if eval_dir is None:
                    return
                if tail == "run":
                    return self.handle_run(slug, eval_dir)
                if tail == "grade":
                    return self.handle_grade(slug, eval_dir)
                if tail == "dryrun":
                    return self.handle_dryrun(eval_dir)
            self.reply_error(404, "not found")

        def dispatch_put(self):
            parts = urllib.parse.urlsplit(self.path)
            if not self.authorized(parts.path):
                return self.reply_error(403, "missing or invalid X-Studio-Key header")
            if parts.path.startswith("/api/evals/"):
                slug, _, tail = parts.path.removeprefix("/api/evals/").partition("/")
                eval_dir = self.resolve_eval(slug)
                if eval_dir is None:
                    return
                if tail == "file":
                    return self.handle_put_file(eval_dir)
            self.reply_error(404, "not found")

        def resolve_eval(self, slug):
            "The Eval dir for slug, or None after replying 404"
            eval_dir = discover_slugs(root).get(slug)
            if eval_dir is None:
                self.reply_error(404, f"no such eval: {slug}")
                return None
            return eval_dir

        def read_json_body(self):
            """The request body parsed as a JSON object, or None (already
            replied 400/415).

            The Content-Type check runs before anything reads the body: a
            plain HTML <form> cannot set Content-Type to application/json
            (only text/plain, application/x-www-form-urlencoded or
            multipart/form-data are form-submittable), so this alone
            defeats simple-request CSRF against every write endpoint. A
            cross-origin fetch that lies about its Content-Type instead
            triggers a CORS preflight, which this server never answers.
            """
            content_type = self.headers.get("Content-Type") or ""
            if "application/json" not in content_type:
                self.reply_error(415, "request body must be application/json")
                return None
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = None
            if not isinstance(data, dict):
                self.reply_error(400, "request body must be a JSON object")
                return None
            return data

        def serve_eval_api(self, rest, query):
            slug, _, tail = rest.partition("/")
            eval_dir = discover_slugs(root).get(slug)
            if eval_dir is None:
                return self.reply_error(404, f"no such eval: {slug}")
            if tail == "":
                return self.reply_json(eval_detail(eval_dir))
            if tail == "file":
                rel = urllib.parse.parse_qs(query).get("path", [""])[0]
                return self.serve_file(eval_dir, rel)
            if tail == "runs":
                return self.reply_json(collect_eval(eval_dir)["rows"])
            if tail == "results":
                grader_name = urllib.parse.parse_qs(query).get("grader", ["default"])[0]
                return self.reply_json(eval_results(eval_dir, grader_name))
            self.reply_error(404, "not found")

        def serve_file(self, eval_dir, rel):
            target = resolve_eval_file(eval_dir, rel)
            if target is None:
                return self.reply_error(404, "no such file")
            content = target.read_bytes()
            self.reply_json(
                {
                    "content": content.decode("utf-8", errors="replace"),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "executable": os.access(target, os.X_OK),
                }
            )

        def handle_get_job(self, job_id):
            # Serialized under the jobs lock: a sweep thread mutates its
            # job dict concurrently with polls of it
            with jobs_lock:
                job = jobs.get(job_id)
                payload = None if job is None else json.dumps(job).encode()
            if payload is None:
                return self.reply_error(404, f"no such job: {job_id}")
            self.reply(200, payload, "application/json")

        # --- POST /api/evals: scaffold -------------------------------

        def handle_scaffold(self):
            body = self.read_json_body()
            if body is None:
                return
            parent_dir = root
            parent_rel = body.get("parent")
            if parent_rel:
                parent_dir = resolve_within(root, parent_rel)
                if parent_dir is None:
                    return self.reply_error(400, "parent escapes the studio root")
            if nesting_eval_dir(root, parent_dir) is not None:
                return self.reply_error(
                    400,
                    "parent is inside an existing eval - evals cannot nest "
                    "inside another eval or its runs/",
                )
            try:
                eval_dir = scaffold_eval(
                    parent_dir, body.get("name"), body.get("description", "")
                )
            except ValueError as ex:
                return self.reply_error(400, str(ex))
            # authoring.scaffold_slug (directory name) and cli.slugify (the
            # API slug, derived from the declared "name") can disagree on
            # mixed-case/spaced names - re-discover rather than assume, so
            # the slug returned is always one GET /api/evals/<slug> serves
            slug = next(
                (
                    s
                    for s, path in discover_slugs(root).items()
                    if path.resolve() == eval_dir.resolve()
                ),
                None,
            )
            self.reply_json({"slug": slug, "validation": validate_eval(eval_dir)}, 201)

        # --- PUT /api/evals/<slug>/file --------------------------------

        def handle_put_file(self, eval_dir):
            body = self.read_json_body()
            if body is None:
                return
            target = resolve_within(eval_dir, body.get("path"))
            if target is None:
                return self.reply_error(400, "path escapes the eval directory")
            if target.is_relative_to((eval_dir / "runs").resolve()):
                return self.reply_error(
                    403, "runs/ is immutable - Studio never writes there"
                )
            if target.is_dir():
                return self.reply_error(400, "path is a directory")
            content = body.get("content")
            if not isinstance(content, str):
                return self.reply_error(400, "content must be a string")

            current = target.read_bytes() if target.is_file() else b""
            current_sha = hashlib.sha256(current).hexdigest()
            if body.get("base_sha256") != current_sha:
                return self.reply_error(
                    409,
                    "file changed on disk since it was loaded",
                    theirs=current.decode("utf-8", errors="replace"),
                    ours=content,
                )

            was_executable = target.is_file() and os.access(target, os.X_OK)
            atomic_write(target, content.encode())
            executable = body.get("executable")
            if executable is None:
                executable = was_executable
            target.chmod(0o755 if executable else 0o644)

            self.reply_json(
                {
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "validation": validate_eval(eval_dir),
                }
            )

        # --- POST /api/evals/<slug>/run ---------------------------------

        def handle_run(self, slug, eval_dir):
            body = self.read_json_body()
            if body is None:
                return
            task_name, config_name, model = (
                body.get("task"),
                body.get("config"),
                body.get("model"),
            )

            config_path = eval_dir / "configs" / f"{config_name}.yaml"
            if not config_name or not config_path.is_file():
                return self.reply_error(400, f"no such config: {config_name}")
            config = load_yaml(config_path)
            runner = (config_path.parent / config["runner"]).resolve()
            if not (runner.is_file() and os.access(runner, os.X_OK)):
                return self.reply_error(
                    400, f"runner {runner} is not an executable file"
                )

            task_path = eval_dir / "tasks" / f"{task_name}.yaml"
            if not task_name or not task_path.is_file():
                return self.reply_error(400, f"no such task: {task_name}")
            task = load_yaml(task_path)

            model = model or config.get("model")
            if not model:
                return self.reply_error(400, "no model given and config has no default")

            with jobs_lock:
                conflict = self.slot_conflict(slug)
                if conflict:
                    return self.reply_error(409, conflict)
                job = {
                    "id": uuid.uuid4().hex,
                    "kind": "run",
                    "eval": slug,
                    "status": "queued",
                    "started": now_iso(),
                    "run_dir": None,
                    "error": None,
                    "log": [],
                }
                jobs[job["id"]] = job
                active_jobs[slug] = job["id"]

            threading.Thread(
                target=run_job,
                args=(job, slug, eval_dir / "runs", task, config_name, runner, model),
                daemon=True,
            ).start()
            self.reply_json(job, 202)

        def slot_conflict(self, slug):
            """The 409 message for a bench action on a busy eval, or None.
            Caller holds jobs_lock. The sweep frees the slot between its
            runs, so its message invites a retry - the spec's exact text."""
            holder = active_jobs.get(slug)
            if holder is None:
                return None
            if jobs[holder]["kind"] == "sweep":
                return sweep.SWEEP_HOLDS_EVAL_MESSAGE
            return f"a job is already active for {slug}"

        # --- POST /api/evals/<slug>/grade -------------------------------

        def handle_grade(self, slug, eval_dir):
            body = self.read_json_body()
            if body is None:
                return
            # Grading is synchronous and takes no slot itself, but never
            # runs against an eval a sweep is mid-run on
            with jobs_lock:
                holder = active_jobs.get(slug)
                if holder is not None and jobs[holder]["kind"] == "sweep":
                    return self.reply_error(409, sweep.SWEEP_HOLDS_EVAL_MESSAGE)
            run_dir = resolve_run_dir(eval_dir, body.get("run"))
            if run_dir is None:
                return self.reply_error(404, "no such run")
            if run_failed(load_yaml(run_dir / "run.yaml")):
                return self.reply_error(400, "cannot grade a failed run")

            grader_name = body.get("grader")
            grader_path = eval_dir / "graders" / f"{grader_name}.yaml"
            if not grader_name or not grader_path.is_file():
                return self.reply_error(404, f"no such grader: {grader_name}")

            grader = load_yaml(grader_path)
            grade_dir = run_dir / "grades" / grader_name
            self.reply_json(grade_run(run_dir, grade_dir, grader, grader_path))

        # --- POST /api/evals/<slug>/dryrun ------------------------------

        def handle_dryrun(self, eval_dir):
            body = self.read_json_body()
            if body is None:
                return
            run_dir = resolve_run_dir(eval_dir, body.get("run"))
            if run_dir is None:
                return self.reply_error(404, "no such run")
            try:
                grader = yaml.safe_load(body.get("grader_yaml") or "")
            except yaml.YAMLError as ex:
                return self.reply_error(400, f"invalid YAML: {ex}")
            if not isinstance(grader, dict) or not isinstance(
                grader.get("checks"), list
            ):
                return self.reply_error(400, "grader_yaml must have a checks list")

            task = load_yaml(run_dir / "run.yaml").get("task")
            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                try:
                    results, halted = run_checks(
                        run_dir, workspace, grader, eval_dir / "graders", task
                    )
                except (KeyError, TypeError, AttributeError) as ex:
                    return self.reply_error(400, f"invalid grader: {ex}")
                score, outcome = score_and_outcome(results, halted, grader)
                artifacts = sorted(p.name for p in workspace.iterdir() if p.is_file())

            self.reply_json(
                {
                    "checks": results,
                    "score": score,
                    "outcome": outcome,
                    "tags": sorted({t for r in results for t in r.get("tags", [])}),
                    "artifacts": artifacts,
                }
            )

        # --- /api/sweep: the one-at-a-time sweep orchestrator -----------

        def handle_get_sweep(self):
            "The active-or-most-recent sweep job, or {active: false}"
            with jobs_lock:
                job = sweep_state["job"]
                payload = json.dumps(job if job else {"active": False}).encode()
            self.reply(200, payload, "application/json")

        def handle_sweep_plan(self, query):
            """The compose-form preview: per-cell shortfall plus default
            grader treatments, for repeated query params - one value per
            param (?evals=a&evals=b&models=x&n=). evals defaults to all;
            models may be empty while the form is still being filled in.
            Values are never comma-split: percent-encoding makes a
            free-text model name containing a literal comma
            indistinguishable from an intended separator, so a CSV
            reading here would silently inflate the shortfall preview.
            """
            params = urllib.parse.parse_qs(query)

            def values(name):
                return [v for v in params.get(name, []) if v]

            body = {"models": values("models")}
            if values("evals"):
                body["evals"] = values("evals")
            if params.get("n"):
                try:
                    body["n"] = int(params["n"][0])
                except ValueError:
                    return self.reply_error(400, "n must be an integer")
            evals = discover_slugs(root)
            try:
                spec = sweep.validate_spec(body, evals, require_models=False)
            except ValueError as ex:
                return self.reply_error(400, str(ex))
            self.reply_json(sweep.plan_preview(spec, evals))

        def handle_post_sweep(self):
            body = self.read_json_body()
            if body is None:
                return
            evals = discover_slugs(root)
            try:
                spec = sweep.validate_spec(body, evals)
            except ValueError as ex:
                return self.reply_error(400, str(ex))

            # Building the job reads runs/ off disk - do it outside the
            # lock, then claim the sweep slot re-checking for a racing POST
            job = sweep.build_job(uuid.uuid4().hex, spec, evals, now_iso())
            cancel = threading.Event()
            with jobs_lock:
                active = sweep_state["job"]
                if active is not None and active["status"] in ("queued", "running"):
                    return self.reply_error(409, sweep.SWEEP_ACTIVE_MESSAGE)
                jobs[job["id"]] = job
                sweep_state["job"] = job
                sweep_state["cancel"] = cancel
                payload = json.dumps(job).encode()

            def acquire(slug):
                # Wait politely for a bench job to free the eval's slot;
                # a cancel request also ends the wait
                while not cancel.is_set():
                    with jobs_lock:
                        if slug not in active_jobs:
                            active_jobs[slug] = job["id"]
                            return True
                    time.sleep(0.5)
                return False

            def release(slug):
                with jobs_lock:
                    if active_jobs.get(slug) == job["id"]:
                        del active_jobs[slug]

            hooks = SimpleNamespace(
                lock=jobs_lock, cancel=cancel, acquire=acquire, release=release
            )
            threading.Thread(
                target=sweep.run_sweep, args=(job, spec, evals, hooks), daemon=True
            ).start()
            self.reply(202, payload, "application/json")

        def handle_cancel_sweep(self):
            body = self.read_json_body()
            if body is None:
                return
            with jobs_lock:
                job = sweep_state["job"]
                if job is None:
                    return self.reply_error(404, "no sweep to cancel")
                job["cancel_requested"] = True
                cancel = sweep_state["cancel"]
                payload = json.dumps(job).encode()
            # Idempotent: cancelling a finished sweep changes nothing
            if cancel is not None:
                cancel.set()
            self.reply(200, payload, "application/json")

        def reply_json(self, data, status=200):
            self.reply(status, json.dumps(data).encode(), "application/json")

        def reply_error(self, status, message, **extra):
            payload = {"error": message} | extra
            self.reply(status, json.dumps(payload).encode(), "application/json")

        def reply(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()
