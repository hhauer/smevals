# smevals studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `smevals studio [DIR]` — a local, gorgeous, easy-to-use authoring environment: shelf (browse/create evals), workbench (schema-aware dual YAML editors with live validation), bench (run one task, grade one run, dry-run grader edits against existing runs).

**Architecture:** stdlib HTTP server (pattern of site.py) + one self-contained studio.html; pure authoring logic in a new authoring.py; run/grade execution reuses cli.py internals in-process. No new dependencies, no build step, 127.0.0.1 only.

**Tech Stack:** Python 3.10+ stdlib + pyyaml + click (existing); vanilla JS/CSS in one HTML file (conventions of app.html: hash routing, esc(), polling).

**Spec (binding; read the relevant sections before each task):** `docs/superpowers/specs/2026-08-02-smevals-studio-design.md`

## Global Constraints

- WIP branch `smevals-studio` (created from main AFTER the code-quality-evals merge lands; the SDD controller handles branch creation).
- TDD throughout; the repo's no-mock rule: endpoint tests hit a real server on an ephemeral port over real tmp files; runner-dependent tests use a real stub executable honoring the documented Runner contract.
- Black-formatted Python; match site.py/app.html conventions (comment style, esc-everything, stdlib-only).
- studio NEVER writes under runs/ except via the real run code path; grader dry-runs grade into a TemporaryDirectory.
- Path traversal: every file API resolves against the eval dir and rejects escapes (mirror site.py's guard, see serve_eval).
- All API responses JSON; errors {"error": msg}; writes atomic (tmp+rename); PUT carries base content sha256, mismatch → 409 {"error", "theirs", "ours"}.
- The frontend-design skill's guidance governs studio.html's visual work; the spec's "design bar" section is the acceptance bar (calm workbench, one accent hue, both color schemes, empty states that teach).
- cli.py refactors: extraction only (e.g. give execute_run an echo=False mode); every existing test stays green; no behavior change to the CLI surface.

## File Map

```
src/smevals/authoring.py        # Task 1: scaffold_eval, validate_eval, FILE_SCHEMAS
src/smevals/studio.py           # Tasks 2-3: handler, job table, API
src/smevals/studio.html         # Tasks 4-6: the app (shelf → workbench → bench)
src/smevals/cli.py              # Task 2: studio command + small extractions
src/smevals/starter-run-llm     # Task 1: starter runner copied into scaffolds
tests/test_authoring.py         # Task 1
tests/test_studio.py            # Tasks 2-3, 6
```

---

### Task 1: authoring.py — scaffold + validation (pure logic)

**Interfaces produced (binding):**
- `scaffold_eval(parent: Path, name: str, description: str) -> Path` — creates `<parent>/<slug>/` with eval.yaml (name, description), tasks/example.yaml (name: example, prompt placeholder), configs/default.yaml (name, runner: ../run-llm, model: gpt-4.1-mini), a copied+chmod'd run-llm (from src/smevals/starter-run-llm, the guarded llm-CLI runner — copy examples/code-review/run-llm's content into the new starter file), graders/default.yaml (contains check, value: "", required true, pass_threshold 1.0), .gitignore (`runs`). Slug: lowercase, spaces→-, strip non [a-z0-9-]. Raises ValueError on existing dir or empty name.
- `validate_eval(eval_dir: Path) -> list[dict]` — records {"file": rel path, "path": dotted key path or "", "problem": str}. Checks (each its own small function): YAML parse errors; eval.yaml requires name; tasks require name (prompt recommended → problem severity is still a flat list, prefix "warning: " for recommendations); configs require name+runner, runner path must resolve relative to the config file and be executable; graders require name+checks, each check's checker must be a built-in name (contains, xml-valid) or a resolving executable path; scoring.pass_threshold numeric 0..1 when present; duplicate names across files of one kind.
- `FILE_SCHEMAS: dict` — per kind ("task"|"config"|"grader"|"eval") an ordered field table: [{key, label, kind: "str"|"text"|"number"|"path"|"checks", required, help}] driving the form view; arbitrary extra keys always permitted.

