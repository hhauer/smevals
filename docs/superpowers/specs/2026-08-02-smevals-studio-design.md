# smevals studio: an interactive eval-authoring environment

2026-08-02. Jesse's standing directive: "build a full interactive
authoring environment for evals. use your best judgement. i want
something gorgeous and easy to use." Designed autonomously; every
question the brainstorming process would normally put to Jesse is
answered here with its reasoning, so he can veto any decision cheaply.

## Decisions made in Jesse's absence

1. **A web app, as a new `smevals studio` command**, not a TUI or
   desktop app. It extends the pattern `smevals serve` proves out:
   stdlib `http.server`, one self-contained HTML file, zero new
   dependencies, no build step. "Gorgeous" is achieved by design
   investment inside that constraint, not by adopting a framework -
   the project's whole ethos is plain files and a tiny dependency
   footprint (click + pyyaml), and a Vite/React toolchain would be the
   first build step in the repo.
2. **Studio and serve stay separate commands sharing one data layer.**
   serve is the read-only reporting surface you can expose or build to
   a static site; studio is a read-write local tool. Separation keeps
   serve's threat surface unchanged (build/serve never gain write
   endpoints) and keeps each page focused.
3. **Studio binds 127.0.0.1 only, no auth.** It writes files and
   executes runners; it must never be exposed. `--host` is
   deliberately not offered on studio.
4. **Scope: authoring + iteration, not run orchestration.** Studio
   creates and edits evals and offers a tight try-it loop (run ONE
   task, grade ONE run, dry-run a grader edit). Fleet sweeps stay in
   the CLI where they belong (they are long-running batch jobs; a
   browser tab is the wrong owner).
5. **Checker/runner scripts are edited as text with no execution
   sandbox.** They are the author's own executables, run exactly as
   the CLI would run them. Studio is a local single-user tool editing
   the author's own repo; sandboxing would be theater.
6. **Runs stay immutable.** Studio never writes inside `runs/` except
   by invoking the real `smevals run` code path. Grader dry-runs
   grade into a temporary directory and never persist a Grade.

## What studio is

`smevals studio [DIR]` (default `.`) serves a local single-page app
over a directory of Evals (Suite semantics identical to serve's
discovery). The app has three working surfaces:

### 1. The shelf (home)

Every eval in the directory as a card (name, description, task/config/
grader counts, last-run recency pulled from runs/), plus a "New eval"
card. Creating an eval scaffolds the canonical layout from the README
tutorial: eval.yaml, tasks/, configs/default.yaml pointing at a
starter run-llm (copied in, chmod +x), graders/default.yaml with a
`contains` placeholder check, .gitignore with `runs`. The form asks
only name + description; everything else is a sensible starter to edit.

### 2. The workbench (per eval)

A three-pane layout: file tree (tasks / configs / graders / checkers /
other files) on the left; an editor in the middle; a context panel on
the right.

- **YAML documents get a dual editor**: a schema-aware form view
  (fields for the known keys of tasks/configs/graders, plus an
  add-any-key affordance since Tasks deliberately allow arbitrary
  scalars) and a raw YAML view, toggled, always round-tripping through
  the same text. The form view is generated from a small schema table
  per file kind, not hardcoded per field.
- **Validation is live and honest**: on every edit, the backend
  re-parses and reports the same problems the CLI would hit (YAML
  syntax, missing required keys, runner/checker paths that do not
  resolve or are not executable, grader `checker:` names that are
  neither built-ins nor files). Problems render inline next to the
  field and as a gutter list in raw view.
- **Executables (runners/checkers) open in a plain code editor** with
  an executable-bit indicator and a "make executable" action.
- **The context panel** shows, for the selected Task: the exact env
  vars a Runner would receive (SMEVALS_PROMPT, SMEVALS_TASK_*), and
  for a Grader: the check pipeline as a numbered list with each
  Check's config. This panel is the "what will actually happen" mirror
  that makes the file formats learnable.

### 3. The bench (try-it loop)

The right-hand context panel grows actions:

- **Run this task**: pick a config and a model (model list from the
  config default, the `llm models` registry if available, and a free
  text field), then execute a real single run (`-t task`, n=1) via the
  same internals as the CLI. Progress streams (see Jobs below); the
  finished run appears with output.txt, stderr, artifacts, timing, and
  a grade-it button.
