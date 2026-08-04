"""Tests for smevals.studio: the interactive authoring server's read APIs.

Endpoint tests run a live server on an ephemeral port over a tmp suite of
two scaffold_eval-created Evals - real files, real HTTP, no mocks, the
repo's usual style for studio.py's sibling site.py.
"""

import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

import pytest
import yaml

from conftest import (
    lms_calls,
    python_script,
    read_yaml,
    run_dirs,
    write_executable,
    write_grade,
    write_run,
)
from smevals import site, studio
from smevals.sweep import SWEEP_ACTIVE_MESSAGE, SWEEP_HOLDS_EVAL_MESSAGE, cell_key
from smevals.authoring import FILE_SCHEMAS, scaffold_eval
from smevals.cli import scalar_env_vars

requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires node")

REPO_ROOT = pathlib.Path(__file__).parent.parent

# A stub Runner: writes canned output plus a log file, per the Runner
# contract - a real executable, not a mock of run execution.
STUB_RUNNER = python_script("""\
    import os, pathlib
    print("STUB OUTPUT")
    pathlib.Path(os.environ["SMEVALS_RUN_DIR"], "stub.log").write_text("ran\\n")
    """)

# A slow variant, so a test can observe a job still queued/running before
# it completes (for the active-job-conflict case)
SLOW_RUNNER = python_script("""\
    import time
    time.sleep(0.4)
    print("STUB OUTPUT")
    """)

FAILING_RUNNER = python_script("import sys\nsys.exit(3)\n")


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
    token = studio.generate_token()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(
        target=studio.run_studio, args=(root, port, token), daemon=True
    ).start()

    def get(path, method="GET", body=None, headers=None):
        """GET by default; pass method=/body= for POST and PUT requests.

        Every call carries the server's real X-Studio-Key by default, so
        every existing test exercises the authorized path without having
        to know about auth. headers overrides/extends that default set
        for one call; a value of None for a key omits that header
        entirely (e.g. to probe a request sent with no Content-Type or
        no X-Studio-Key).
        """
        data = json.dumps(body).encode() if body is not None else None
        req_headers = {"X-Studio-Key": token}
        if data is not None:
            req_headers["Content-Type"] = "application/json"
        for key, value in (headers or {}).items():
            if value is None:
                req_headers.pop(key, None)
            else:
                req_headers[key] = value
        for attempt in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, body=data, headers=req_headers)
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


def run_and_wait(
    get, slug, *, task="example", config="default", model="stub-model", timeout=5.0
):
    "POST a run job and poll /api/jobs/<id> until it leaves queued/running"
    status, _, body = get(
        f"/api/evals/{slug}/run",
        method="POST",
        body={"task": task, "config": config, "model": model},
    )
    assert status == 202, body
    job = json.loads(body)
    deadline = time.monotonic() + timeout
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, f"job never finished: {job}"
        time.sleep(0.02)
        _, _, body = get(f"/api/jobs/{job['id']}")
        job = json.loads(body)
    return job


# --- GET / -----------------------------------------------------------------


def test_serves_studio_html(server):
    get, *_ = server
    status, ctype, body = get("/")
    assert status == 200
    assert ctype == "text/html"
    assert body.decode() == studio.studio_html()


def test_studio_html_calls_only_routes_studio_py_serves(server):
    """Every /api/... literal in studio.html's JS is a route studio.py handles.

    A cross-grep, not a router simulation: the JS builds paths like
    `/api/evals/${enc(slug)}/file`, so each literal fragment is matched
    against the /api/... string literals in studio.py's dispatch code. A
    fragment ending at an interpolation (a trailing "/") matches any
    studio.py route under that prefix; anything else must match a route
    exactly. Serving is asserted live too, so a bundling regression (the
    packaged file missing, say) fails here rather than only in the browser.
    """
    get, *_ = server
    status, _, body = get("/")
    assert status == 200
    html = body.decode()

    js_fragments = set(re.findall(r"/api/[A-Za-z0-9_./-]*", html))
    assert js_fragments, "studio.html should call the API"

    studio_py = pathlib.Path(studio.__file__).read_text()
    routes = set(re.findall(r'"(/api/[A-Za-z0-9_./-]*)"', studio_py))
    assert routes, "studio.py should declare /api/ routes as string literals"

    for fragment in sorted(js_fragments):
        if fragment.endswith("/"):
            ok = any(route.startswith(fragment) for route in routes)
        else:
            ok = fragment in routes
        assert ok, f"studio.html calls {fragment!r}, not a route in studio.py"


def test_studio_html_interpolated_tails_match_studio_py_routes():
    """Route tails after a ${...} interpolation are invisible to the
    cross-grep above (the /api/... literal ends at the interpolation), so
    the tails of /api/evals/${slug}/<tail> calls get their own check
    against the tail-dispatch literals in studio.py."""
    html = studio.studio_html()
    tails = set(re.findall(r"/api/evals/\$\{[^}]*\}/([A-Za-z0-9_]+)", html))
    assert tails, "studio.html should make nested /api/evals/<slug>/... calls"
    # The bench (Task 6) must actually wire its run/grade/dry-run loop; the
    # per-eval Results tab (Task 2) must call the Results-tab document
    assert {"file", "runs", "run", "grade", "dryrun", "results"} <= tails

    studio_py = pathlib.Path(studio.__file__).read_text()
    served = set(re.findall(r'tail == "([A-Za-z0-9_]+)"', studio_py))
    assert served, "studio.py should dispatch on tail literals"
    assert tails <= served, f"studio.html calls unserved tails: {tails - served}"


def test_studio_html_wires_results_routes():
    """Task 2: the global matrix (#/results) and the per-eval Results tab
    (#/eval/<slug>/results) are wired into the router and call Task 1's
    read endpoints - one aggregation path, never a second one in the JS."""
    html = studio.studio_html()

    # the global matrix: a nav link, a route handler, and the endpoint call
    assert "#/results" in html
    assert "renderResultsMatrix" in html
    assert 'api("/api/results")' in html

    # the per-eval tab: routed as a workbench surface, calling eval_results
    assert "results: true" in html
    assert "/api/evals/${enc(wb.slug)}/results" in html


def test_studio_html_wires_sweep_routes():
    """Task 4: the Sweeps surface (#/sweep) is wired into the router and
    calls Task 3's orchestrator endpoints - compose via the plan preview,
    start/poll/cancel via /api/sweep, models via /api/models."""
    html = studio.studio_html()

    # the surface: a nav link and a route handler
    assert "#/sweep" in html
    assert "renderSweep" in html

    # the orchestrator endpoints
    assert 'api("/api/sweep")' in html
    assert "/api/sweep/plan?" in html
    assert 'api("/api/sweep/cancel"' in html
    assert 'api("/api/models")' in html

    # the sweep poll flips the results ticker's gate - live results mid-sweep
    assert "state.sweepActive =" in html

    # composition survives a studio restart (localStorage + the exact hint)
    assert "smevals-sweep-compose" in html
    assert "already-recorded runs count" in html

    # the honest cancel label
    assert "stops after the current run" in html


