# smevals studio phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One app. The studio absorbs the results dashboards (per-eval Results tab, shelf results strip, global models × evals matrix), gains a full sweep orchestrator (evals × models × n, one sweep at a time, live board, cancel, resumable), and the task editor's env mirror moves inline into the form per Jesse's verbatim direction. `smevals serve`/`build` remain the read-only publishing path, untouched apart from shared-code additions to site.py.

**Architecture:** site.py stays the one shared data layer — new pure aggregation functions land there (reporter semantics: failed-run exclusion, stale-grade annotation, target-n inference) and studio exposes them through new read-only endpoints; the sweep orchestrator is a new sweep.py driving cli.py's real run/grade internals in-process in a background thread, registered in the phase-1 job table; studio.html grows three surfaces (Results tab, global Results, Sweeps) inside the existing token system and hash router. No new dependencies, no build step, one HTML file, 127.0.0.1 only.

**Tech Stack:** Python 3.10+ stdlib + pyyaml + click (existing); vanilla JS/CSS in studio.html (existing conventions: design tokens, esc() discipline, hash routing, polling).

**Spec (binding; read the relevant sections before each task):** `docs/superpowers/specs/2026-08-03-studio-phase-2-design.md` (and phase 1's `2026-08-02-smevals-studio-design.md` for everything it doesn't supersede).

## Global Constraints

- Continue on the `smevals-studio` WIP branch; TDD throughout; the repo's no-mock rule (real server on an ephemeral port, real tmp evals, real stub executables honoring the documented contracts — including a fake `lms` executable on PATH for sweep tests, in the style of `test_models_configs_only_fallback_without_llm_on_path`).
- Zero new dependencies; studio.html stays ONE self-contained file; every visual addition draws from the existing design tokens; both color schemes screenshotted for every new surface.
- ONE aggregation code path: results numbers come from site.py functions shared with serve — never a JS re-implementation forked from app.html's math. cli.py and site.py refactors are extraction-only (extract-and-delegate; CLI and serve behavior byte-identical; full suite green proves it).
- runs/ immutable except via the real run path (`execute_run`); sweeps write nothing anywhere else — sweep state is in-memory job state; resumability comes from top-up semantics plus the browser remembering the last sweep composition (localStorage), never a server-side state file.
- **Lock discipline (decided here, stated in the UI):** a sweep holds the per-eval `active_jobs` slot only for the duration of its current run and releases it between runs. A bench run/grade attempted while the sweep holds that eval's slot gets **409** with the message `a sweep is running this eval right now — it frees up between runs, try again in a moment`. The sweep itself never 409s: if a bench job holds the eval when the sweep reaches it, the sweep waits (0.5s poll) until the slot frees. One sweep at a time: starting a second is 409 `a sweep is already running`.
- The existing smoke tests (`test_studio_html_calls_only_routes_studio_py_serves` and the interpolated-tails variant) must stay green — every new `/api/` path the JS calls must exist in studio.py.
- All API errors JSON `{error}`; failures render inline where the action happened; Black-formatted Python.

## File Map

```
src/smevals/site.py         # Task 1: shared aggregation (group_stats, eval_results, results_matrix)
src/smevals/studio.py       # Task 1: results endpoints + summary counts; Task 3: sweep endpoints + wiring
src/smevals/sweep.py        # Task 3: orchestrator, sweep planning, lms integration (new file)
src/smevals/cli.py          # Task 3: extraction-only refactors (compute_remaining, grade_pending)
src/smevals/studio.html     # Task 2: results surfaces + lazy runs fix; Task 4: sweeps; Task 5: inline env
tests/test_site.py          # Task 1: aggregation semantics
tests/test_studio.py        # Tasks 1, 3: endpoint + sweep lifecycle tests
tests/studio_pure/cases.json# Task 5: env-annotation corpus updates
```

---

### Task 1: shared aggregation layer + results read APIs