- **Grade this run**: apply any grader to any existing run for real.
- **Dry-run a grader**: the flagship authoring affordance. Grade any
  existing run with the CURRENT EDITOR STATE of a grader - including
  unsaved edits - into a temp workspace. Results (per-check status,
  score, tags, notes, artifacts listing) render side by side with the
  run's last persisted Grade, so editing a rubric or threshold becomes
  a tight see-the-difference loop. Nothing is written under runs/.

### Job model

Runs and grades execute server-side as subprocess jobs registered in
an in-memory table (id, kind, eval, status, started, log tail). The
frontend polls `/api/jobs/<id>` (the app already polls; SSE/websockets
would be the first streaming machinery in the codebase - not worth it
for v1). One job at a time per eval; queueing rejects with a clear
message rather than silently serializing.

## Architecture

```
src/smevals/
├── studio.py      # HTTP handler: API endpoints + job table + validation
├── studio.html    # the single-page app (like app.html: inline CSS/JS)
├── authoring.py   # pure logic: scaffold_eval, validate_eval, schema tables
└── cli.py         # + studio command (thin: parse args, call studio.run)
```

- `authoring.py` is import-clean pure functions over paths and dicts -
  fully unit-testable without HTTP. `validate_eval(eval_dir)` returns
  a list of `{file, path, problem}` records; `scaffold_eval(dir, name,
  description)` creates the layout and returns what it made.
- `studio.py` wires HTTP to authoring functions and to the existing
  internals in cli.py (`execute_run`, grading) - reusing them, not
  duplicating. Where cli.py functions need a small refactor to be
  callable in-process (e.g. echo-free variants), the refactor happens
  in cli.py with existing tests kept green.
- API (JSON): GET /api/evals; POST /api/evals (scaffold); GET/PUT
  /api/evals/<slug>/file?path= (read/write with validation response);
  POST .../run {task, config, model}; POST .../grade {run, grader};
  POST .../dryrun {run, grader_yaml}; GET /api/jobs/<id>. PUT rejects
  paths escaping the eval dir (same traversal guard serve uses) and
  writes atomically (tmp + rename).
- studio.html follows app.html's conventions (hash routing, `esc()`
  everywhere, polling) but with its own stylesheet and identity.

## The design bar ("gorgeous")

The frontend-design skill governs implementation; the spec pins the
direction: a calm workbench, not a dashboard. One accent hue with
semantic greens/reds reserved for pass/fail; generous whitespace;
system font stack with a monospace face for file content and env vars;
the three panes breathe (no borders-everywhere chrome); form and raw
views transition without layout jumps; empty states teach (a fresh
eval's workbench explains the Runner contract in one paragraph with
the env-var mirror live). Dark and light both first-class via
`prefers-color-scheme`. The bar: Jesse should want to screenshot it.

## Error handling

- All API errors are JSON {error} with useful messages; the app renders
  them inline where the action happened, never as alert().
- File conflicts: PUT carries the content hash it loaded; a mismatch
  (file changed on disk - Jesse edits in his editor too) returns 409
  with both versions and the app offers reload-or-overwrite.
- Job failures surface the subprocess's stderr tail directly.

## Testing

- `authoring.py`: pure pytest (scaffold shapes, validation cases -
  each problem kind has a fixture eval).
- `studio.py`: endpoint tests with a live server on an ephemeral port
  against tmp evals (the repo's existing test style: real files, real
  subprocesses, no mocks). Run/grade endpoints tested with a stub
  runner script (a real executable writing canned output - the
  documented Runner contract, not a mock of internals).
- Frontend: a pytest smoke asserts studio.html serves and references
  only endpoints that exist; interactive verification via the
  e2e-scenario-testing skill at the end (scenario cards: create an
  eval, edit a grader, dry-run it, run a task with the stub runner).

## Out of scope (v1)

Multi-user, remote exposure, sweep orchestration, run deletion or any
mutation under runs/, checker debugging/stepping, git integration
(Jesse has git), editing files outside the eval directory.

## Done means

`smevals studio examples` gives a shelf of the eight existing evals;
creating a scratch eval, authoring a task/config/grader through the
forms, running its task against a real model, and tightening the
grader via dry-run all work end to end; suite green; the UI clears the
design bar in both color schemes.