# --- GET /api/schemas --------------------------------------------------------


def test_schemas_endpoint_serves_file_schemas(server):
    # The form view is generated from FILE_SCHEMAS; the endpoint must hand
    # the frontend exactly what authoring.py declares, not a copy
    get, *_ = server
    status, ctype, body = get("/api/schemas")
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == FILE_SCHEMAS


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


def test_discovery_disambiguates_duplicate_names(server):
    # Two Evals that declare the identical name would slug identically -
    # resolve_eval_slugs (serve/build) fails loud on this; Studio is a
    # live UI where silently dropping one from the shelf is worse, so it
    # disambiguates instead. discover_evals visits "first-eval" before
    # "second-eval" (sorted iteration), so the second collider gets -2.
    get, root, first, second = server
    (first / "eval.yaml").write_text(
        yaml.safe_dump({"name": "dup", "description": "one"})
    )
    (second / "eval.yaml").write_text(
        yaml.safe_dump({"name": "dup", "description": "two"})
    )

    entries = {e["slug"]: e for e in json.loads(get("/api/evals")[2])}
    assert set(entries) == {"dup", "dup-2"}
    assert entries["dup"]["description"] == "one"
    assert entries["dup-2"]["description"] == "two"

    # Both Evals stay reachable at their disambiguated slug
    status, _, _ = get("/api/evals/dup")
    assert status == 200
    status, _, _ = get("/api/evals/dup-2")
    assert status == 200


def test_discovery_reports_runs_counts(server):
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-1"), grader_doc, score=1.0)
    write_run(first / "runs", model="m-1", exit_code=1, output="")

    entries = {e["slug"]: e for e in json.loads(get("/api/evals")[2])}
    assert entries["first-eval"]["runs"] == {"total": 2, "failed": 1}
    assert entries["second-eval"]["runs"] == {"total": 0, "failed": 0}


def test_discovery_reports_best_from_default_grader(server):
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-good"), grader_doc, score=1.0)
    write_grade(write_run(first / "runs", model="m-bad"), grader_doc, score=0.2)

    entries = {e["slug"]: e for e in json.loads(get("/api/evals")[2])}
    assert entries["first-eval"]["best"] == {
        "config": "default",
        "model": "m-good",
        "score": 1.0,
        "runs": 1,
    }
    assert entries["second-eval"]["best"] is None


def test_discovery_best_matches_site_eval_summary(server):
    # The shelf strip's number must agree with serve's own index summary
    # for the same data - "best" is not a second, forked computation
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-good"), grader_doc, score=1.0)
    write_grade(write_run(first / "runs", model="m-bad"), grader_doc, score=0.2)

    entries = {e["slug"]: e for e in json.loads(get("/api/evals")[2])}
    expected = site.eval_summary("first-eval", site.collect_eval(first))["best"]
    assert entries["first-eval"]["best"] == expected


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


def test_file_read_rejects_null_byte_without_crashing(server):
    # A null byte makes Path.resolve() raise ValueError - must come back
    # as a clean 404, not an empty reply with a dead request thread
    get, *_ = server
    status, ctype, body = get("/api/evals/first-eval/file?path=eval.yaml%00.txt")
    assert status == 404
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- POST /api/evals (scaffold) ----------------------------------------------


def test_scaffold_returns_slug_that_get_serves(server):
    get, root, first, second = server
    status, ctype, body = get(
        "/api/evals",
        method="POST",
        body={"name": "third-eval", "description": "A third eval"},
    )
    assert status == 201
    assert ctype == "application/json"
    data = json.loads(body)
    assert data["slug"] == "third-eval"
    assert data["validation"] == []

    status, _, body = get(f"/api/evals/{data['slug']}")
    assert status == 200
    entry = json.loads(body)
    assert {f["path"] for f in entry["files"]["tasks"]} == {"tasks/example.yaml"}


def test_scaffold_mixed_case_name_returns_servable_slug(server):
    # authoring.scaffold_slug lowercases the new Eval's directory ("My Eval"
    # -> my-eval/) but cli.slugify - which discover_slugs uses to compute
    # the API slug from the eval's declared "name" field - preserves case
    # ("My Eval" -> "My-Eval"). The two disagree, so the response must carry
    # whatever slug discover_slugs actually computes after scaffolding (the
    # API's own truth), not the directory name, or GET wouldn't find it.
    get, root, first, second = server
    status, _, body = get(
        "/api/evals",
        method="POST",
        body={"name": "My Eval", "description": "Mixed case"},
    )
    assert status == 201
    slug = json.loads(body)["slug"]

    status, _, body = get(f"/api/evals/{slug}")
    assert status == 200
    assert {f["path"] for f in json.loads(body)["files"]["tasks"]} == {
        "tasks/example.yaml"
    }


def test_scaffold_missing_name_is_400(server):
    get, *_ = server
    status, ctype, body = get("/api/evals", method="POST", body={"description": "x"})
    assert status == 400
    assert "error" in json.loads(body)


def test_scaffold_duplicate_directory_is_400(server):
    get, *_ = server
    status, _, body = get(
        "/api/evals", method="POST", body={"name": "first-eval", "description": "dup"}
    )
    assert status == 400
    assert "error" in json.loads(body)


def test_scaffold_rejects_parent_inside_an_evals_runs_dir(server):
    # runs/ is immutable and never a valid home for a new Eval; scaffolding
    # there would also make the new Eval undiscoverable (nested inside a
    # dir discover_evals already treats as one Eval)
    get, root, first, second = server
    status, ctype, body = get(
        "/api/evals",
        method="POST",
        body={
            "name": "nested",
            "description": "x",
            "parent": "first-eval/runs/example/default/m/2026-01-01T00-00-00Z",
        },
    )
    assert status == 400
    assert ctype == "application/json"
    assert "error" in json.loads(body)
    assert not any(root.rglob("nested"))


def test_scaffold_rejects_parent_inside_an_eval_root(server):
    get, root, first, second = server
    status, _, body = get(
        "/api/evals",
        method="POST",
        body={"name": "nested", "description": "x", "parent": "first-eval"},
    )
    assert status == 400
    assert "error" in json.loads(body)
    assert not (first / "nested").exists()


def test_scaffold_rejects_parent_inside_an_eval_subdirectory(server):
    get, root, first, second = server
    status, _, body = get(
        "/api/evals",
        method="POST",
        body={"name": "nested", "description": "x", "parent": "first-eval/tasks"},
    )
    assert status == 400
    assert "error" in json.loads(body)
    assert not (first / "tasks" / "nested").exists()


