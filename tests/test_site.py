"""Tests for eval discovery, the static site builder and the live server."""

import http.client
import json
import socket
import threading
import time

import click
import pytest
import yaml

from conftest import read_yaml, write_grade, write_run
from smevals import site
from smevals.cli import discover_evals, resolve_eval_slugs


def graded_eval(make_eval, tmp_path, name="demo", models=("m-1",), scores=(1.0,)):
    eval_dir = make_eval(name=name, runner=None, root=tmp_path)
    grader_doc = read_yaml(eval_dir / "graders" / "default.yaml")
    for model, score in zip(models, scores):
        run_dir = write_run(eval_dir / "runs", model=model)
        write_grade(
            run_dir,
            grader_doc,
            score=score,
            outcome="pass" if score >= 0.5 else "fail",
        )
    return eval_dir


# --- discovery -----------------------------------------------------------


def test_discover_evals_recurses_and_stops_at_evals(make_eval, tmp_path):
    suite = tmp_path / "suite"
    a = make_eval(name="a", root=suite)
    b = make_eval(name="b", root=suite / "nested")
    make_eval(name="hidden", root=suite / ".secret")
    # A decoy eval.yaml inside an Eval must not be discovered: descent
    # stops at each Eval so runs/ trees are never scanned
    decoy = a / "runs" / "decoy"
    decoy.mkdir(parents=True)
    (decoy / "eval.yaml").write_text("name: decoy\n")

    assert list(discover_evals(suite)) == [a, b]


def test_resolve_eval_slugs_rejects_duplicates(make_eval, tmp_path):
    suite = tmp_path / "suite"
    make_eval(name="one", root=suite)
    dupe = make_eval(name="two", root=suite)
    (dupe / "eval.yaml").write_text("name: one\n")  # same name, different dir
    with pytest.raises(click.ClickException, match="Duplicate eval slug 'one'"):
        resolve_eval_slugs([suite])


def test_resolve_eval_slugs_errors_on_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(click.ClickException, match="No Evals found"):
        resolve_eval_slugs([tmp_path / "empty"])


# --- data layer ----------------------------------------------------------


def test_collect_eval_rows_and_grades(make_eval, tmp_path):
    eval_dir = graded_eval(make_eval, tmp_path)
    data = site.collect_eval(eval_dir)
    assert data["eval"]["name"] == "demo"
    assert data["eval"]["default_grader"] == "default"
    assert [t["name"] for t in data["eval"]["tasks"]] == ["first"]
    (row,) = data["rows"]
    assert row["task"] == "first"
    assert row["model"] == "m-1"
    assert "output.txt" in row["files"]
    grade = row["grades"]["default"]
    assert grade["outcome"] == "pass"
    assert grade["score"] == 1.0
    # grade.yaml itself is not listed among the grade's artifact files
    assert grade["files"] == ["grader.yaml"]


def test_eval_summary_picks_best_mean(make_eval, tmp_path):
    eval_dir = graded_eval(
        make_eval,
        tmp_path,
        models=("m-good", "m-good", "m-bad"),
        scores=(1.0, 0.8, 0.4),
    )
    data = site.collect_eval(eval_dir)
    summary = site.eval_summary("demo", data)
    assert summary["runs"] == 3
    assert summary["graded"] == 3
    assert summary["fails"] == 1
    assert summary["best"] == {
        "config": "default",
        "model": "m-good",
        "score": 0.9,
        "runs": 2,
    }


# --- results aggregation (group_stats / eval_results / results_matrix) ---
#
# Verification corpus for the reporter semantics documented in the
# scratchpad reporter's README/verification.md: failed-run exclusion,
# stale-grade detection, graded-but-unscored gaps, target-n inference and
# the all-runs-failed model appearing at n=0 rather than vanishing.


def results_eval(make_eval, tmp_path, name="results"):
    "A bare eval scaffold plus its parsed default grader doc, for hand-built runs/grades"
    eval_dir = make_eval(name=name, runner=None, root=tmp_path)
    grader_doc = read_yaml(eval_dir / "graders" / "default.yaml")
    return eval_dir, grader_doc


