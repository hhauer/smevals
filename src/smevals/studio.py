"""smevals studio: a local, read-write authoring environment for Evals.

`run_studio(root, port)` serves the single-page app plus a JSON API over
every Eval discovered under root (Suite semantics identical to serve's
discovery - see cli.discover_evals). Binds 127.0.0.1 only: studio writes
files and executes runners, so it must never be exposed on the network.

This module carries the read APIs only (Task 2 of the studio plan): the
shelf listing, one Eval's file tree + validation, and guarded file reads.
Write/run/grade endpoints come in later tasks.
"""

import hashlib
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from .authoring import validate_eval
from .cli import discover_evals, load_eval, slugify
from .site import cached_yaml

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


def last_run_iso(eval_path):
    "The most recent Run's started timestamp, or None if there are no Runs"
    runs_root = eval_path / "runs"
    if not runs_root.exists():
        return None
    started = [
        cached_yaml(run_file).get("started") for run_file in runs_root.rglob("run.yaml")
    ]
    started = [s for s in started if s]
    return max(started) if started else None


def eval_summary(slug, eval_path):
    "The /api/evals entry for one Eval"
    doc = cached_yaml(eval_path / "eval.yaml") or {}
    return {
        "slug": slug,
        "name": doc.get("name") or eval_path.name,
        "description": doc.get("description", ""),
        "counts": {
            kind: len(list((eval_path / kind).glob("*.yaml")))
            for kind in ("tasks", "configs", "graders")
        },
        "last_run_iso": last_run_iso(eval_path),
        "problems": len(validate_eval(eval_path)),
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


def resolve_eval_file(eval_dir, rel):
    """Resolve a path relative to an Eval dir, guarding against traversal.

    Rejects ../ escapes, absolute paths and symlinks resolving outside the
    Eval dir - same is_relative_to containment check serve's site.py uses,
    which (unlike a string-prefix check) can't be fooled by a sibling
    directory whose name merely starts with the same prefix. A path
    carrying an embedded null byte makes Path.resolve() raise instead of
    returning - that is not-found too, not a server error.
    """
    if not rel:
        return None
    eval_root = eval_dir.resolve()
    try:
        target = (eval_dir / rel).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(eval_root) or not target.is_file():
        return None
    return target


def run_studio(root, port):
    "Serve smevals Studio over every Eval discovered under root"
    root = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/":
                return self.reply(200, studio_html().encode(), "text/html")
            if parts.path == "/api/evals":
                return self.reply_json(
                    [
                        eval_summary(slug, eval_path)
                        for slug, eval_path in sorted(discover_slugs(root).items())
                    ]
                )
            if parts.path.startswith("/api/evals/"):
                return self.serve_eval_api(
                    parts.path.removeprefix("/api/evals/"), parts.query
                )
            self.reply_error(404, "not found")

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

        def reply_json(self, data):
            self.reply(200, json.dumps(data).encode(), "application/json")

        def reply_error(self, status, message):
            self.reply(
                status, json.dumps({"error": message}).encode(), "application/json"
            )

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