def test_scaffold_parent_at_a_legitimate_suite_subdirectory_still_works(server):
    # A plain subfolder of the studio root that is not itself (or inside)
    # an Eval - e.g. a suite grouping folder - stays a legal scaffold target
    get, root, first, second = server
    (root / "group").mkdir()
    status, _, body = get(
        "/api/evals",
        method="POST",
        body={"name": "grouped-eval", "description": "x", "parent": "group"},
    )
    assert status == 201, body
    data = json.loads(body)
    assert data["slug"] == "grouped-eval"
    assert (root / "group" / "grouped-eval" / "eval.yaml").is_file()


# --- PUT /api/evals/<slug>/file -----------------------------------------------


def test_put_file_roundtrip(server):
    get, root, first, second = server
    status, _, body = get("/api/evals/first-eval/file?path=eval.yaml")
    sha = json.loads(body)["sha256"]

    new_content = yaml.safe_dump({"name": "first-eval", "description": "updated"})
    status, ctype, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={"path": "eval.yaml", "content": new_content, "base_sha256": sha},
    )
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert data["sha256"] == hashlib.sha256(new_content.encode()).hexdigest()
    assert data["validation"] == []
    assert (first / "eval.yaml").read_text() == new_content
    # No leftover tmp file from the atomic write
    assert not list(first.rglob("*.tmp"))


def test_put_file_creates_new_file(server):
    get, root, first, second = server
    empty_sha = hashlib.sha256(b"").hexdigest()
    content = yaml.safe_dump({"name": "second", "prompt": "hi"})
    status, _, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={
            "path": "tasks/second.yaml",
            "content": content,
            "base_sha256": empty_sha,
        },
    )
    assert status == 200
    assert (first / "tasks" / "second.yaml").read_text() == content


def test_put_file_conflict_returns_theirs_and_ours(server):
    get, root, first, second = server
    status, _, body = get("/api/evals/first-eval/file?path=eval.yaml")
    stale_sha = json.loads(body)["sha256"]

    # Someone else (Jesse's own editor) changes the file on disk first
    external_content = yaml.safe_dump(
        {"name": "first-eval", "description": "external edit"}
    )
    (first / "eval.yaml").write_text(external_content)

    our_content = yaml.safe_dump({"name": "first-eval", "description": "studio edit"})
    status, ctype, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={"path": "eval.yaml", "content": our_content, "base_sha256": stale_sha},
    )
    assert status == 409
    assert ctype == "application/json"
    data = json.loads(body)
    assert "error" in data
    assert data["theirs"] == external_content
    assert data["ours"] == our_content
    # The conflicting write never lands
    assert (first / "eval.yaml").read_text() == external_content


def test_put_file_rejects_traversal(server):
    get, root, first, second = server
    status, ctype, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={"path": "../escape.txt", "content": "pwned", "base_sha256": ""},
    )
    assert status == 400
    assert "error" in json.loads(body)
    assert not (root / "escape.txt").exists()


def test_put_file_rejects_writes_under_runs(server):
    # Runs stay immutable (spec's named invariant): PUT must not be a back
    # door into runs/, even with the correct base_sha256 for the real file
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    job = run_and_wait(get, "first-eval")
    run_file = first / "runs" / job["run_dir"] / "run.yaml"
    original = run_file.read_text()
    sha = hashlib.sha256(original.encode()).hexdigest()

    status, ctype, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={
            "path": f"runs/{job['run_dir']}/run.yaml",
            "content": "tampered: true\n",
            "base_sha256": sha,
        },
    )
    assert status == 403
    assert ctype == "application/json"
    assert "error" in json.loads(body)
    assert run_file.read_text() == original


def test_put_file_onto_a_directory_is_400(server):
    get, root, first, second = server
    empty_sha = hashlib.sha256(b"").hexdigest()
    status, ctype, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={"path": "tasks", "content": "oops", "base_sha256": empty_sha},
    )
    assert status == 400
    assert ctype == "application/json"
    assert "error" in json.loads(body)


def test_put_file_sets_executable_bit(server):
    get, root, first, second = server
    status, _, body = get("/api/evals/first-eval/file?path=run-llm")
    sha = json.loads(body)["sha256"]
    status, _, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={
            "path": "run-llm",
            "content": "#!/bin/sh\necho hi\n",
            "base_sha256": sha,
            "executable": False,
        },
    )
    assert status == 200
    assert not os.access(first / "run-llm", os.X_OK)


def test_put_file_preserves_executable_bit_by_default(server):
    get, root, first, second = server
    status, _, body = get("/api/evals/first-eval/file?path=run-llm")
    sha = json.loads(body)["sha256"]
    status, _, body = get(
        "/api/evals/first-eval/file",
        method="PUT",
        body={"path": "run-llm", "content": "#!/bin/sh\necho hi\n", "base_sha256": sha},
    )
    assert status == 200
    assert os.access(first / "run-llm", os.X_OK)


# --- POST /api/evals/<slug>/run -----------------------------------------------


def test_run_job_lifecycle_to_done(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)

    job = run_and_wait(get, "first-eval")
    assert job["status"] == "done"
    assert job["kind"] == "run"
    assert job["run_dir"]

    run_dir = first / "runs" / job["run_dir"]
    assert (run_dir / "run.yaml").exists()
    assert (run_dir / "output.txt").read_text() == "STUB OUTPUT\n"
    assert (run_dir / "stub.log").read_text() == "ran\n"
    assert read_yaml(run_dir / "run.yaml")["config"]["model"] == "stub-model"


def test_run_unknown_config_is_400(server):
    get, *_ = server
    status, ctype, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "example", "config": "nope", "model": "m"},
    )
    assert status == 400
    assert "error" in json.loads(body)


def test_run_unknown_task_is_400(server):
    get, *_ = server
    status, _, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "nope", "config": "default", "model": "m"},
    )
    assert status == 400


def test_run_unknown_eval_is_404(server):
    get, *_ = server
    status, _, body = get(
        "/api/evals/nope/run",
        method="POST",
        body={"task": "example", "config": "default", "model": "m"},
    )
    assert status == 404


def test_run_active_job_conflict(server):
    get, root, first, second = server
    write_executable(first / "run-llm", SLOW_RUNNER)

    status, _, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "example", "config": "default", "model": "m"},
    )
    assert status == 202
    job = json.loads(body)

    status, ctype, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "example", "config": "default", "model": "m"},
    )
    assert status == 409
    assert ctype == "application/json"
    assert "error" in json.loads(body)

    # Drain the first job so its thread doesn't outlive the test
    deadline = time.monotonic() + 5
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, "job never finished"
        time.sleep(0.02)
        _, _, body = get(f"/api/jobs/{job['id']}")
        job = json.loads(body)
    assert job["status"] == "done"


def test_run_active_job_conflict_is_per_eval(server):
    get, root, first, second = server
    write_executable(first / "run-llm", SLOW_RUNNER)
    write_executable(second / "run-llm", STUB_RUNNER)

    status, _, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "example", "config": "default", "model": "m"},
    )
    assert status == 202
    first_job = json.loads(body)

    # second-eval isn't busy, even while first-eval has an active job
    second_job = run_and_wait(get, "second-eval")
    assert second_job["status"] == "done"

    deadline = time.monotonic() + 5
    while first_job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, "job never finished"
        time.sleep(0.02)
        _, _, body = get(f"/api/jobs/{first_job['id']}")
        first_job = json.loads(body)