def test_group_stats_excludes_failed_runs_from_means(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, model="m-1"), grader_doc, score=1.0)
    write_grade(write_run(runs_root, model="m-1"), grader_doc, score=0.6)
    # A failed Run that was graded anyway (e.g. graded before the failure
    # was noticed) - a harness error is never evidence, so it must be
    # excluded from the mean and from n, same as `smevals report`
    failed = write_run(runs_root, model="m-1", exit_code=1, output="")
    write_grade(failed, grader_doc, outcome="fail", score=0.0)

    data = site.collect_eval(eval_dir)
    (group,) = site.group_stats(data["rows"], "default")
    assert group["config"] == "default"
    assert group["model"] == "m-1"
    assert group["n"] == 2
    assert group["scored_n"] == 2
    assert group["mean"] == pytest.approx(0.8)


def test_group_stats_scored_n_gap_for_unscored_grade(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, model="m-1"), grader_doc, score=1.0)
    # A required Check failed before the scoring Check ran: graded, but
    # unscored - counted in n, not in scored_n
    write_grade(
        write_run(runs_root, model="m-1"), grader_doc, outcome="fail", score=None
    )

    data = site.collect_eval(eval_dir)
    (group,) = site.group_stats(data["rows"], "default")
    assert group["n"] == 2
    assert group["scored_n"] == 1
    assert group["mean"] == 1.0
    assert group["fail_count"] == 1


def test_group_stats_metrics_summarize_bool_and_numeric(make_eval, tmp_path):
    # Mirrors cli.render_model_blocks: a metric key whose every value is a
    # bool becomes a true-rate; anything else becomes mean +- stderr
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(
        write_run(runs_root, model="m-1"),
        grader_doc,
        score=1.0,
        checks=[
            {
                "checker": "c",
                "ok": True,
                "metrics": {"latency": 2.0, "status_correct": True},
            }
        ],
    )
    write_grade(
        write_run(runs_root, model="m-1"),
        grader_doc,
        score=1.0,
        checks=[
            {
                "checker": "c",
                "ok": True,
                "metrics": {"latency": 4.0, "status_correct": False},
            }
        ],
    )

    data = site.collect_eval(eval_dir)
    (group,) = site.group_stats(data["rows"], "default")
    assert group["metrics"]["latency"] == {
        "type": "numeric",
        "mean": 3.0,
        "stderr": 1.0,
        "n": 2,
    }
    assert group["metrics"]["status_correct"] == {
        "type": "bool",
        "true_rate": 0.5,
        "n": 2,
    }


def test_group_stats_tag_counts_and_rates(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(
        write_run(runs_root, model="m-1"), grader_doc, score=1.0, tags=["hat", "bike"]
    )
    write_grade(
        write_run(runs_root, model="m-1"),
        grader_doc,
        outcome="fail",
        score=0.0,
        tags=["hat"],
    )

    data = site.collect_eval(eval_dir)
    (group,) = site.group_stats(data["rows"], "default")
    assert group["tag_counts"] == {"hat": 2, "bike": 1}
    assert group["fail_count"] == 1
    assert group["pass_rate"] == pytest.approx(0.5)


def test_group_stats_sorted_by_mean_desc_then_model(make_eval, tmp_path):
    # Same fixture as test_report.py's leaderboard-ordering test - the
    # ranking rule (mean descending, model name breaks ties) must agree
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    for model, scores in [
        ("model-a", [1.0, 0.8]),
        ("model-b", [0.6]),
        ("model-c", [0.6]),
        ("model-d", [0.5]),
    ]:
        for score in scores:
            write_grade(write_run(runs_root, model=model), grader_doc, score=score)

    data = site.collect_eval(eval_dir)
    groups = site.group_stats(data["rows"], "default")
    assert [g["model"] for g in groups] == ["model-a", "model-b", "model-c", "model-d"]


def test_group_stats_reconciles_with_report_mean_stderr(invoke, make_eval, tmp_path):
    # Ground truth: `smevals report`'s own rendered leaderboard figure for
    # this fixture (test_report.py asserts the identical string)
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    for score in (1.0, 0.8):
        write_grade(write_run(runs_root, model="model-a"), grader_doc, score=score)

    result = invoke("report", eval_dir)
    assert "0.90 ±0.10" in result.output

    data = site.collect_eval(eval_dir)
    (group,) = site.group_stats(data["rows"], "default")
    assert f"{group['mean']:.2f} ±{group['stderr']:.2f}" == "0.90 ±0.10"


def test_eval_results_shape_and_excluded_failed(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, model="m-1"), grader_doc, score=1.0)
    failed = write_run(runs_root, model="m-1", exit_code=1, output="")
    write_grade(failed, grader_doc, outcome="fail", score=0.0)

    results = site.eval_results(eval_dir, "default")
    assert results["grader"] == "default"
    assert results["graders"] == ["default"]
    assert results["total"] == 1
    assert results["excluded_failed"] == 1
    assert results["ungraded"] == 0
    assert results["stale"] == 0
    assert len(results["groups"]) == 1
    assert "generated" in results


