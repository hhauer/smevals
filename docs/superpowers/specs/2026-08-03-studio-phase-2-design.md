# studio phase 2: results integration + sweep orchestrator

2026-08-03. Jesse's direction, given live: "integrate the studio and the
dashboards and also a tool to run the evals" — with two shape decisions
made by him directly: **one app** (studio absorbs the results
dashboards; `smevals serve`/`build` remain the read-only/static
publishing path) and a **full sweep orchestrator** (evals × models × n
matrix in the UI). This supersedes phase 1's decisions #2 and #4, by
the authority those decisions were always waiting on.

## A. Results integration (one app)

- Each eval's workbench gains a **Results** tab beside the file
  workbench: the serve dashboard content for that eval — grader picker,
  leaderboard (config × model, mean ± stderr, fails), tag shares,
  per-model metric blocks, runs drill-down reusing the bench's run
  detail. Data comes from the same collection layer serve uses
  (site.py's collect_eval / eval_summary via new read-only studio
  endpoints, or direct reuse — implementation's choice, but ONE
  aggregation code path shared with serve, never a fork).
- The shelf gains a compact results strip per card (best score + model
  from the default grader, matching serve's index summary) and a global
  **Results** view: the models × evals matrix (mean default-grader
  scores, best-per-eval highlighted) — the executive summary the
  scratchpad reporter prototype produces, now live. Adopt the
  reporter's aggregation semantics (failed-run exclusion, stale-grade
  annotation); the standalone reporter script is superseded once this
  lands but its verification corpus becomes test fixtures.
- Live like serve: re-read on poll while a sweep is running.

## B. Sweep orchestrator ("Sweeps" surface)

- A top-level Sweeps view: compose a sweep = pick evals (defaulting to
  all), models (multi-select from /api/models + LM Studio inventory via
  `lms ls` when available + free text), target n per task×model, and
  grader treatment (deterministic graders inline; judge graders as a
  deferred pass at the end — the pattern the shell sweeps proved).
- Execution model: ONE sweep active at a time, server-side, as a
  persistent job (extends the phase-1 job table): serial run execution
  in eval-major-within-model-major order (matching eval-sweep.sh:
  unload all → `lms load <model> -c 32768` for local models → all
  selected evals for that model → next model → judge passes). Local vs
  hosted model detection: present in LM Studio inventory → local;
  else hosted (no lms calls).
- smevals' top-up semantics make sweeps resumable: the orchestrator
  computes shortfall per pair exactly as `smevals run -n` does (reuse
  that code path in-process; no shelling out to the CLI).
- Progress: a live board — matrix cells (eval × model) showing
  queued/running/done with pass counts and mean score so far, a log
  tail for the current run, cancel button (cancel = stop after the
  in-flight run; never kill a runner mid-run in v1).
- Failure policy mirrors the CLI: failed Runs recorded and excluded,
  empty-response runs fail via the runner guard, a failing pair does
  not halt the sweep.
- The studio stays single-user local: one sweep, no scheduling, no
  distribution. The CLI sweeps remain for headless/scripted use.

## C. Task editor: inline env mirror (Jesse, verbatim intent)

"In the ui that lets you edit a task, integrate the content pane and
the 'what the tool receives' pane, instead of duplicating content,
show the env variable names inline in the main pane."

- The task form stops duplicating values into a side panel. Instead
  each field is annotated in place with the env var it becomes: the
  prompt field's label carries `→ SMEVALS_PROMPT`, the name field
  `→ SMEVALS_TASK` (and `SMEVALS_TASK_NAME`), every add-any-key field
  `→ SMEVALS_TASK_<KEY>` computed live from the key as typed.
- Context that is NOT duplication stays in the side panel, compacted:
  the config/model picker driving `SMEVALS_MODEL`, the `SMEVALS_RUN_DIR`
  shape, and the one-paragraph Runner-contract teaching text.
- Same treatment in the grader/check editor where it applies
  (`SMEVALS_CHECK_<KEY>` inline on check config fields).

## Constraints carried from phase 1

Zero new dependencies; one HTML file; loopback bind; runs/ immutable
except via the real run path; the job/lock discipline extends to sweep
jobs (a sweep holds the per-eval lock only around its current run so
single-run bench actions queue politely or 409 with a clear message —
decide in the plan and state it in the UI).

## Out of scope (unchanged)

Auth/multi-user, remote workers, run deletion, scheduling. serve/build
stay untouched apart from any shared-code extraction.

## Done means

Author → run matrix → watch results land → drill into failures, all in
one app; suite green; sweeps resumable after studio restart; the
tutorial movie records THIS integrated app.