The data layer for every results surface, adopting the scratchpad reporter's semantics on top of site.py's `collect_eval`. site.py is already the shared module both serve and studio import — no new module needed; additions are new pure functions beside `collect_eval`/`eval_summary`, and serve/build behavior does not change.

**Interfaces produced (binding):**

- `site.group_stats(rows, grader_name) -> list[dict]` — group `collect_eval` rows by (config, model), **excluding rows whose run failed** (`exit_code` non-zero — reporter semantics; a failed Run is a harness error, not evidence). Each group: `{config, model, n, scored_n, mean, stderr, pass_rate, fail_count, tag_counts, metrics}` where `mean`/`stderr` match `cli.mean_stderr`'s math (stderr `None` when n<2), `metrics` summarizes per-key exactly as `render_model_blocks` does (boolean keys → true-rate, numeric → mean/stderr), sorted by mean descending then model (reporter's ordering).
- `site.eval_results(eval_path, grader_name) -> dict` — one eval's Results-tab document: `{grader, graders: [names], groups: group_stats(...), tags: {tag: count}, total, excluded_failed, ungraded, stale, generated}`. `stale` counts grades whose `grades/<name>/grader.yaml` snapshot no longer parses equal to the current grader spec (reuse `cli.grade_matches_grader` — import it, don't fork it). `ungraded` = non-failed runs with no grade from this grader.
- `site.results_matrix(evals: dict[slug, Path]) -> dict` — the executive summary, reporter semantics throughout: `{evals: [slug], models: [...], matrix: {model: {slug: {mean, n, target_n, incomplete}}}, generated}`. Per eval: default grader (via `collect_eval`'s `default_grader` fallback), primary config = the config with the most runs, `target_n` inferred as the highest non-failed run count for any (config, model) pair, `incomplete` = n < target_n, and **a model whose every run failed appears with n=0 rather than vanishing**.
- studio.py endpoints (read-only, traversal-safe, mirroring existing idioms):
  - `GET /api/evals/<slug>/results?grader=` → `eval_results` (grader param optional; falls back like serve does).
  - `GET /api/results` → `results_matrix` over `discover_slugs(root)`.
  - `GET /api/evals` entries extended with `"runs": {"total": N, "failed": N}` and `"best": {model, config, score, runs} | null` — best = highest mean from `group_stats` under the default grader. This is the shelf strip's data AND the cheap count the lazy-runs fix (Task 2) needs; it rides the `run.yaml` rglob `last_run_iso` already does, so no extra walk.

**Steps:**
- [ ] Failing tests first, in tests/test_site.py: fixture evals in tmp_path reproducing the reporter README's documented cases as the verification corpus — failed runs excluded from means but counted in `excluded_failed`; stale detection after a grader edit; graded-but-unscored runs counted in `scored_n` gap; target-n inference; the all-runs-failed model present at n=0; matrix best-per-eval derivable from the payload; `group_stats` numbers reconciling with `smevals report`'s rendered mean ± stderr for the same fixture.
- [ ] Failing endpoint tests in tests/test_studio.py: `/api/evals/<slug>/results` shape + grader fallback + 404s; `/api/results`; extended `/api/evals` counts/best (and that `best` matches what `site.eval_summary` computes for the same data — the "matching serve's index summary" clause).
- [ ] Implement in site.py + studio.py → green → Black → commit.

---

### Task 2: Results surfaces in studio.html + the lazy runs fix

**Rails:** three surfaces, all fed by Task 1's endpoints, rendered with the existing tokens; router grows `#/results` (global) and `#/eval/<slug>/results` (per-eval tab). Per-eval Results tab sits beside the file workbench and runs surfaces (a segmented control in the wb-head or tree, implementation's choice — one obvious switch): grader picker, leaderboard (config × model, mean ± stderr, run/fail counts, pass rate — competition ranking, ties share a rank), tag shares, per-model metric blocks, and stale/excluded/ungraded annotations rendered as quiet badges, never hidden. Drill-down: each leaderboard group links into the existing runs list filtered to that config × model, and rows open the **existing run detail** — no second run renderer. Shelf: each card gains a compact results strip (best score + model from the default grader, from the new `best` field) and a `Results` entry in the header/nav opens the global matrix: models × evals, mean default-grader scores, best-per-eval highlighted with the accent, incomplete cells marked (n/target in the cell's title and a footnote row), empty states that teach. All numbers come from the API — the JS formats, it never aggregates.

**The phase-1 debt, absorbed here (binding):** remove the eager `loadRuns(wb)` call from `loadWorkbench` (studio.html line ~1842). Runs are fetched lazily by the first surface that needs rows: the runs list/detail, the dry-run picker, and the bench's latest-run panel each call an `ensureRuns(wb)` helper (single-flight, cached, `force` for refreshes); chrome that only needs counts (tree, shelf, Results tab header) uses the summary counts from Task 1 instead of the full listing. The Results tab itself calls `/results`, not `/runs`, so opening it on a large eval never pays for the full row fetch until the author actually drills into runs.

**Live like serve:** a single poll ticker (~3s) re-fetches the visible results surface (and the shelf strip) **only while a sweep job is active** (wired fully in Task 4; this task lands the ticker gated on a `state.sweepActive` flag that Task 4 sets).

**Steps:**
- [ ] Build against a live backend over a scratch suite with real swept fixture data (reuse the Task 1 fixtures); verify grader switching, drill-down, stale/incomplete annotations, and that the workbench no longer fetches `/runs` on open (network tab evidence).
- [ ] Keep the endpoint cross-grep smoke green; add a pytest asserting studio.html references the new routes.
- [ ] Screenshots: shelf with results strips, per-eval Results tab, global matrix — both color schemes → commit.

---

### Task 3: sweep orchestrator backend (sweep.py + cli extractions + endpoints)

**cli.py extractions (binding, extraction-only, suite green proves byte-identical CLI behavior):**
- `compute_remaining(runs_root, task_docs, models, config_name, repeat) -> dict[(task_name, model), int]` — the shortfall logic currently inlined in `run` (including the without-`-n` = exactly-one rule); `run` delegates to it.
- `grade_pending(runs_root, grader_name, grader, grader_path, echo) -> dict` — the grading loop currently inlined in `grade` (skip failed runs, skip up-to-date grades, count stale), returning `{graded, skipped, stale, failed_runs, failures}`; `grade` delegates and keeps its exact echo output.

**sweep.py interfaces (binding):**
- `lms_path() -> Path | None` — `lms` on PATH, else `~/.lmstudio/bin/lms`, else None.
- `lms_inventory() -> set[str]` — model ids from `lms ls` (best-effort scrape, 5s timeout, empty set on any failure). Local vs hosted detection: present in inventory → local; else hosted.
- `lms_unload_all()`, `lms_load(model, context_length) -> (ok, message)` — wrapping `lms unload --all` and `lms load <model> -c <ctx> -y` (default context_length **32768** — the JIT-8192 empty-output lesson from the shell sweeps, kept as a spec'd constant with that comment).
- `SweepSpec` (a plain dict, validated): `{evals: [slug], models: [str], n: int, graders: {slug: {grader_name: "inline"|"deferred"|"skip"}}, context_length: int}`. Default grader treatment computed server-side and returned by the plan endpoint: a grader is `"deferred"` when any of its checks carries a `model` key (the judge pattern in the bundled examples), else `"inline"`; the UI may override.
- `run_sweep(job, spec, evals, hooks)` — the orchestrator, run in a daemon thread, updating `job` in place under the jobs lock. Execution order **exactly the shell sweeps'**: for each model (spec order): if local → `lms_unload_all()` then `lms_load(model, ctx)` (load failure marks every cell for that model failed with the message, continue to next model); for each selected eval: acquire the eval's `active_jobs` slot (waiting politely if a bench job holds it), top up via `compute_remaining` + `execute_run` one run at a time — releasing and re-acquiring the slot around each run — grading each successful run inline with every `"inline"` grader via `grade_run`; after all models: `lms_unload_all()`, then the deferred pass — `grade_pending` for every `"deferred"` grader per eval. Failed runs are recorded and excluded, a failing pair never halts the sweep (mirror `run`'s failure accounting). Cancel: a `cancel_requested` flag checked between runs (and between deferred grader passes) — never mid-run; status becomes `"cancelled"`.
- Sweep job shape (extends the phase-1 job table; one reserved active-sweep slot beside `active_jobs`): `{id, kind: "sweep", status: queued|running|done|failed|cancelled, started, spec, cells: {"<slug>\u0000<model>": {target, have, failed, done, mean, state: queued|loading|running|grading|done|failed}}, current: {eval, model, task} | null, deferred: [{eval, grader, state}], log: [tail], error, cancel_requested}`. `have`/`mean` update after every run from the same counting rules as `count_existing_runs`/`group_stats`.
- studio.py endpoints: `GET /api/sweep` → the active-or-most-recent sweep job (or `{active: false}`); `POST /api/sweep` (body = spec; validates slugs/models/n, computes cells and grader defaults, 409 if one is active) → 202 + job; `POST /api/sweep/cancel` → sets the flag; `GET /api/sweep/plan` (body-less, query `?evals=&models=&n=`) → per-cell shortfall preview + default grader treatments (what the compose form shows before starting); `GET /api/models` extended to `{models: [...], local: [...]}` (union gains `lms_inventory()`, cached like `llm_models`).

**Steps:**
- [ ] Failing tests first: extraction tests (existing `run`/`grade` behavior unchanged — the current suite is the proof, plus direct unit tests of the two new functions); sweep lifecycle against the server fixture with the stub runner and a fake `lms` script on PATH recording its argv — assert: model-major order, `unload --all` before each local load, `-c 32768` passed, hosted model (absent from fake inventory) triggers no lms calls, inline grades exist per run, deferred grader graded only in the final pass after the last model, shortfall honored (pre-seeded runs reduce executed count — the resumability proof), cancel stops after the in-flight run, second sweep 409s, bench run during a held eval 409s with the spec'd message, sweep waits for a bench job to finish, failing runner marks the cell but the sweep completes.
- [ ] Implement sweep.py + studio.py wiring + cli.py extractions → green → Black → commit.

---

### Task 4: Sweeps surface (compose + live board) in studio.html

**Rails:** router grows `#/sweep`; a `Sweeps` entry in the header/nav beside `Results`. **Compose:** eval multi-select defaulting to all; model multi-select fed by `/api/models` (`local` entries badged "local · LM Studio", hosted unbadged) plus a free-text add; target n per task×model (default 5); grader treatment table from `/api/sweep/plan` (inline/deferred/skip per grader per eval, judge-pattern graders pre-set to deferred) plus the shortfall preview ("this sweep will execute ~N runs"). The composition persists to localStorage on every change; after a studio restart the form is pre-filled and a hint explains resumability: "already-recorded runs count — starting again only runs the shortfall." **Board:** the eval × model matrix, each cell showing state color (tokens only: accent for running/loading, pass-green for done, fail-red for failed), `have/target`, failed count, and mean-so-far; a current-run line (eval / model / task) with the job log tail (monospace, like the bench job log); deferred-pass rows; a Cancel button ("stops after the current run — never kills a runner mid-run"). Poll `/api/sweep` at ~1s while active; set `state.sweepActive` so Task 2's results ticker runs and results land live as the sweep progresses. Bench surfaces render the 409 sweep message inline when they receive it. Empty state teaches what a sweep is and that CLI sweeps remain for headless use.

**Steps:**
- [ ] Build against a live backend with the stub runner + fake lms suite; drive a real small sweep and watch cells advance, results strip update, cancel behave.
- [ ] Smoke cross-grep green (new endpoints referenced exist).
- [ ] Screenshots: compose form and mid-sweep board, both color schemes → commit.

---

### Task 5: inline env-var annotation (spec section C)

**Rails (Jesse's verbatim intent — the form stops duplicating values into the mirror):** in the task form, each field's label row carries its env var inline in the mono face: name → `SMEVALS_TASK` (and `SMEVALS_TASK_NAME`), prompt → `SMEVALS_PROMPT`, and every add-any-key/extra field → `SMEVALS_TASK_<KEY>` computed live from the key as typed (the `envKey` transform already in the pure block; the add-key input previews its env name as you type). The context panel's `renderTaskMirror` env block — the value-duplicating part — is removed; the panel keeps, compacted: the config/model picker driving `SMEVALS_MODEL` (which also feeds the run form), the `SMEVALS_RUN_DIR` shape line, the one-paragraph Runner-contract teaching text, and the Run-this-task action unchanged. Same treatment in the grader form: each check-config field in `checksEditor` annotated `→ SMEVALS_CHECK_<KEY>` (scalar keys only — mirroring `scalar_env_vars`'s rule; non-scalar values get no annotation, matching what the checker actually receives). Raw view untouched. The pure block's `envMirror` shrinks or retargets to serve the annotations; whatever survives stays covered by the studio_pure corpus (update `cases.json` — the corpus runner executes the same pure code Python-side tests assert on).

**Steps:**
- [ ] Update tests/studio_pure/cases.json first for the changed/new pure helpers (env-key derivation incl. non-alnum keys, scalar-only rule) → implement → corpus green.
- [ ] Build; verify live: typing a new key shows its env name before the field exists on disk; SMEVALS_MODEL still tracks the picker; grader check fields annotated.
- [ ] Screenshots of the task form and grader form with annotations, both schemes → commit.

---

### Task 6: e2e scenarios, polish, docs, final verification

**Steps:**
- [ ] **e2e-scenario-testing skill**: author and execute scenario cards against a fresh `smevals studio` on a scratch suite with the stub runner + fake lms. Cards (falsifiable, per surface): compose a 2-eval × 2-model × n=2 sweep and watch the board complete with correct cell counts; interrupt studio mid-sweep, restart, recompose from the pre-filled form, and verify only the shortfall executes; cancel stops after the in-flight run; bench run during the sweep's held eval shows the 409 message; deferred judge grader grades only at the end; Results tab leaderboard/tags/stale badge match `smevals report` for the same eval; global matrix highlights the best model and marks the incomplete cell; shelf strip shows best score; task form shows `SMEVALS_TASK_<KEY>` live for a newly typed key; workbench open triggers no `/runs` fetch. Cards + outcomes in the report.
- [ ] frontend-design-skill-guided polish pass over the three new surfaces (rhythm, focus order, Escape/keyboard behavior consistent with phase 1, reduced-motion respect).
- [ ] README: extend the `smevals studio` section (~6 lines) with results + sweeps; verify `smevals docs` carries it; note that the standalone reporter script is superseded.
- [ ] Full suite green; run studio over `examples/` and screenshot shelf, Results tab, global matrix, and the sweep board for the report, both schemes; `git status --short` clean → commit. (The tutorial movie of the integrated app is a controller-owned follow-on once this plan lands — noted here so "done means" stays honest.)

---

## Deviations from prior practice

None structural — same rails-based creative tasks (2, 4, 5) as phase 1, with the spec's surfaces + screenshots + e2e cards carrying acceptance. The one scope call made here (spec left it open): resumability is top-up semantics + client-side composition memory, not server-side sweep persistence — zero new files on disk, and the orchestrator's shortfall math is the same code `smevals run -n` uses, so "resume" is literally "start again."

---

### Critical Files for Implementation

- /Users/jesse/git/prime-radiant/smevals/src/smevals/studio.py
- /Users/jesse/git/prime-radiant/smevals/src/smevals/studio.html
- /Users/jesse/git/prime-radiant/smevals/src/smevals/site.py
- /Users/jesse/git/prime-radiant/smevals/src/smevals/cli.py
- /Users/jesse/git/prime-radiant/smevals/tests/test_studio.py