def test_eval_results_ungraded_counts_non_failed_without_grade(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, model="m-1"), grader_doc, score=1.0)
    write_run(runs_root, model="m-1")  # never graded

    results = site.eval_results(eval_dir, "default")
    assert results["ungraded"] == 1
    assert results["total"] == 1


def test_eval_results_stale_detection_after_grader_edit(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    write_grade(write_run(eval_dir / "runs", model="m-1"), grader_doc, score=1.0)

    # Edit the grader after grading: the recorded snapshot no longer
    # parses equal to the current spec
    (eval_dir / "graders" / "default.yaml").write_text(
        yaml.safe_dump(
            {"name": "default", "checks": [{"checker": "contains", "value": "goodbye"}]}
        )
    )

    results = site.eval_results(eval_dir, "default")
    assert results["stale"] == 1


def test_eval_results_grader_fallback_when_named_grader_missing(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    write_grade(write_run(eval_dir / "runs", model="m-1"), grader_doc, score=1.0)

    results = site.eval_results(eval_dir, "nonexistent")
    assert results["grader"] == "default"  # falls back like collect_eval/serve do


def test_eval_results_with_no_graders_returns_empty_shape(make_eval, tmp_path):
    # An Eval with no graders/*.yaml at all: nothing to fall back to
    eval_dir = make_eval(name="results", runner=None, root=tmp_path, graders={})
    write_run(eval_dir / "runs", model="m-1")

    results = site.eval_results(eval_dir, "default")
    assert results["grader"] is None
    assert results["graders"] == []
    assert results["groups"] == []
    assert results["ungraded"] == 0
    assert results["excluded_failed"] == 0


def test_eval_results_tags_aggregate_across_groups(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, model="m-1"), grader_doc, score=1.0, tags=["hat"])
    write_grade(
        write_run(runs_root, model="m-2"), grader_doc, score=1.0, tags=["hat", "bike"]
    )

    results = site.eval_results(eval_dir, "default")
    assert results["tags"] == {"hat": 2, "bike": 1}


# --- eval_results task filter (Batch 3 item 6: per-task leaderboard) ------
#
# Additive: task=None (the default) must keep behaving exactly like before
# this parameter existed - covered by every eval_results test above, which
# calls it with no task argument at all.


def test_eval_results_task_filter_scopes_groups_and_total(make_eval, tmp_path):
    eval_dir = make_eval(
        name="results",
        tasks={"alpha": {"prompt": "a"}, "beta": {"prompt": "b"}},
        runner=None,
        root=tmp_path,
    )
    grader_doc = read_yaml(eval_dir / "graders" / "default.yaml")
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, task="alpha", model="m-1"), grader_doc, score=1.0)
    write_grade(write_run(runs_root, task="alpha", model="m-1"), grader_doc, score=0.6)
    write_grade(write_run(runs_root, task="beta", model="m-1"), grader_doc, score=0.2)

    scoped = site.eval_results(eval_dir, "default", task="alpha")
    assert scoped["task"] == "alpha"
    assert scoped["total"] == 2
    (group,) = scoped["groups"]
    assert group["n"] == 2
    assert group["mean"] == pytest.approx(0.8)

    # unscoped (the default) still covers every task
    unscoped = site.eval_results(eval_dir, "default")
    assert unscoped["task"] is None
    assert unscoped["total"] == 3