**Steps:** failing pytest first (scaffold shape incl. executable bit + gitignore; slugging; each validation problem kind via fixture evals in tmp_path; FILE_SCHEMAS completeness vs the README's documented keys) → implement → green → Black → commit.

---

### Task 2: studio server skeleton + read APIs + CLI command

**Interfaces produced:** `studio.py: run_studio(root: Path, port: int)`; cli command `studio` (`smevals studio [DIR] [-p PORT]`, host hardcoded 127.0.0.1, help text mirroring serve's style). Endpoints: GET / → studio.html; GET /api/evals → [{slug, name, description, counts: {tasks, configs, graders}, last_run_iso|null, problems: int}] (reuse site.py's discovery: a non-eval dir is a Suite searched recursively; reuse cached_yaml); GET /api/evals/<slug> → {files: tree grouped by kind, validation: validate_eval output}; GET /api/evals/<slug>/file?path= → {content, sha256, executable: bool} (traversal-guarded).

**Steps:** failing endpoint tests first (server fixture: ephemeral port, tmp suite with two scaffolded evals; requests via urllib) covering discovery, file read, traversal rejection (../, absolute, symlink escape), 404s → implement (Handler subclass in the site.py idiom) → green → wire cli command (+ its --help output asserted in one test) → Black → commit.

---

### Task 3: write + execute APIs (the bench backend)

**Interfaces produced:**
- POST /api/evals (body {name, description, parent?}) → scaffold via authoring.py, response includes new slug + validation.
- PUT /api/evals/<slug>/file {path, content, base_sha256, executable?} → atomic write; 409 on hash mismatch with {"theirs", "ours"}; response {sha256, validation} (validation is the whole eval's, recomputed).
- POST /api/evals/<slug>/run {task, config, model} → spawns the real single-run path (`execute_run`-equivalent extracted from cli.py with echo suppressed) in a thread; registers job {id, kind: "run", status: queued|running|done|failed, run_dir?, error?, log: tail}; 409 if the eval already has an active job.
- POST /api/evals/<slug>/grade {run, grader} → real grading of one run dir (persists a Grade, exactly as CLI would).
- POST /api/evals/<slug>/dryrun {run, grader_yaml} → parse grader_yaml from the request (unsaved editor state), grade the run into a TemporaryDirectory using the same check-execution internals, respond when done (dry-runs are quick: run them synchronously, no job) with {checks: [...], score, outcome, tags, artifacts: names only} and persist NOTHING.
- GET /api/jobs/<id> → job state.
- GET /api/evals/<slug>/runs → the eval's runs (task, config, model, timestamp, ok, grades: {grader: outcome/score}) — reuse collect_eval.
- GET /api/models → best-effort model suggestions: the union of configs' models + parsed `llm models list --json` if the command succeeds within 2s, else just configs' models. Cache for the process lifetime.

**cli.py extraction (binding):** refactor the single-run execution and single-run grading paths so studio can call them in-process without click.echo side effects — extract-and-delegate only; CLI behavior byte-identical; full suite green proves it.

**Steps:** failing tests first — stub runner script writes "STUB OUTPUT" + a log file; tests: scaffold POST; PUT roundtrip + 409 conflict + traversal reject; run job lifecycle to done with run.yaml on disk (poll the job endpoint); grade persists grade.yaml; dryrun returns results AND leaves runs/ untouched (assert no new grades/<name>/ dir appears under the run and the temp workspace is cleaned); active-job 409 → implement → green → Black → commit.

---

### Task 4: studio.html — shell + shelf

**Rails:** one file, inline CSS/JS, hash routing (#/ shelf, #/eval/<slug> workbench), fetch-based API layer with a single error-rendering helper, esc() discipline throughout. Visual identity per the spec's design bar: define the palette (one accent + neutrals + semantic pass/fail), type scale, spacing tokens as CSS custom properties with prefers-color-scheme variants FIRST — every later surface uses the tokens. Shelf: responsive card grid (name, description, counts, relative last-run time, problems badge), New-eval card → inline form (name, description) → POST → navigate to workbench. Empty states: no evals found (teach: what an eval is, one command to scaffold); eval with problems (badge links into workbench).

**Steps:** build; verify against the live backend with a scratch suite (chromium-cli or manual curl+screenshot per the run skill's browser pattern — screenshots in the report); the pytest smoke (studio.html serves; every /api/ path string in the JS exists in studio.py — a simple cross-grep test) → commit.

---

### Task 5: workbench — tree, dual editors, validation, context mirror

**Rails:** three panes (tree / editor / context). Tree groups by kind with problem dots. YAML docs: form view generated from FILE_SCHEMAS (add-any-key affordance; checks get an ordered sub-editor: add/remove/reorder checks, checker picker offering built-ins + discovered checker files) ⇄ raw view (textarea with line numbers and problem gutter); both views round-trip through the same text state; switching never loses edits; save = PUT with base sha; 409 renders reload-or-overwrite choice with a diff-ish side-by-side (plain text panes, no diff lib). Executables: read-only-safe code editor (plain textarea, monospace), executable-bit indicator + make-executable action (PUT executable flag). Context panel: for a selected task, the live env-var mirror (SMEVALS_MODEL from chosen config, SMEVALS_PROMPT, SMEVALS_TASK_* derived from the actual parsed doc); for a grader, the numbered check pipeline with each check's config and resolution status. Unsaved-changes indicators; beforeunload guard.

**Steps:** build iteratively against live backend; screenshot both color schemes for the report; keep the smoke test's endpoint cross-check green → commit.

---

### Task 6: bench — run, grade, dry-run loop + e2e scenarios

**Rails:** context panel actions per spec: Run this task (config picker, model combo box fed by /api/models + free text; job progress inline with log tail; finished run rendered: output.txt, stderr if present, artifacts list, duration; grade-it button per grader). Runs list per eval (from /api/evals/<slug>/runs) with grade chips. Dry-run panel: grader editor state + run picker → side-by-side: dry result vs last persisted grade of that run (per-check rows: status, score, notes; tags; final score/outcome) with changed values highlighted. All long text esc()'d; failures render the API error inline.

**Steps:** build → **e2e-scenario-testing skill**: author and execute scenario cards against a fresh `smevals studio` on a scratch suite with the stub runner (cards: create eval; author task+grader via forms; run the task; grade it; edit grader rubric; dry-run shows the difference; conflict path: edit a file on disk mid-session and save → 409 flow). Cards + outcomes in the report → pytest suite green → commit.

---

### Task 7: polish pass + docs + final verification

**Steps:** frontend-design-skill-guided polish pass over all three surfaces (spacing/type rhythm, focus states, keyboard: cmd/ctrl-S saves, esc closes panels; reduced-motion respect); README gains a short `smevals studio` section under Commands (mirroring the other commands' style, ~6 lines); `smevals docs` therefore includes it (verify); full suite; run studio over examples/ and screenshot shelf + a workbench + a dry-run for the report; `git status --short` clean → commit.

---

## Deviations from prior practice

Rails-based creative tasks (4-6) like the code-quality plan; the visual
work cannot be fully pre-specified in a plan. The spec's design bar +
frontend-design skill + screenshot evidence in reports + the final
review carry the "gorgeous" acceptance instead.
