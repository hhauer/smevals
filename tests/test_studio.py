"""Tests for smevals.studio: the interactive authoring server's read APIs.

Endpoint tests run a live server on an ephemeral port over a tmp suite of
two scaffold_eval-created Evals - real files, real HTTP, no mocks, the
repo's usual style for studio.py's sibling site.py.
"""

import hashlib
import http.client
import json
import pathlib
import socket
import subprocess
import threading
import time

import pytest
import yaml

from smevals import studio
from smevals.authoring import scaffold_eval

REPO_ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture
def suite(tmp_path):
    """A root dir holding two scaffold_eval-created Evals.

    Names are already directory-safe and lowercase: scaffold_eval's
    directory slug lowercases, while the eval.yaml "name" field (which
    studio's slug is derived from, matching cli.py's serve/build) does
    not - names with spaces or capitals would slug to something other
    than the scaffolded directory name. Sidestepping that mismatch here
    since resolving it is outside this task's scope.
    """
    first = scaffold_eval(tmp_path, "first-eval", "Measures the first thing")
    second = scaffold_eval(tmp_path, "second-eval", "Measures the second thing")
    return tmp_path, first, second


@pytest.fixture
def server(suite):
    root, first, second = suite
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(target=studio.run_studio, args=(root, port), daemon=True).start()

    def get(path):
        for attempt in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path)
                response = conn.getresponse()
                return (
                    response.status,
                    response.getheader("Content-Type"),
                    response.read(),
                )
            except ConnectionRefusedError:
                time.sleep(0.05)
        raise AssertionError("server never came up")

    return get, root, first, second


# --- GET / -----------------------------------------------------------------


def test_serves_studio_html(server):
    get, *_ = server
    status, ctype, body = get("/")
    assert status == 200
    assert ctype == "text/html"
    assert body.decode() == studio.studio_html()


# --- GET /api/evals ----------------------------------------------------------


def test_discovery_lists_both_evals(server):
    get, *_ = server
    status, ctype, body = get("/api/evals")
    assert status == 200
    assert ctype == "application/json"
    entries = json.loads(body)
    by_slug = {e["slug"]: e for e in entries}
    assert set(by_slug) == {"first-eval", "second-eval"}

    entry = by_slug["first-eval"]
    assert entry["name"] == "first-eval"
    assert entry["description"] == "Measures the first thing"
    assert entry["counts"] == {"tasks": 1, "configs": 1, "graders": 1}
    assert entry["last_run_iso"] is None
    assert entry["problems"] == 0


def test_discovery_counts_problems(server):
    get, root, first, second = server
    # Break the second eval's grader: no checks is a validation problem
    (second / "graders" / "default.yaml").write_text(
        yaml.safe_dump({"name": "default"})
    )

    entries = {e["slug"]: e for e in json.loads(get("/api/evals")[2])}
    assert entries["second-eval"]["problems"] == 1
    assert entries["first-eval"]["problems"] == 0


def test_discovery_reports_last_run(server):
    get, root, first, second = server
    run_dir = (
        first / "runs" / "example" / "default" / "gpt-4.1-mini" / "2026-01-01T00-00-00Z"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "run.yaml").write_text(
        yaml.safe_dump(
            {
                "task": {"name": "example"},
                "started": "2026-01-01T00:00:00+00:00",
                "exit_code": 0,
            }
        )
    )

    entries = {e["slug"]: e for e in json.loads(get("/api/evals")[2])}
    assert entries["first-eval"]["last_run_iso"] == "2026-01-01T00:00:00+00:00"
    assert entries["second-eval"]["last_run_iso"] is None


# --- GET /api/evals/<slug> ---------------------------------------------------


def test_eval_detail_tree_and_validation(server):
    get, root, first, second = server
    status, ctype, body = get("/api/evals/first-eval")
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert data["validation"] == []

    files = data["files"]
    assert {f["path"] for f in files["tasks"]} == {"tasks/example.yaml"}
    assert {f["path"] for f in files["configs"]} == {"configs/default.yaml"}
    assert {f["path"] for f in files["graders"]} == {"graders/default.yaml"}
    assert files["checkers"] == []
    other_paths = {f["path"] for f in files["other"]}
    assert {"eval.yaml", "run-llm", ".gitignore"} <= other_paths
    # runs/ is immutable and never listed among an eval's authorable files
    assert not any(p.startswith("runs/") for p in other_paths)


def test_eval_detail_groups_checkers_by_convention(server):
    get, root, first, second = server
    checker = first / "checkers" / "custom-check"
    checker.parent.mkdir()
    checker.write_text("#!/bin/sh\nexit 0\n")
    checker.chmod(0o755)

    data = json.loads(get("/api/evals/first-eval")[2])
    assert {f["path"] for f in data["files"]["checkers"]} == {"checkers/custom-check"}


def test_eval_detail_reports_validation_problems(server):
    get, root, first, second = server
    (first / "tasks" / "example.yaml").write_text("foo: [1, 2\n")  # invalid YAML

    data = json.loads(get("/api/evals/first-eval")[2])
    assert any(p["file"] == "tasks/example.yaml" for p in data["validation"])


def test_eval_detail_unknown_slug_is_404(server):
    get, *_ = server
    status, ctype, body = get("/api/evals/nope")
    assert status == 404
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- GET /api/evals/<slug>/file ----------------------------------------------


def test_file_read_returns_content_hash_and_executable(server):
    get, root, first, second = server
    status, ctype, body = get("/api/evals/first-eval/file?path=eval.yaml")
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    content = (first / "eval.yaml").read_text()
    assert data["content"] == content
    assert data["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert data["executable"] is False


def test_file_read_reports_executable_bit(server):
    get, root, first, second = server
    data = json.loads(get("/api/evals/first-eval/file?path=run-llm")[2])
    assert data["executable"] is True


def test_file_read_unknown_path_is_404(server):
    get, *_ = server
    status, ctype, body = get("/api/evals/first-eval/file?path=nope.yaml")
    assert status == 404
    assert "error" in json.loads(body)


def test_file_read_missing_path_param_is_404(server):
    get, *_ = server
    status, _, _ = get("/api/evals/first-eval/file")
    assert status == 404


def test_file_read_unknown_eval_is_404(server):
    get, *_ = server
    status, _, _ = get("/api/evals/nope/file?path=eval.yaml")
    assert status == 404


def test_file_read_rejects_dotdot_traversal(server):
    get, root, first, second = server
    (root / "secret.txt").write_text("do not serve me")
    status, _, _ = get("/api/evals/first-eval/file?path=../secret.txt")
    assert status == 404


def test_file_read_rejects_absolute_path(server):
    get, root, first, second = server
    secret = root / "abs-secret.txt"
    secret.write_text("do not serve me")
    status, _, _ = get(f"/api/evals/first-eval/file?path={secret}")
    assert status == 404


def test_file_read_rejects_symlink_escape(server):
    get, root, first, second = server
    outside = root / "outside.txt"
    outside.write_text("do not serve me")
    (first / "escape-link").symlink_to(outside)
    status, _, _ = get("/api/evals/first-eval/file?path=escape-link")
    assert status == 404


# --- misc routing ------------------------------------------------------------


def test_unknown_route_is_404_json(server):
    get, *_ = server
    status, ctype, body = get("/api/bogus")
    assert status == 404
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- CLI ---------------------------------------------------------------------


def test_cli_studio_help():
    result = subprocess.run(
        ["uv", "run", "smevals", "studio", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: smevals studio" in result.stdout
    assert "127.0.0.1" in result.stdout
    assert "--port" in result.stdout