def test_eval_results_task_filter_scopes_excluded_and_ungraded(make_eval, tmp_path):
    eval_dir = make_eval(
        name="results",
        tasks={"alpha": {"prompt": "a"}, "beta": {"prompt": "b"}},
        runner=None,
        root=tmp_path,
    )
    grader_doc = read_yaml(eval_dir / "graders" / "default.yaml")
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, task="alpha", model="m-1"), grader_doc, score=1.0)
    write_run(runs_root, task="alpha", model="m-1")  # never graded
    failed = write_run(runs_root, task="beta", model="m-1", exit_code=1, output="")
    write_grade(failed, grader_doc, outcome="fail", score=0.0)

    alpha = site.eval_results(eval_dir, "default", task="alpha")
    assert alpha["ungraded"] == 1
    assert alpha["excluded_failed"] == 0

    beta = site.eval_results(eval_dir, "default", task="beta")
    assert beta["ungraded"] == 0
    assert beta["excluded_failed"] == 1


def test_eval_results_task_filter_unknown_task_is_empty(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    write_grade(write_run(eval_dir / "runs", model="m-1"), grader_doc, score=1.0)

    results = site.eval_results(eval_dir, "default", task="no-such-task")
    assert results["groups"] == []
    assert results["total"] == 0


def test_results_matrix_target_n_inference_and_incomplete(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    for _ in range(3):
        write_grade(write_run(runs_root, model="model-a"), grader_doc, score=1.0)
    write_grade(write_run(runs_root, model="model-b"), grader_doc, score=0.5)

    matrix = site.results_matrix({"results": eval_dir})
    assert matrix["evals"] == ["results"]
    assert set(matrix["models"]) == {"model-a", "model-b"}
    assert matrix["matrix"]["model-a"]["results"] == {
        "mean": 1.0,
        "n": 3,
        "target_n": 3,
        "incomplete": False,
    }
    assert matrix["matrix"]["model-b"]["results"] == {
        "mean": 0.5,
        "n": 1,
        "target_n": 3,
        "incomplete": True,
    }
    assert "generated" in matrix


def test_results_matrix_all_failed_model_present_at_n_zero(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    for _ in range(2):
        write_grade(write_run(runs_root, model="good-model"), grader_doc, score=1.0)
    write_run(runs_root, model="flaky-model", exit_code=1, output="")

    matrix = site.results_matrix({"results": eval_dir})
    assert "flaky-model" in matrix["models"]
    assert matrix["matrix"]["flaky-model"]["results"] == {
        "mean": None,
        "n": 0,
        "target_n": 2,
        "incomplete": True,
    }


def test_results_matrix_best_per_eval_derivable(make_eval, tmp_path):
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    write_grade(write_run(runs_root, model="model-a"), grader_doc, score=1.0)
    write_grade(write_run(runs_root, model="model-b"), grader_doc, score=0.5)

    matrix = site.results_matrix({"results": eval_dir})
    best_model = max(
        matrix["models"], key=lambda m: matrix["matrix"][m]["results"]["mean"]
    )
    assert best_model == "model-a"


def test_results_matrix_uses_busiest_config(make_eval, tmp_path):
    # design note from the reporter README: an eval running one model
    # under two configs shows only the busiest config in the matrix
    eval_dir, grader_doc = results_eval(make_eval, tmp_path)
    runs_root = eval_dir / "runs"
    for _ in range(2):
        write_grade(
            write_run(runs_root, model="model-a", config="default"),
            grader_doc,
            score=1.0,
        )
    write_grade(
        write_run(runs_root, model="model-a", config="alt"), grader_doc, score=0.2
    )

    matrix = site.results_matrix({"results": eval_dir})
    assert matrix["matrix"]["model-a"]["results"]["mean"] == 1.0


# --- static build --------------------------------------------------------


def test_build_creates_self_contained_site(invoke, make_eval, tmp_path):
    eval_a = graded_eval(make_eval, tmp_path, name="alpha")
    eval_b = graded_eval(make_eval, tmp_path, name="beta")
    site_dir = tmp_path / "site"
    invoke("build", eval_a, eval_b, "-o", site_dir)

    assert (site_dir / "index.html").read_text() == site.app_html()
    index = json.loads((site_dir / "index.json").read_text())
    assert index["live"] is False
    assert [e["slug"] for e in index["evals"]] == ["alpha", "beta"]

    data = json.loads((site_dir / "evals" / "alpha" / "eval.json").read_text())
    assert len(data["rows"]) == 1
    # Run artifacts are copied into the site
    copied = site_dir / "evals" / "alpha" / "runs" / data["rows"][0]["run"]
    assert (copied / "output.txt").exists()
    assert (copied / "grades" / "default" / "grade.yaml").exists()


def test_build_refreshes_one_eval_without_touching_others(invoke, make_eval, tmp_path):
    eval_a = graded_eval(make_eval, tmp_path, name="alpha")
    eval_b = graded_eval(make_eval, tmp_path, name="beta")
    site_dir = tmp_path / "site"
    invoke("build", eval_a, "-o", site_dir)
    invoke("build", eval_b, "-o", site_dir)

    # Add a run to alpha and rebuild only alpha
    grader_doc = read_yaml(eval_a / "graders" / "default.yaml")
    write_grade(write_run(eval_a / "runs"), grader_doc, score=0.5)
    invoke("build", eval_a, "-o", site_dir)

    index = json.loads((site_dir / "index.json").read_text())
    by_slug = {e["slug"]: e for e in index["evals"]}
    assert set(by_slug) == {"alpha", "beta"}
    assert by_slug["alpha"]["runs"] == 2
    assert by_slug["beta"]["runs"] == 1


# --- live server ---------------------------------------------------------


@pytest.fixture
def server(make_eval, tmp_path):
    eval_dir = graded_eval(make_eval, tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(
        target=site.run_server,
        args=({"demo": eval_dir}, "default", "127.0.0.1", port),
        daemon=True,
    ).start()

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

    return get, eval_dir


def test_serve_index_and_eval_json(server):
    get, eval_dir = server
    status, ctype, body = get("/")
    assert status == 200
    assert ctype == "text/html"

    status, ctype, body = get("/index.json")
    assert status == 200
    index = json.loads(body)
    assert index["live"] is True
    assert index["evals"][0]["slug"] == "demo"

    status, _, body = get("/evals/demo/eval.json")
    assert status == 200
    assert len(json.loads(body)["rows"]) == 1

    status, _, _ = get("/evals/nope/eval.json")
    assert status == 404


def test_serve_run_artifacts_with_inline_yaml(server):
    get, eval_dir = server
    data = site.collect_eval(eval_dir)
    rel = data["rows"][0]["run"]
    # YAML is served as text/plain so browsers render it inline
    status, ctype, body = get(f"/evals/demo/runs/{rel}/run.yaml")
    assert status == 200
    assert ctype == "text/plain; charset=utf-8"
    assert b"exit_code" in body

    status, _, body = get(f"/evals/demo/runs/{rel}/output.txt")
    assert status == 200
    assert body == b"hello world\n"


def test_serve_refuses_prefix_sibling_of_runs(server):
    # A sibling dir whose name merely starts with "runs" must not be
    # reachable - a string-prefix containment check would let it through
    get, eval_dir = server
    secret = eval_dir / "runs-secret" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("do not serve me")
    status, _, _ = get("/evals/demo/runs/../runs-secret/secret.txt")
    assert status == 404


def test_serve_refuses_paths_outside_runs(server):
    get, eval_dir = server
    # http.client sends the path verbatim - no client-side normalization
    status, _, _ = get("/evals/demo/runs/../eval.yaml")
    assert status == 404
    status, _, _ = get("/evals/demo/runs/../../../etc/passwd")
    assert status == 404