def test_run_failed_runner_marks_job_failed(server):
    get, root, first, second = server
    write_executable(first / "run-llm", FAILING_RUNNER)
    job = run_and_wait(get, "first-eval")
    assert job["status"] == "failed"
    assert job["error"] == "runner exited non-zero"
    assert job["run_dir"]


# --- GET /api/jobs/<id> --------------------------------------------------------


def test_get_unknown_job_is_404(server):
    get, *_ = server
    status, ctype, body = get("/api/jobs/nope")
    assert status == 404
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- POST /api/evals/<slug>/grade ----------------------------------------------


def test_grade_endpoint_persists_grade(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    job = run_and_wait(get, "first-eval")
    run_rel = job["run_dir"]

    status, ctype, body = get(
        "/api/evals/first-eval/grade",
        method="POST",
        body={"run": run_rel, "grader": "default"},
    )
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert data["outcome"] == "pass"  # scaffold's default checker is contains:""

    grade_file = first / "runs" / run_rel / "grades" / "default" / "grade.yaml"
    assert grade_file.exists()
    assert read_yaml(grade_file)["outcome"] == "pass"


def test_grade_unknown_run_is_404(server):
    get, *_ = server
    status, _, body = get(
        "/api/evals/first-eval/grade",
        method="POST",
        body={"run": "nope/nope/nope/nope", "grader": "default"},
    )
    assert status == 404
    assert "error" in json.loads(body)


def test_grade_unknown_grader_is_404(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    job = run_and_wait(get, "first-eval")
    status, _, body = get(
        "/api/evals/first-eval/grade",
        method="POST",
        body={"run": job["run_dir"], "grader": "nope"},
    )
    assert status == 404


def test_grade_failed_run_is_400(server):
    get, root, first, second = server
    write_executable(first / "run-llm", FAILING_RUNNER)
    job = run_and_wait(get, "first-eval")
    status, _, body = get(
        "/api/evals/first-eval/grade",
        method="POST",
        body={"run": job["run_dir"], "grader": "default"},
    )
    assert status == 400
    assert "failed" in json.loads(body)["error"]


# --- POST /api/evals/<slug>/dryrun ---------------------------------------------

PATH_RECORDER_CHECKER = python_script("""\
    import os, pathlib
    pathlib.Path(os.environ["SMEVALS_RUN_DIR"], "workspace-path.txt").write_text(
        str(pathlib.Path.cwd())
    )
    print("recorded")
    """)

WORKSPACE_WRITER_CHECKER = python_script("""\
    import pathlib
    pathlib.Path("scratch.txt").write_text("hi")
    """)


def test_dryrun_grades_without_persisting_and_cleans_temp_dir(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    write_executable(first / "checkers" / "recorder", PATH_RECORDER_CHECKER)
    write_executable(first / "checkers" / "writer", WORKSPACE_WRITER_CHECKER)
    job = run_and_wait(get, "first-eval")
    run_rel = job["run_dir"]
    run_dir = first / "runs" / run_rel

    # A grader that does NOT match what's saved on disk (graders/default.yaml)
    # - this must reflect the request body, not the persisted file
    grader_yaml = yaml.safe_dump(
        {
            "name": "scratch",
            "checks": [
                {"checker": "../checkers/writer"},
                {"checker": "../checkers/recorder"},
            ],
        }
    )
    status, ctype, body = get(
        "/api/evals/first-eval/dryrun",
        method="POST",
        body={"run": run_rel, "grader_yaml": grader_yaml},
    )
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert data["outcome"] == "pass"
    assert [c["ok"] for c in data["checks"]] == [True, True]
    assert data["artifacts"] == ["scratch.txt"]
    assert not (run_dir / "grades").exists()  # nothing persisted under the Run

    # The temp workspace the checkers ran in is gone once the request returned
    workspace_path = pathlib.Path((run_dir / "workspace-path.txt").read_text())
    assert not workspace_path.exists()


def test_dryrun_invalid_yaml_is_400(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    job = run_and_wait(get, "first-eval")
    status, _, body = get(
        "/api/evals/first-eval/dryrun",
        method="POST",
        body={"run": job["run_dir"], "grader_yaml": "checks: [unterminated"},
    )
    assert status == 400
    assert "error" in json.loads(body)


def test_dryrun_malformed_grader_is_400(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    job = run_and_wait(get, "first-eval")
    status, _, body = get(
        "/api/evals/first-eval/dryrun",
        method="POST",
        # checks entries missing the required "checker" key
        body={
            "run": job["run_dir"],
            "grader_yaml": "name: x\nchecks:\n  - value: hi\n",
        },
    )
    assert status == 400
    assert "error" in json.loads(body)


def test_dryrun_unknown_run_is_404(server):
    get, *_ = server
    status, _, body = get(
        "/api/evals/first-eval/dryrun",
        method="POST",
        body={"run": "nope", "grader_yaml": "name: x\nchecks: []"},
    )
    assert status == 404


# --- GET /api/evals/<slug>/runs ------------------------------------------------


def test_runs_listing_reuses_collect_eval(server):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    job = run_and_wait(get, "first-eval")
    get(
        "/api/evals/first-eval/grade",
        method="POST",
        body={"run": job["run_dir"], "grader": "default"},
    )

    status, ctype, body = get("/api/evals/first-eval/runs")
    assert status == 200
    assert ctype == "application/json"
    rows = json.loads(body)
    assert len(rows) == 1
    row = rows[0]
    assert row["task"] == "example"
    assert row["config"] == "default"
    assert row["model"] == "stub-model"
    assert row["exit_code"] == 0
    assert row["grades"]["default"]["outcome"] == "pass"


# --- GET /api/evals/<slug>/results ----------------------------------------------


def test_eval_results_endpoint_shape(server):
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-1"), grader_doc, score=1.0)
    failed = write_run(first / "runs", model="m-1", exit_code=1, output="")
    write_grade(failed, grader_doc, outcome="fail", score=0.0)

    status, ctype, body = get("/api/evals/first-eval/results")
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert data["grader"] == "default"
    assert data["graders"] == ["default"]
    assert data["total"] == 1
    assert data["excluded_failed"] == 1
    assert len(data["groups"]) == 1
    assert data["groups"][0]["model"] == "m-1"


def test_eval_results_endpoint_grader_query_param(server):
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-1"), grader_doc, score=1.0)

    status, _, body = get("/api/evals/first-eval/results?grader=default")
    assert status == 200
    assert json.loads(body)["grader"] == "default"


def test_eval_results_endpoint_falls_back_for_unknown_grader(server):
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-1"), grader_doc, score=1.0)

    status, _, body = get("/api/evals/first-eval/results?grader=nope")
    assert status == 200
    assert json.loads(body)["grader"] == "default"


def test_eval_results_endpoint_unknown_eval_is_404(server):
    get, *_ = server
    status, ctype, body = get("/api/evals/nope/results")
    assert status == 404
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- GET /api/results ------------------------------------------------------------


def test_results_matrix_endpoint(server):
    get, root, first, second = server
    grader_doc = read_yaml(first / "graders" / "default.yaml")
    write_grade(write_run(first / "runs", model="m-1"), grader_doc, score=1.0)

    status, ctype, body = get("/api/results")
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert set(data["evals"]) == {"first-eval", "second-eval"}
    assert data["matrix"]["m-1"]["first-eval"]["mean"] == 1.0
    assert "generated" in data


# --- GET /api/models ------------------------------------------------------------

# `llm models list` has no --json option (verified against the real llm
# 0.31.1 installed on this machine - `llm models list --json` exits 2,
# "No such option '--json'"). These lines are copied verbatim from that
# machine's real `llm models list` plain-text output, so the parser is
# tested against ground truth, not an invented shape.
FAKE_LLM = python_script("""\
    import sys
    if sys.argv[1:3] == ["models", "list"]:
        print("OpenAI Chat: gpt-4o (aliases: 4o)")
        print("OpenAI Chat: gpt-4o-audio-preview")
        print("OpenAI Chat: gpt-3.5-turbo (aliases: 3.5, chatgpt)")
        print(
            "OpenAI Completion: gpt-3.5-turbo-instruct "
            "(aliases: 3.5-instruct, chatgpt-instruct)"
        )
        print("Default: gpt-4o-mini")
    """)


def isolate_from_real_lms(monkeypatch, tmp_path):
    """Point HOME at a scratch dir: sweep.lms_path falls back to
    ~/.lmstudio/bin/lms, and /api/models must never touch a real LM
    Studio on the machine running the tests."""
    home = tmp_path / "no-lms-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def test_models_configs_only_fallback_without_llm_on_path(
    server, monkeypatch, tmp_path
):
    get, *_ = server
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_path_dir))
    isolate_from_real_lms(monkeypatch, tmp_path)

    status, ctype, body = get("/api/models")
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert set(data["models"]) == {
        "gpt-4.1-mini"
    }  # both scaffolded evals' config default
    assert data["local"] == []


def test_models_parses_real_llm_plain_text_format(server, monkeypatch, tmp_path):
    get, *_ = server
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "llm", FAKE_LLM)
    monkeypatch.setenv("PATH", str(bin_dir))
    isolate_from_real_lms(monkeypatch, tmp_path)

    status, _, body = get("/api/models")
    assert status == 200
    data = json.loads(body)
    assert {
        "gpt-4o",
        "gpt-4o-audio-preview",
        "gpt-3.5-turbo",
        "gpt-3.5-turbo-instruct",
        "gpt-4o-mini",
        "gpt-4.1-mini",  # from the scaffolded evals' configs
    } <= set(data["models"])
    # the " (aliases: ...)" suffix and its contents must not leak through
    assert "4o" not in data["models"]
    assert "3.5, chatgpt" not in data["models"]


# --- unhandled-exception safety net --------------------------------------

# Every API error must come back as JSON {error}, per the spec - never a
# dropped connection. These reproduce cases that previously crashed the
# request thread with no reply at all (RemoteDisconnected on the client).


def test_dryrun_missing_output_txt_returns_json_error(server):
    get, root, first, second = server
    # A hand-fabricated Run missing output.txt (not something execute_run
    # would ever produce) - the built-in "contains" checker reads
    # output.txt directly and would otherwise raise unhandled
    run_dir = first / "runs" / "example" / "default" / "m" / "2026-01-01T00-00-00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "run.yaml").write_text(
        yaml.safe_dump({"task": {"name": "example"}, "exit_code": 0})
    )
    grader_yaml = yaml.safe_dump(
        {"name": "x", "checks": [{"checker": "contains", "value": "hi"}]}
    )
    status, ctype, body = get(
        "/api/evals/first-eval/dryrun",
        method="POST",
        body={
            "run": "example/default/m/2026-01-01T00-00-00Z",
            "grader_yaml": grader_yaml,
        },
    )
    assert status == 500
    assert ctype == "application/json"
    assert "error" in json.loads(body)


