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
import socket
import subprocess
import threading
import time

import pytest
import yaml

from conftest import python_script, read_yaml, write_executable
from smevals import studio
from smevals.authoring import FILE_SCHEMAS, scaffold_eval

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
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(target=studio.run_studio, args=(root, port), daemon=True).start()

    def get(path, method="GET", body=None):
        "GET by default; pass method=/body= for POST and PUT requests"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        for attempt in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, body=data, headers=headers)
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


def test_models_configs_only_fallback_without_llm_on_path(
    server, monkeypatch, tmp_path
):
    get, *_ = server
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_path_dir))

    status, ctype, body = get("/api/models")
    assert status == 200
    assert ctype == "application/json"
    data = json.loads(body)
    assert set(data["models"]) == {
        "gpt-4.1-mini"
    }  # both scaffolded evals' config default


def test_models_parses_real_llm_plain_text_format(server, monkeypatch, tmp_path):
    get, *_ = server
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "llm", FAKE_LLM)
    monkeypatch.setenv("PATH", str(bin_dir))

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