def test_run_config_missing_runner_key_returns_json_error(server):
    get, root, first, second = server
    (first / "configs" / "broken.yaml").write_text(yaml.safe_dump({"name": "broken"}))
    status, ctype, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "example", "config": "broken", "model": "m"},
    )
    assert status == 500
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- Content-Type gate (CSRF hardening) --------------------------------------
#
# A simple HTML form cannot set Content-Type to application/json (only
# text/plain, application/x-www-form-urlencoded or multipart/form-data
# are form-submittable), so gating on it kills cross-origin CSRF via a
# plain <form>; a cross-origin fetch that lies and sends
# application/json anyway triggers a CORS preflight, which studio.py
# never answers (its 501 kills the real request before it's sent).


def test_write_endpoint_rejects_non_json_content_type(server):
    get, root, first, second = server
    status, ctype, body = get(
        "/api/evals",
        method="POST",
        body={"name": "third-eval", "description": "x"},
        headers={"Content-Type": "text/plain"},
    )
    assert status == 415
    assert ctype == "application/json"
    assert "error" in json.loads(body)
    # never scaffolded
    assert not (root / "third-eval").exists()


def test_write_endpoint_rejects_missing_content_type(server):
    get, *_ = server
    status, ctype, body = get(
        "/api/evals",
        method="POST",
        body={"name": "third-eval", "description": "x"},
        headers={"Content-Type": None},
    )
    assert status == 415
    assert ctype == "application/json"
    assert "error" in json.loads(body)


def test_write_endpoint_accepts_json_content_type_with_charset(server):
    # A real JSON POST may carry a charset parameter - the gate must
    # match on substring, not exact-equal
    get, *_ = server
    status, _, body = get(
        "/api/evals",
        method="POST",
        body={"name": "third-eval", "description": "x"},
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert status == 201, body


# --- X-Studio-Key auth --------------------------------------------------------
#
# Studio binds 127.0.0.1, but loopback is not private: a hostile page open
# in the same browser can still reach it (no SOP for cross-origin GET/simple
# POST, no auth cookie required). A per-run random token, out of band from
# cookies, closes that: every /api/* request must carry it.


def test_api_request_without_key_header_is_403(server):
    get, *_ = server
    status, ctype, body = get("/api/evals", headers={"X-Studio-Key": None})
    assert status == 403
    assert ctype == "application/json"
    assert "error" in json.loads(body)


def test_api_request_with_wrong_key_is_403(server):
    get, *_ = server
    status, ctype, body = get("/api/evals", headers={"X-Studio-Key": "not-the-token"})
    assert status == 403
    assert ctype == "application/json"
    assert "error" in json.loads(body)


def test_api_request_with_correct_key_is_200(server):
    get, *_ = server
    status, ctype, body = get("/api/evals")  # the fixture's default header
    assert status == 200
    assert ctype == "application/json"


def test_html_is_served_without_any_key(server):
    get, *_ = server
    status, ctype, body = get("/", headers={"X-Studio-Key": None})
    assert status == 200
    assert ctype == "text/html"


def test_generate_token_returns_distinct_urlsafe_strings():
    a, b = studio.generate_token(), studio.generate_token()
    assert a != b
    assert len(a) >= 16
    assert re.fullmatch(r"[A-Za-z0-9_-]+", a)


# --- misc routing ------------------------------------------------------------


def test_unknown_route_is_404_json(server):
    get, *_ = server
    status, ctype, body = get("/api/bogus")
    assert status == 404
    assert ctype == "application/json"
    assert "error" in json.loads(body)


# --- the pure YAML layer vs real PyYAML --------------------------------------
#
# studio.html's form view round-trips files through a hand-rolled YAML
# subset (the STUDIO_PURE block). Its contract: for every construct it
# accepts, its value and its emitted text must mean exactly what PyYAML
# (what the CLI reads) would say - anything it cannot promise that for
# must refuse the document into raw-only mode with a reason. The corpus
# in tests/studio_pure/cases.json attacks that contract; a MISMATCH is
# silent data corruption and fails this test.

STUDIO_PURE = REPO_ROOT / "tests" / "studio_pure"


def canon(node):
    "JS has one number type: integral floats compare equal to ints"
    if isinstance(node, float) and node.is_integer():
        return int(node)
    if isinstance(node, dict):
        return {k: canon(v) for k, v in node.items()}
    if isinstance(node, list):
        return [canon(v) for v in node]
    return node


@pytest.fixture(scope="module")
def corpus_report():
    result = subprocess.run(
        [
            "node",
            str(STUDIO_PURE / "run_corpus.mjs"),
            str(REPO_ROOT / "src" / "smevals" / "studio.html"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@requires_node
def test_studio_pure_corpus_no_silent_divergence_from_pyyaml(corpus_report):
    rows = corpus_report["cases"]
    assert len(rows) == 54
    failures = []
    for row in rows:
        name = row["name"]
        try:
            py_doc = yaml.safe_load(row["yaml"])
        except yaml.YAMLError:
            # PyYAML itself rejects it; the form view must not pretend to
            # understand what the CLI cannot read
            if row["jsOk"]:
                failures.append(f"{name}: JS accepted YAML PyYAML rejects")
            continue
        if not row["jsOk"]:
            if not row.get("jsError"):
                failures.append(f"{name}: refused without a reason")
            continue
        if canon(py_doc) != canon(row["jsDoc"]):
            failures.append(f"{name}: MISMATCH py={py_doc!r} js={row['jsDoc']!r}")
        if not (row.get("reparseOk") and row.get("reparseStable")):
            failures.append(f"{name}: emit->parse round trip drifted")
    assert not failures, "\n".join(failures)


@requires_node
def test_studio_pure_corpus_saves_preserve_untouched_fields(corpus_report):
    # The data-loss channel: edit an UNRELATED key via applyEdit, save, and
    # re-read with PyYAML - every field the user never touched must still
    # mean exactly what it did on disk.
    failures = []
    for row in corpus_report["cases"]:
        if not row.get("jsOk") or not row.get("editOk"):
            continue
        try:
            py_before = yaml.safe_load(row["yaml"])
        except yaml.YAMLError:
            continue
        py_after = yaml.safe_load(row["editedEmit"])
        if isinstance(py_after, dict):
            py_after = {
                k: v for k, v in py_after.items() if k != "__untouched_marker__"
            }
        if canon(py_before) != canon(py_after):
            failures.append(
                f"{row['name']}: DATA LOSS py(before)={py_before!r} "
                f"py(after unrelated edit+save)={py_after!r}"
            )
    assert not failures, "\n".join(failures)


@requires_node
def test_studio_pure_env_mirror_matches_python_str(corpus_report):
    # Finding: the env mirror must display what Python would receive -
    # str() of each scalar, including "1.0" for float-typed values and
    # True/False capitalization for bools.
    probe = corpus_report["envProbe"]
    assert probe["ok"], probe
    task = yaml.safe_load(probe["doc"])
    expected = scalar_env_vars("SMEVALS_TASK_", task)
    mirrored = {k: v for k, v in probe["vars"].items() if k.startswith("SMEVALS_TASK_")}
    assert mirrored == expected
    assert probe["vars"]["SMEVALS_TASK"] == str(task["name"])
    assert probe["vars"]["SMEVALS_PROMPT"] == str(task["prompt"])


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


# --- sweeps: /api/sweep, /api/sweep/plan, /api/sweep/cancel -------------------
#
# Lifecycle tests drive the real orchestrator: a live server, real stub
# runners, and a fake `lms` executable on PATH recording its argv (the
# fake_lms fixture also redirects HOME so sweep.lms_path can never fall
# back to the real ~/.lmstudio/bin/lms - these tests run on the machine
# that hosts the real local sweeps and must NEVER touch its LM Studio).

# Sleeps long enough to observe/interrupt a sweep mid-flight
SLOW_SWEEP_RUNNER = python_script("""\
    import time
    time.sleep(0.3)
    print("STUB OUTPUT")
    """)

# Emits a score so cell means are observable
SCORING_CHECKER = python_script("""\
    import json
    print(json.dumps({"score": 1.0}))
    """)


def order_logging_runner(log_path, label):
    "A stub runner appending '<label> <model>' per execution to a shared log"
    return python_script(f"""\
        import os
        with open({str(log_path)!r}, "a") as f:
            f.write("{label} " + os.environ["SMEVALS_MODEL"] + "\\n")
        print("STUB OUTPUT")
        """)


def post_sweep(get, spec, expect=202):
    status, _, body = get("/api/sweep", method="POST", body=spec)
    assert status == expect, body
    return json.loads(body)


def wait_sweep(get, timeout=30):
    "Poll GET /api/sweep until the job leaves queued/running"
    deadline = time.monotonic() + timeout
    while True:
        _, _, body = get("/api/sweep")
        job = json.loads(body)
        if job.get("status") not in ("queued", "running"):
            return job
        assert time.monotonic() < deadline, f"sweep never finished: {job}"
        time.sleep(0.05)


def test_sweep_get_initially_inactive(server):
    get, *_ = server
    status, ctype, body = get("/api/sweep")
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"active": False}


def test_sweep_endpoints_require_key(server):
    get, *_ = server
    status, _, _ = get("/api/sweep", headers={"X-Studio-Key": None})
    assert status == 403


def test_sweep_runs_eval_major_within_model_major(server, fake_lms, tmp_path):
    get, root, first, second = server
    order_log = tmp_path / "order.log"
    write_executable(first / "run-llm", order_logging_runner(order_log, "first-eval"))
    write_executable(second / "run-llm", order_logging_runner(order_log, "second-eval"))

    job = post_sweep(
        get,
        {
            "evals": ["first-eval", "second-eval"],
            "models": ["hosted-a", "hosted-b"],
            "n": 1,
        },
    )
    assert job["kind"] == "sweep"
    job = wait_sweep(get)
    assert job["status"] == "done", job
    assert order_log.read_text().splitlines() == [
        "first-eval hosted-a",
        "second-eval hosted-a",
        "first-eval hosted-b",
        "second-eval hosted-b",
    ]
    for slug in ("first-eval", "second-eval"):
        for model in ("hosted-a", "hosted-b"):
            cell = job["cells"][cell_key(slug, model)]
            assert cell["state"] == "done"
            assert cell["have"] == 1
            assert cell["done"] == 1
    # The sweep job is also a plain job: pollable at /api/jobs/<id>
    status, _, body = get(f"/api/jobs/{job['id']}")
    assert status == 200
    assert json.loads(body)["kind"] == "sweep"


def test_sweep_local_model_unloads_then_loads_with_context_length(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)

    post_sweep(
        get, {"evals": ["first-eval"], "models": ["local-alpha", "hosted-x"], "n": 1}
    )
    job = wait_sweep(get)
    assert job["status"] == "done", job
    # unload --all always precedes a local load; -c carries the spec'd
    # 32768 default; the hosted model contributes no lms calls; one final
    # unload precedes the deferred pass because a local model was loaded
    assert lms_calls(fake_lms) == [
        ["ls"],
        ["unload", "--all"],
        ["load", "local-alpha", "-c", "32768", "-y"],
        ["unload", "--all"],
    ]


def test_sweep_hosted_models_trigger_no_lms_load_or_unload(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)

    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-only"], "n": 1})
    job = wait_sweep(get)
    assert job["status"] == "done", job
    # Only the inventory probe - a hosted-only sweep never loads/unloads
    assert lms_calls(fake_lms) == [["ls"]]


def test_sweep_load_failure_marks_model_cells_failed_and_continues(
    server, fake_lms, monkeypatch
):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    monkeypatch.setenv("FAKE_LMS_FAIL_LOAD", "local-alpha")

    post_sweep(
        get, {"evals": ["first-eval"], "models": ["local-alpha", "hosted-b"], "n": 1}
    )
    job = wait_sweep(get)
    assert job["status"] == "done", job
    assert job["cells"][cell_key("first-eval", "local-alpha")]["state"] == "failed"
    assert job["cells"][cell_key("first-eval", "local-alpha")]["have"] == 0
    hosted = job["cells"][cell_key("first-eval", "hosted-b")]
    assert hosted["state"] == "done"
    assert hosted["have"] == 1
    assert any("model not found" in line for line in job["log"])


def test_sweep_grades_inline_per_run_and_updates_mean(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    write_executable(first / "checkers" / "score", SCORING_CHECKER)
    (first / "graders" / "default.yaml").write_text(
        yaml.safe_dump(
            {"name": "default", "checks": [{"checker": "../checkers/score"}]}
        )
    )

    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-a"], "n": 2})
    job = wait_sweep(get)
    assert job["status"] == "done", job
    dirs = run_dirs(first)
    assert len(dirs) == 2
    for run_dir in dirs:
        grade = read_yaml(run_dir / "grades" / "default" / "grade.yaml")
        assert grade["outcome"] == "pass"
        assert grade["score"] == 1.0
    cell = job["cells"][cell_key("first-eval", "hosted-a")]
    assert cell == {
        "target": 2,
        "have": 2,
        "failed": 0,
        "done": 2,
        "mean": 1.0,
        "state": "done",
    }


def test_sweep_grader_override_skip_writes_no_grades(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)

    post_sweep(
        get,
        {
            "evals": ["first-eval"],
            "models": ["hosted-a"],
            "n": 1,
            "graders": {"first-eval": {"default": "skip"}},
        },
    )
    job = wait_sweep(get)
    assert job["status"] == "done", job
    assert not any((d / "grades").exists() for d in run_dirs(first))


def test_sweep_defers_model_keyed_grader_to_a_final_pass(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", STUB_RUNNER)
    # The judge pattern: a check carrying a model key defers by default
    (first / "graders" / "judge.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "judge",
                "checks": [
                    {"checker": "contains", "value": "STUB", "model": "gpt-4.1"}
                ],
            }
        )
    )

    post_sweep(
        get, {"evals": ["first-eval"], "models": ["hosted-a", "hosted-b"], "n": 1}
    )
    job = wait_sweep(get)
    assert job["status"] == "done", job
    assert job["deferred"] == [
        {"eval": "first-eval", "grader": "judge", "state": "done"}
    ]

    dirs = run_dirs(first)
    assert len(dirs) == 2
    # Every run (both models) got the judge grade, and every judge grade
    # postdates every run: one deferred pass after the last model, not
    # inline grading during each model's phase
    run_starts = [
        datetime.fromisoformat(read_yaml(d / "run.yaml")["started"]) for d in dirs
    ]
    judge_times = []
    for run_dir in dirs:
        grade = read_yaml(run_dir / "grades" / "judge" / "grade.yaml")
        assert grade["outcome"] == "pass"
        judge_times.append(datetime.fromisoformat(grade["graded"]))
        # the inline default grader still graded this run during the sweep
        assert (run_dir / "grades" / "default" / "grade.yaml").exists()
    assert min(judge_times) > max(run_starts)


def test_sweep_executes_only_the_shortfall(server, fake_lms, tmp_path):
    # The resumability proof: pre-seeded runs count toward the target, so
    # the sweep executes only what is missing
    get, root, first, second = server
    counter_log = tmp_path / "executions.log"
    write_executable(first / "run-llm", order_logging_runner(counter_log, "first-eval"))
    write_run(first / "runs", task="example", model="hosted-a")
    write_run(first / "runs", task="example", model="hosted-a")

    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-a"], "n": 3})
    job = wait_sweep(get)
    assert job["status"] == "done", job
    assert counter_log.read_text().splitlines() == ["first-eval hosted-a"]
    cell = job["cells"][cell_key("first-eval", "hosted-a")]
    assert cell["have"] == 3
    assert cell["target"] == 3
    assert cell["done"] == 1


def test_sweep_cancel_stops_after_the_in_flight_run(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", SLOW_SWEEP_RUNNER)

    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-a"], "n": 8})
    # Wait for the first run to be in flight, then ask for cancellation
    deadline = time.monotonic() + 10
    while True:
        _, _, body = get("/api/sweep")
        if json.loads(body).get("current"):
            break
        assert time.monotonic() < deadline, "sweep never started a run"
        time.sleep(0.02)
    status, _, body = get("/api/sweep/cancel", method="POST", body={})
    assert status == 200
    assert json.loads(body)["cancel_requested"] is True

    job = wait_sweep(get)
    assert job["status"] == "cancelled"
    executed = len(run_dirs(first))
    assert 1 <= executed < 8
    # never mid-run: every executed run is complete (run.yaml is written
    # last, and the last one carries a real exit code)
    assert all((d / "run.yaml").exists() for d in run_dirs(first))


def test_second_sweep_is_409_while_one_is_active(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", SLOW_SWEEP_RUNNER)

    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-a"], "n": 3})
    error = post_sweep(
        get, {"evals": ["second-eval"], "models": ["hosted-b"], "n": 1}, expect=409
    )
    assert error["error"] == SWEEP_ACTIVE_MESSAGE

    get("/api/sweep/cancel", method="POST", body={})
    job = wait_sweep(get)
    assert job["status"] == "cancelled"
    # A finished sweep frees the slot for the next one
    write_executable(second / "run-llm", STUB_RUNNER)
    post_sweep(get, {"evals": ["second-eval"], "models": ["hosted-b"], "n": 1})
    assert wait_sweep(get)["status"] == "done"


def test_bench_run_and_grade_409_while_sweep_holds_the_eval(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", SLOW_SWEEP_RUNNER)
    seeded = write_run(first / "runs", task="example", model="pre-seeded")

    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-a"], "n": 8})
    saw_run_409 = saw_grade_409 = False
    deadline = time.monotonic() + 15
    while not (saw_run_409 and saw_grade_409):
        assert time.monotonic() < deadline, "never observed the sweep-held 409s"
        _, _, body = get("/api/sweep")
        sweep_job = json.loads(body)
        assert sweep_job["status"] in ("queued", "running"), sweep_job
        if not sweep_job.get("current"):
            time.sleep(0.02)
            continue
        # The sweep holds first-eval's slot while a run is in flight -
        # bench actions on that eval must 409 with the spec'd message
        if not saw_run_409:
            status, _, body = get(
                "/api/evals/first-eval/run",
                method="POST",
                body={"task": "example", "config": "default", "model": "bench-m"},
            )
            if status == 409 and json.loads(body)["error"] == SWEEP_HOLDS_EVAL_MESSAGE:
                saw_run_409 = True
        if not saw_grade_409:
            status, _, body = get(
                "/api/evals/first-eval/grade",
                method="POST",
                body={
                    "run": str(seeded.relative_to(first / "runs")),
                    "grader": "default",
                },
            )
            if status == 409 and json.loads(body)["error"] == SWEEP_HOLDS_EVAL_MESSAGE:
                saw_grade_409 = True

    get("/api/sweep/cancel", method="POST", body={})
    assert wait_sweep(get)["status"] == "cancelled"


def test_sweep_waits_for_an_active_bench_job(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", SLOW_SWEEP_RUNNER)

    status, _, body = get(
        "/api/evals/first-eval/run",
        method="POST",
        body={"task": "example", "config": "default", "model": "bench-m"},
    )
    assert status == 202, body
    bench_job = json.loads(body)

    # The sweep never 409s against a bench job: it waits for the slot
    post_sweep(get, {"evals": ["first-eval"], "models": ["hosted-a"], "n": 1})
    job = wait_sweep(get)
    assert job["status"] == "done", job

    _, _, body = get(f"/api/jobs/{bench_job['id']}")
    assert json.loads(body)["status"] == "done"
    # Both the bench run and the sweep's run landed
    assert len(run_dirs(first)) == 2


def test_sweep_failing_runner_marks_the_cell_but_the_sweep_completes(server, fake_lms):
    get, root, first, second = server
    write_executable(first / "run-llm", FAILING_RUNNER)
    write_executable(second / "run-llm", STUB_RUNNER)

    post_sweep(
        get,
        {"evals": ["first-eval", "second-eval"], "models": ["hosted-a"], "n": 2},
    )
    job = wait_sweep(get)
    assert job["status"] == "done", job  # a failing pair never halts the sweep
    failing = job["cells"][cell_key("first-eval", "hosted-a")]
    assert failing["state"] == "failed"
    assert failing["failed"] == 2
    assert failing["have"] == 0
    healthy = job["cells"][cell_key("second-eval", "hosted-a")]
    assert healthy["state"] == "done"
    assert healthy["have"] == 2


# --- GET /api/sweep/plan ------------------------------------------------------


def test_sweep_plan_previews_shortfall_and_treatments(server):
    get, root, first, second = server
    (first / "graders" / "judge.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "judge",
                "checks": [{"checker": "contains", "value": "x", "model": "gpt-4.1"}],
            }
        )
    )
    write_run(first / "runs", task="example", model="hosted-a")

    status, ctype, body = get(
        "/api/sweep/plan?evals=first-eval&models=hosted-a,hosted-b&n=3"
    )
    assert status == 200
    assert ctype == "application/json"
    plan = json.loads(body)
    assert plan["cells"] == {
        cell_key("first-eval", "hosted-a"): {"target": 3, "have": 1, "remaining": 2},
        cell_key("first-eval", "hosted-b"): {"target": 3, "have": 0, "remaining": 3},
    }
    assert plan["graders"] == {"first-eval": {"default": "inline", "judge": "deferred"}}
    assert plan["n"] == 3


def test_sweep_plan_defaults_to_every_eval_without_models(server):
    get, *_ = server
    status, _, body = get("/api/sweep/plan")
    assert status == 200
    plan = json.loads(body)
    assert plan["evals"] == ["first-eval", "second-eval"]
    assert plan["cells"] == {}
    assert set(plan["graders"]) == {"first-eval", "second-eval"}


def test_sweep_plan_unknown_eval_is_400(server):
    get, *_ = server
    status, _, body = get("/api/sweep/plan?evals=nope")
    assert status == 400
    assert "error" in json.loads(body)


def test_sweep_plan_bad_n_is_400(server):
    get, *_ = server
    status, _, body = get("/api/sweep/plan?n=zero")
    assert status == 400
    assert "error" in json.loads(body)


# --- POST /api/sweep validation ----------------------------------------------


def test_sweep_post_rejects_bad_specs(server):
    get, *_ = server
    for body in (
        {"models": ["m"], "evals": ["nope"]},
        {"models": []},
        {"models": ["m"], "n": 0},
        {"models": ["m"], "graders": {"first-eval": {"default": "later"}}},
    ):
        status, _, reply = get("/api/sweep", method="POST", body=body)
        assert status == 400, (body, reply)
        assert "error" in json.loads(reply)
    # nothing started
    assert json.loads(get("/api/sweep")[2]) == {"active": False}


def test_sweep_cancel_without_a_sweep_is_404(server):
    get, *_ = server
    status, _, body = get("/api/sweep/cancel", method="POST", body={})
    assert status == 404
    assert "error" in json.loads(body)


def test_models_includes_lms_inventory_as_local(server, fake_lms):
    get, *_ = server
    status, _, body = get("/api/models")
    assert status == 200
    data = json.loads(body)
    assert data["local"] == ["local-alpha", "local-beta"]
    assert {"local-alpha", "local-beta", "gpt-4.1-mini"} <= set(data["models"])
