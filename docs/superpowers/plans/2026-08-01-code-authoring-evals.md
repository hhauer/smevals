# Code-Authoring Evals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two new example evals (`interval-set`, `usage-billing`) that measure writing TypeScript from a high-quality spec, graded by a deterministic hidden test suite plus a side-by-side LLM code-quality judge.

**Architecture:** A `code-authoring` Suite directory under `examples/` shares one runner and four checkers between the two evals. The Runner is the standard `llm`-CLI script. Each eval's `default` grader runs extract → tsc → data-driven case harness; a `judge` grader runs extract → gpt-4.1 quality judge. Reference and planted-bug solutions live per eval and are exercised by the repo's pytest suite.

**Tech Stack:** Python 3.10+ (checkers, pytest), Node ≥ 24 (runs `.ts` directly via type stripping), TypeScript 5.9 via `npx` (type-check gate only), `llm` CLI (runner and judge).

**Spec:** `docs/superpowers/specs/2026-08-01-code-authoring-evals-design.md`

## Global Constraints

- All work on a WIP branch `code-authoring-evals`; commit after every task.
- Node-dependent tests must `pytest.mark.skipif` when `node` is not on PATH; the pure-Python suite must stay green without node.
- TypeScript version is pinned to `5.9` in grader config, never hardcoded in checkers.
- Checkers follow the documented Checker contract exactly: JSON on stdout with only `score`/`metrics`/`tags`/`notes`/`details`; exit 0 = pass.
- Python files are formatted with Black (the repo standard); match the comment style of the existing example checkers.
- Never mock: pytest runs the real checker executables as subprocesses with real env vars; only the LLM judge goes untested by pytest.
- Case-group names are lower_snake_case; a failed group produces tag `fails_<group>`.
- The model-facing spec lives ONLY in `tasks/*.yaml`; nothing under `reference/` or `tests/` may be mentioned in the prompt.

## File Map

```
examples/code-authoring/
├── run-llm                          # Task 1
├── checkers/
│   ├── extract-ts                   # Task 1
│   ├── tsc-check                    # Task 2
│   ├── run-tests                    # Task 3
│   ├── ts-case-harness.mjs          # Task 3 (helper, not a Checker itself)
│   └── llm-judge-code               # Task 6
├── interval-set/
│   ├── .gitignore                   # Task 4
│   ├── eval.yaml                    # Task 4
│   ├── tasks/interval-set.yaml      # Task 4
│   ├── configs/default.yaml         # Task 4
│   ├── graders/default.yaml         # Task 4
│   ├── graders/judge.yaml           # Task 6
│   ├── tests/cases.ts               # Task 4
│   └── reference/
│       ├── solution.ts              # Task 4
│       ├── bug-merges-open-touching.ts   # Task 4
│       └── bug-remove-keeps-boundary.ts  # Task 4
└── usage-billing/
    ├── .gitignore                   # Task 5
    ├── eval.yaml                    # Task 5
    ├── tasks/usage-billing.yaml     # Task 5
    ├── configs/default.yaml         # Task 5
    ├── graders/default.yaml         # Task 5
    ├── graders/judge.yaml           # Task 6
    ├── tests/cases.ts               # Task 5
    └── reference/
        ├── solution.ts              # Task 5
        ├── bug-exclusive-tier-bound.ts   # Task 5
        └── bug-credits-before-tax.ts     # Task 5
tests/test_example_code_authoring.py # Tasks 1-5 grow it
```

---

### Task 1: Branch, suite scaffolding, and the `extract-ts` checker

**Files:**
- Create: `examples/code-authoring/run-llm`
- Create: `examples/code-authoring/checkers/extract-ts`
- Create: `tests/test_example_code_authoring.py`

**Interfaces:**
- Produces: `extract-ts` reads `$SMEVALS_RUN_DIR/output.txt`, writes the extracted module to `$SMEVALS_CHECK_CREATES` (default `solution.ts`) in the cwd, prints `{"notes": ...}`, exits 0 unless the output is empty. Later tasks rely on the helper `run_checker(checker_name, cwd, run_dir, check)` defined in the test file.

- [ ] **Step 1: Create the WIP branch**

```bash
git checkout -b code-authoring-evals
```

- [ ] **Step 2: Write failing tests for extract-ts**

Create `tests/test_example_code_authoring.py`:

```python
"""Tests for the code-authoring example evals: checkers, harness, fixtures.

These run the real checker executables as subprocesses, exactly as
smevals grade would. Anything needing node skips when node is absent.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

SUITE = pathlib.Path(__file__).parent.parent / "examples" / "code-authoring"
CHECKERS = SUITE / "checkers"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH"
)


def run_checker(name, cwd, run_dir, check=None, task=None):
    """Execute a Checker per the documented contract; return (proc, result).

    result is the parsed JSON from stdout, or None if stdout was empty.
    """
    check = {"checker": name, **(check or {})}
    env = os.environ | {
        "SMEVALS_RUN_DIR": str(run_dir),
        "SMEVALS_CHECK": json.dumps(check),
        "SMEVALS_TASK": (task or {}).get("name", "test-task"),
    }
    for key, value in check.items():
        if isinstance(value, (str, int, float, bool)):
            env[f"SMEVALS_CHECK_{key.upper()}"] = str(value)
    proc = subprocess.run(
        [str(CHECKERS / name)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    result = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc, result


def make_run(tmp_path, output):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.txt").write_text(output)
    workspace = tmp_path / "grade"
    workspace.mkdir()
    return run_dir, workspace


def test_extract_ts_takes_last_ts_fence(tmp_path):
    run_dir, ws = make_run(
        tmp_path,
        "Here is a sketch:\n```ts\nconst draft = 1;\n```\n"
        "And the final version:\n```typescript\nexport const x: number = 2;\n```\nDone.",
    )
    proc, result = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode == 0
    assert (ws / "solution.ts").read_text().strip() == "export const x: number = 2;"
    assert "extracted" in result["notes"]


def test_extract_ts_falls_back_to_any_fence(tmp_path):
    run_dir, ws = make_run(tmp_path, "```\nexport const y = 3;\n```")
    proc, _ = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode == 0
    assert "y = 3" in (ws / "solution.ts").read_text()


def test_extract_ts_falls_back_to_whole_output(tmp_path):
    run_dir, ws = make_run(tmp_path, "export const z = 4;\n")
    proc, _ = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode == 0
    assert "z = 4" in (ws / "solution.ts").read_text()


def test_extract_ts_fails_on_empty_output(tmp_path):
    run_dir, ws = make_run(tmp_path, "   \n")
    proc, _ = run_checker("extract-ts", ws, run_dir)
    assert proc.returncode != 0


def test_extract_ts_honors_creates(tmp_path):
    run_dir, ws = make_run(tmp_path, "```ts\nexport const q = 5;\n```")
    proc, _ = run_checker("extract-ts", ws, run_dir, check={"creates": "code.ts"})
    assert proc.returncode == 0
    assert (ws / "code.ts").exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_example_code_authoring.py -v`
Expected: FAIL (checker executable does not exist → FileNotFoundError)

- [ ] **Step 4: Write run-llm and extract-ts**

Create `examples/code-authoring/run-llm` (identical contract to the other examples):

```bash
#!/usr/bin/env bash
# Runner contract:
#   inputs:  $SMEVALS_MODEL, $SMEVALS_PROMPT, $SMEVALS_TASK, $SMEVALS_RUN_DIR
#   cwd:     the run directory - files written here are kept as artifacts
#   stdout:  the model's response (saved by smevals as output.txt)
#   exit:    non-zero marks the Run as failed
set -euo pipefail

llm -m "$SMEVALS_MODEL" "$SMEVALS_PROMPT"
llm logs -c --json > log.json
```

Create `examples/code-authoring/checkers/extract-ts`:

```python
#!/usr/bin/env python3
# Checker contract:
#   inputs:  $SMEVALS_RUN_DIR, $SMEVALS_CHECK (JSON), $SMEVALS_CHECK_* per config key
#   cwd:     the grade workspace - files written here are kept as artifacts
#   stdout:  optional JSON with extra result fields
#   exit:    0 = check passed, non-zero = failed
#
# Pulls the TypeScript module out of the model's response. Prefers the
# last ```ts/```typescript fence (models often restate fragments before
# the final module), then the last fence of any language, then the raw
# output. Strict validation is tsc-check's job.
import json
import os
import pathlib
import re
import sys

text = (pathlib.Path(os.environ["SMEVALS_RUN_DIR"]) / "output.txt").read_text()
target = os.environ.get("SMEVALS_CHECK_CREATES", "solution.ts")

blocks = re.findall(r"```(?:typescript|ts)[ \t]*\n(.*?)```", text, re.DOTALL)
source = "typescript fence"
if not blocks:
    blocks = re.findall(r"```[\w-]*[ \t]*\n(.*?)```", text, re.DOTALL)
    source = "generic fence"
code = blocks[-1] if blocks else text
if not blocks:
    source = "raw output"
if not code.strip():
    sys.exit("no code found in output.txt")

pathlib.Path(target).write_text(code)
print(json.dumps({"notes": f"extracted {len(code)} bytes from {source}"}))
```

```bash
chmod +x examples/code-authoring/run-llm examples/code-authoring/checkers/extract-ts
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_example_code_authoring.py -v`
Expected: 5 PASS

- [ ] **Step 6: Commit**

```bash
git add examples/code-authoring tests/test_example_code_authoring.py
git commit -m "code-authoring suite: runner and extract-ts checker"
```

---

### Task 2: The `tsc-check` checker

**Files:**
- Create: `examples/code-authoring/checkers/tsc-check`
- Modify: `tests/test_example_code_authoring.py` (append)

**Interfaces:**
- Consumes: a `solution.ts` (or `$SMEVALS_CHECK_FILE`) in the workspace.
- Produces: exit 0 iff `npx -y typescript@$SMEVALS_CHECK_TYPESCRIPT_VERSION tsc --strict --noEmit --target es2022` accepts the file; notes carry the first five compiler errors.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_example_code_authoring.py`:

```python
GOOD_TS = "export function double(x: number): number {\n  return x * 2;\n}\n"
BAD_TS = 'export function double(x: number): number {\n  return "nope";\n}\n'


@requires_node
def test_tsc_check_passes_valid_ts(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(GOOD_TS)
    proc, result = run_checker(
        "tsc-check", ws, run_dir, check={"typescript_version": "5.9"}
    )
    assert proc.returncode == 0, proc.stderr
    assert "5.9" in result["notes"]


@requires_node
def test_tsc_check_fails_type_error_with_notes(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(BAD_TS)
    proc, result = run_checker(
        "tsc-check", ws, run_dir, check={"typescript_version": "5.9"}
    )
    assert proc.returncode != 0
    assert "TS2322" in result["notes"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_example_code_authoring.py -k tsc -v`
Expected: FAIL (FileNotFoundError for the checker)

- [ ] **Step 3: Write tsc-check**

Create `examples/code-authoring/checkers/tsc-check`:

```python
#!/usr/bin/env python3
# Type-checks the extracted module with a pinned TypeScript version so
# the Grader snapshot fully determines behavior. Failing tsc fails the
# Check; the first five compiler errors land in notes.
import json
import os
import subprocess
import sys

version = os.environ["SMEVALS_CHECK_TYPESCRIPT_VERSION"]
file = os.environ.get("SMEVALS_CHECK_FILE", "solution.ts")

proc = subprocess.run(
    [
        "npx",
        "-y",
        f"typescript@{version}",
        "tsc",
        "--strict",
        "--noEmit",
        "--target",
        "es2022",
        file,
    ],
    capture_output=True,
    text=True,
)
if proc.returncode == 0:
    print(json.dumps({"notes": f"tsc {version} --strict: clean"}))
    sys.exit(0)

errors = [line for line in proc.stdout.splitlines() if "error TS" in line]
shown = "\n".join(errors[:5]) or proc.stdout.strip() or proc.stderr.strip()
print(
    json.dumps(
        {
            "notes": f"tsc {version} --strict: {len(errors)} error(s)\n{shown}",
            "metrics": {"tsc_errors": len(errors)},
        }
    )
)
sys.exit(1)
```

```bash
chmod +x examples/code-authoring/checkers/tsc-check
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_example_code_authoring.py -k tsc -v`
Expected: 2 PASS (first run downloads typescript@5.9 into the npx cache; allow a minute)

- [ ] **Step 5: Commit**

```bash
git add examples/code-authoring/checkers/tsc-check tests/test_example_code_authoring.py
git commit -m "code-authoring suite: tsc-check checker"
```

---

### Task 3: The `run-tests` checker and case harness

**Files:**
- Create: `examples/code-authoring/checkers/run-tests`
- Create: `examples/code-authoring/checkers/ts-case-harness.mjs`
- Modify: `tests/test_example_code_authoring.py` (append)

**Interfaces:**
- Consumes: `solution.ts` in the workspace; `$SMEVALS_CHECK_CASES`, a path **relative to the suite root** (the checker's own parent-of-parent), e.g. `interval-set/tests/cases.ts`.
- Produces: the smevals result JSON. `score` = passed/total. `metrics`: one boolean-rate key per case group plus `cases_passed`/`cases_total`. `tags`: `fails_<group>` for each group with a failure, `throws_at_runtime` if any case threw, `import_error` if the solution failed to import, `timeout` if the harness was killed. `details.failures`: first five failures with `{group, name, expected, got}` or `{group, name, error}`.
- Case file contract (used by Tasks 4 and 5): a `.ts` module exporting `cases: {group: string; name: string; run: (m: any) => unknown; expect: unknown}[]`. `run` receives the imported solution module and returns a JSON-comparable value.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_example_code_authoring.py`. The toy fixtures live in the test's tmp dir; `cases` paths escape the suite root via `..`, which the checker must allow (it resolves, it does not sandbox):

```python
TOY_CASES = """
export const cases = [
  { group: "math", name: "doubles", run: (m: any) => m.double(2), expect: 4 },
  { group: "math", name: "zero", run: (m: any) => m.double(0), expect: 0 },
  { group: "shape", name: "object", run: (m: any) => ({ v: m.double(3) }), expect: { v: 6 } },
];
"""


def toy_check(tmp_path, cases_src=TOY_CASES):
    (tmp_path / "cases.ts").write_text(cases_src)
    rel = os.path.relpath(tmp_path / "cases.ts", SUITE)
    return {"cases": rel}


@requires_node
def test_run_tests_all_pass(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(GOOD_TS)
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert result["score"] == 1.0
    assert result["metrics"]["math"] is True
    assert result["metrics"]["cases_total"] == 3
    assert result["tags"] == []


@requires_node
def test_run_tests_partial_credit_and_group_tags(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(
        "export function double(x: number): number { return x === 0 ? 1 : x * 2; }\n"
    )
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode != 0
    assert result["score"] == pytest.approx(2 / 3)
    assert result["metrics"]["math"] is False
    assert result["metrics"]["shape"] is True
    assert "fails_math" in result["tags"]
    failure = result["details"]["failures"][0]
    assert failure["name"] == "zero" and failure["expected"] == 0 and failure["got"] == 1


@requires_node
def test_run_tests_throwing_solution(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text(
        'export function double(x: number): number { throw new Error("boom"); }\n'
    )
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode != 0
    assert result["score"] == 0.0
    assert "throws_at_runtime" in result["tags"]
    assert "boom" in result["details"]["failures"][0]["error"]


@requires_node
def test_run_tests_unimportable_solution(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    (ws / "solution.ts").write_text("export const = broken syntax(((\n")
    proc, result = run_checker("run-tests", ws, run_dir, check=toy_check(tmp_path))
    assert proc.returncode != 0
    assert result["score"] == 0.0
    assert "import_error" in result["tags"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_example_code_authoring.py -k run_tests -v`
Expected: FAIL (FileNotFoundError for the checker)

- [ ] **Step 3: Write the harness**

Create `examples/code-authoring/checkers/ts-case-harness.mjs`:

```js
// Child process of the run-tests checker. Imports a cases module and a
// solution module (node strips TS types natively), runs every case, and
// emits one NDJSON line per case so the parent can salvage partial
// results if it has to kill us on timeout.
import { pathToFileURL } from "node:url";

const [casesPath, solutionPath] = process.argv.slice(2);
const emit = (obj) => console.log(JSON.stringify(obj));

function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === "object")
    return Object.fromEntries(
      Object.keys(v)
        .sort()
        .map((k) => [k, sortKeys(v[k])])
    );
  return v;
}
const canon = (v) => JSON.stringify(sortKeys(v));

const { cases } = await import(pathToFileURL(casesPath));
let solution;
try {
  solution = await import(pathToFileURL(solutionPath));
} catch (err) {
  emit({ importError: String(err) });
  process.exit(0);
}

for (const c of cases) {
  try {
    const got = c.run(solution);
    if (canon(got) === canon(c.expect)) {
      emit({ group: c.group, name: c.name, pass: true });
    } else {
      emit({ group: c.group, name: c.name, pass: false, expected: c.expect, got });
    }
  } catch (err) {
    emit({ group: c.group, name: c.name, pass: false, error: String(err) });
  }
}
```

- [ ] **Step 4: Write the run-tests checker**

Create `examples/code-authoring/checkers/run-tests`:

```js
#!/usr/bin/env node
// Runs the eval's hidden data-driven cases against the extracted
// solution. Score is the fraction of cases passing; metrics get one
// boolean per case group; groups with failures become fails_<group>
// tags. The harness runs in a child process with an overall timeout so
// a hanging solution cannot hang the Grader.
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const casesPath = path.resolve(here, "..", process.env.SMEVALS_CHECK_CASES);
const solutionPath = path.resolve(process.env.SMEVALS_CHECK_SOLUTION ?? "solution.ts");
const timeoutMs = Number(process.env.SMEVALS_CHECK_TIMEOUT_MS ?? 30000);

const child = spawnSync(
  process.execPath,
  [path.join(here, "ts-case-harness.mjs"), casesPath, solutionPath],
  { timeout: timeoutMs, encoding: "utf8" }
);

const events = (child.stdout ?? "")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line));

const tags = [];
const timedOut = child.signal === "SIGTERM" || child.signal === "SIGKILL";
if (timedOut) tags.push("timeout");
const importError = events.find((e) => e.importError);
if (importError) tags.push("import_error");
if (!timedOut && !importError && child.status !== 0) {
  // The harness itself crashed: surface it as a failed check with notes.
  console.log(
    JSON.stringify({ score: 0.0, notes: `harness crashed: ${child.stderr.slice(0, 500)}` })
  );
  process.exit(1);
}

const results = events.filter((e) => e.group);
const groups = {};
for (const r of results) {
  (groups[r.group] ??= []).push(r);
}
const passed = results.filter((r) => r.pass).length;
// Timed-out runs are scored over the cases that got a verdict; an
// import error means zero cases ran, which scores 0 below.
const total = importError ? 1 : results.length || 1;

const metrics = { cases_passed: passed, cases_total: results.length };
for (const [group, rs] of Object.entries(groups)) {
  const ok = rs.every((r) => r.pass);
  metrics[group] = ok;
  if (!ok) tags.push(`fails_${group}`);
}
if (results.some((r) => r.error)) tags.push("throws_at_runtime");

const failures = results
  .filter((r) => !r.pass)
  .slice(0, 5)
  .map(({ pass, ...rest }) => rest);
if (importError) failures.unshift({ error: importError.importError });

const score = passed / total;
const allPass = !importError && !timedOut && results.length > 0 && passed === results.length;
console.log(
  JSON.stringify({
    score,
    metrics,
    tags,
    notes: `${passed}/${results.length} cases passed`,
    details: { failures },
  })
);
process.exit(allPass ? 0 : 1);
```

```bash
chmod +x examples/code-authoring/checkers/run-tests
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_example_code_authoring.py -k run_tests -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add examples/code-authoring/checkers tests/test_example_code_authoring.py
git commit -m "code-authoring suite: data-driven run-tests checker and harness"
```

---

### Task 4: The interval-set eval

**Files:**
- Create: `examples/code-authoring/interval-set/.gitignore` (content: `runs`)
- Create: `examples/code-authoring/interval-set/eval.yaml`
- Create: `examples/code-authoring/interval-set/tasks/interval-set.yaml`
- Create: `examples/code-authoring/interval-set/configs/default.yaml`
- Create: `examples/code-authoring/interval-set/graders/default.yaml`
- Create: `examples/code-authoring/interval-set/tests/cases.ts`
- Create: `examples/code-authoring/interval-set/reference/solution.ts`
- Create: `examples/code-authoring/interval-set/reference/bug-merges-open-touching.ts`
- Create: `examples/code-authoring/interval-set/reference/bug-remove-keeps-boundary.ts`
- Modify: `tests/test_example_code_authoring.py` (append)

**Interfaces:**
- Consumes: the Task 3 case-file contract and `run_checker` helper.
- Produces: the `Interval` wire shape used by cases and reference: `{lo: number | null, hi: number | null, loOpen: boolean, hiOpen: boolean}` (null = unbounded, unbounded sides always open). Module exports `class IntervalSet` with `constructor(intervals?: Interval[])`, `add(iv)`, `remove(iv)`, `contains(x): boolean`, `intersects(iv): boolean`, `spans(): Interval[]`, `union(other): IntervalSet`, `intersection(other): IntervalSet`.

- [ ] **Step 1: Write failing pytest fixtures test**

Append to `tests/test_example_code_authoring.py`:

```python
def fixture_check(eval_name):
    return {"cases": f"{eval_name}/tests/cases.ts"}


def grade_fixture(tmp_path, eval_name, solution_filename):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / eval_name / "reference" / solution_filename
    (ws / "solution.ts").write_text(src.read_text())
    return run_checker("run-tests", ws, run_dir, check=fixture_check(eval_name))


@requires_node
def test_interval_set_reference_scores_1(tmp_path):
    proc, result = grade_fixture(tmp_path, "interval-set", "solution.ts")
    assert result["score"] == 1.0, result["details"]
    assert proc.returncode == 0


@requires_node
def test_interval_set_reference_typechecks(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / "interval-set" / "reference" / "solution.ts"
    (ws / "solution.ts").write_text(src.read_text())
    proc, _ = run_checker(
        "tsc-check", ws, run_dir, check={"typescript_version": "5.9"}
    )
    assert proc.returncode == 0


@requires_node
def test_interval_set_bug_merges_open_touching(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "interval-set", "bug-merges-open-touching.ts"
    )
    assert result["score"] < 1.0
    assert "fails_no_merge_open_touching" in result["tags"]


@requires_node
def test_interval_set_bug_remove_keeps_boundary(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "interval-set", "bug-remove-keeps-boundary.ts"
    )
    assert result["score"] < 1.0
    assert "fails_remove_splitting" in result["tags"]
```

Run: `uv run pytest tests/test_example_code_authoring.py -k interval -v`
Expected: FAIL (fixtures do not exist)

- [ ] **Step 2: Write eval.yaml, config, default grader, .gitignore**

`examples/code-authoring/interval-set/eval.yaml`:

```yaml
name: interval-set
description: >-
  Implement an interval-set library in TypeScript from a dense,
  fully-specified spec. Edge-case minefield: mixed open/closed
  endpoints, merging, splitting on removal, degenerate and unbounded
  intervals. Graded by a hidden data-driven test suite plus a strict
  tsc gate; a separate judge grader scores code quality.
```

`examples/code-authoring/interval-set/configs/default.yaml`:

```yaml
name: default
runner: ../../run-llm
model: gpt-4.1-mini
```

`examples/code-authoring/interval-set/graders/default.yaml`:

```yaml
name: default
checks:
  # The final fenced module in the response becomes solution.ts
  - checker: ../../checkers/extract-ts
    creates: solution.ts
    required: true

  # Strict type-check with a pinned compiler; failing tsc fails the Grade
  - checker: ../../checkers/tsc-check
    typescript_version: "5.9"
    required: true

  # Hidden data-driven cases; score is the fraction passing
  - checker: ../../checkers/run-tests
    cases: interval-set/tests/cases.ts

scoring:
  pass_threshold: 1.0
```

`examples/code-authoring/interval-set/.gitignore`:

```
runs
```

- [ ] **Step 3: Write the task spec**

`examples/code-authoring/interval-set/tasks/interval-set.yaml` — the prompt is the complete model-facing spec:

```yaml
name: interval-set
prompt: |
  Implement a TypeScript module for sets of real numbers built from
  intervals with mixed open/closed endpoints.

  Reply with a single fenced TypeScript code block containing the
  complete module. It must compile under `tsc --strict` with no
  dependencies and no imports.

  ## Wire format

  Intervals cross the API as plain objects:

      type Interval = {
        lo: number | null;   // null means unbounded below
        hi: number | null;   // null means unbounded above
        loOpen: boolean;     // true: lo is excluded
        hiOpen: boolean;     // true: hi is excluded
      };

  An unbounded side is always open: treat `lo: null` as loOpen true and
  `hi: null` as hiOpen true regardless of the flag passed in.

  ## Exports

  Export the `Interval` type and a class `IntervalSet`:

  - `constructor(intervals?: Interval[])` - starts from the given
    intervals (added in order) or empty.
  - `add(iv: Interval): void`
  - `remove(iv: Interval): void`
  - `contains(x: number): boolean` - is the point x in the set?
  - `intersects(iv: Interval): boolean` - does iv share at least one
    point with the set?
  - `spans(): Interval[]` - the canonical representation, defined below.
  - `union(other: IntervalSet): IntervalSet` - a new set; neither
    operand is modified.
  - `intersection(other: IntervalSet): IntervalSet` - a new set;
    neither operand is modified.

  ## Semantics

  The set always behaves as the union of the intervals added minus the
  intervals removed, applied in call order. `spans()` returns the
  canonical form: sorted ascending, pairwise disjoint, and maximal -
  two intervals that could be merged into one must be merged.

  Merging rules follow from set semantics exactly:

  - `[1,2]` then `[2,3]` yields one span `[1,3]` (they share the
    point 2).
  - `[1,2)` then `[2,3]` yields one span `[1,3]` (2 is covered by the
    second interval and the reals in between by the first; no gap).
  - `[1,2)` then `(2,3]` yields two spans - the point 2 belongs to
    neither, so merging would wrongly add it.

  Removal splits: removing `(2,3)` from `[1,4]` leaves `[1,2]` and
  `[3,4]`. Removing `[2,3]` from `[1,4]` leaves `[1,2)` and `(3,4]`.

  Degenerate intervals: `[5,5]` is the single point 5. `(5,5)`,
  `[5,5)` and `(5,5]` are empty; adding or removing an empty interval
  is a no-op, and empty intervals never appear in `spans()`.

  Inputs may have `lo > hi`; such intervals are empty. Endpoint
  numbers are finite; unboundedness is expressed only via null.

  `contains` and `intersects` respect openness: for the set `{[1,2)}`,
  `contains(2)` is false, and `[2,3]` does not intersect the set - the
  point 2 is excluded from the set and no smaller point is in `[2,3]`.

  `spans()` output for unbounded sides uses `lo: null`/`hi: null` with
  the corresponding open flag true.
```

- [ ] **Step 4: Write the reference solution**

`examples/code-authoring/interval-set/reference/solution.ts`:

```ts
// Reference implementation for the interval-set eval. Never shown to
// models; pytest asserts it scores 1.0 under the hidden cases.
//
// Every bound maps to a cut point [value, side] ordered
// lexicographically. A start bound: closed [x -> [x,0], open (x ->
// [x,1]. An end bound: open x) -> [x,0], closed x] -> [x,1]. An
// interval is the half-open cut range [startKey, endKey) over this
// ordering, which makes empty/merge/split decisions exact comparisons.
export type Interval = {
  lo: number | null;
  hi: number | null;
  loOpen: boolean;
  hiOpen: boolean;
};

type Key = readonly [number, 0 | 1];

const startKey = (iv: Interval): Key =>
  iv.lo === null ? [-Infinity, 0] : [iv.lo, iv.loOpen ? 1 : 0];
const endKey = (iv: Interval): Key =>
  iv.hi === null ? [Infinity, 1] : [iv.hi, iv.hiOpen ? 0 : 1];
const cmp = (a: Key, b: Key): number =>
  a[0] !== b[0] ? (a[0] < b[0] ? -1 : 1) : a[1] - b[1];

type Span = { start: Key; end: Key };

const toSpan = (iv: Interval): Span | null => {
  const span = { start: startKey(iv), end: endKey(iv) };
  return cmp(span.start, span.end) < 0 ? span : null;
};

const toInterval = (s: Span): Interval => ({
  lo: s.start[0] === -Infinity ? null : s.start[0],
  hi: s.end[0] === Infinity ? null : s.end[0],
  loOpen: s.start[0] === -Infinity ? true : s.start[1] === 1,
  hiOpen: s.end[0] === Infinity ? true : s.end[1] === 0,
});

export class IntervalSet {
  private list: Span[] = [];

  constructor(intervals?: Interval[]) {
    for (const iv of intervals ?? []) this.add(iv);
  }

  add(iv: Interval): void {
    const span = toSpan(iv);
    if (!span) return;
    const merged: Span[] = [];
    let { start, end } = span;
    for (const s of this.list) {
      // Overlapping or touching spans coalesce: touching means one's
      // end cut equals the other's start cut.
      if (cmp(s.end, start) < 0 || cmp(end, s.start) < 0) {
        merged.push(s);
      } else {
        if (cmp(s.start, start) < 0) start = s.start;
        if (cmp(end, s.end) < 0) end = s.end;
      }
    }
    merged.push({ start, end });
    merged.sort((a, b) => cmp(a.start, b.start));
    this.list = merged;
  }

  remove(iv: Interval): void {
    const cut = toSpan(iv);
    if (!cut) return;
    const kept: Span[] = [];
    for (const s of this.list) {
      if (cmp(cut.start, s.start) > 0) {
        // Left remainder ends where the removal starts: flip the
        // removal's start cut into an end cut (same coordinate).
        kept.push({ start: s.start, end: min(cut.start, s.end) });
      }
      if (cmp(cut.end, s.end) < 0) {
        kept.push({ start: max(cut.end, s.start), end: s.end });
      }
    }
    this.list = kept.filter((s) => cmp(s.start, s.end) < 0);
  }

  contains(x: number): boolean {
    return this.intersects({ lo: x, hi: x, loOpen: false, hiOpen: false });
  }

  intersects(iv: Interval): boolean {
    const span = toSpan(iv);
    if (!span) return false;
    return this.list.some(
      (s) => cmp(s.start, span.end) < 0 && cmp(span.start, s.end) < 0
    );
  }

  spans(): Interval[] {
    return this.list.map(toInterval);
  }

  union(other: IntervalSet): IntervalSet {
    const result = new IntervalSet(this.spans());
    for (const iv of other.spans()) result.add(iv);
    return result;
  }

  intersection(other: IntervalSet): IntervalSet {
    const result = new IntervalSet();
    for (const a of this.list) {
      for (const b of other.list) {
        const start = max(a.start, b.start);
        const end = min(a.end, b.end);
        if (cmp(start, end) < 0) result.add(toInterval({ start, end }));
      }
    }
    return result;
  }
}

const min = (a: Key, b: Key): Key => (cmp(a, b) <= 0 ? a : b);
const max = (a: Key, b: Key): Key => (cmp(a, b) >= 0 ? a : b);
```

- [ ] **Step 5: Write the planted-bug fixtures**

`reference/bug-merges-open-touching.ts` is a copy of `solution.ts` with the `add` overlap test comparing coordinates only — the classic bug that merges `[1,2)` + `(2,3]`. Replace the condition inside `add`'s loop:

```ts
      if (s.end[0] < start[0] || end[0] < s.start[0]) {
```

(everything else identical to solution.ts; update the header comment to say `Planted bug: merges intervals that merely touch by coordinate, even when the shared point is excluded from both.`)

`reference/bug-remove-keeps-boundary.ts` is a copy of `solution.ts` where `remove` flips the cut inclusively — removing `[2,3]` from `[1,4]` wrongly leaves `[1,2]` and `[3,4]` (boundary points kept). Replace `remove`'s two `kept.push` lines:

```ts
        kept.push({ start: s.start, end: min([cut.start[0], 1], s.end) });
```
and
```ts
        kept.push({ start: max([cut.end[0], 0], s.start), end: s.end });
```

(header comment: `Planted bug: removal keeps the removed interval's closed boundary points in the set.`)

- [ ] **Step 6: Write the hidden cases**

`examples/code-authoring/interval-set/tests/cases.ts`:

```ts
// Hidden test cases for the interval-set eval. Grouped so that a
// failed group names the misunderstanding (fails_<group> tag).
type Iv = { lo: number | null; hi: number | null; loOpen: boolean; hiOpen: boolean };
const iv = (lo: number | null, hi: number | null, loOpen = false, hiOpen = false): Iv =>
  ({ lo, hi, loOpen, hiOpen });

export const cases = [
  {
    group: "basics",
    name: "single interval round-trips",
    run: (m: any) => new m.IntervalSet([iv(1, 2)]).spans(),
    expect: [iv(1, 2)],
  },
  {
    group: "basics",
    name: "disjoint intervals sort",
    run: (m: any) => new m.IntervalSet([iv(5, 6), iv(1, 2)]).spans(),
    expect: [iv(1, 2), iv(5, 6)],
  },
  {
    group: "basics",
    name: "overlap merges",
    run: (m: any) => new m.IntervalSet([iv(1, 3), iv(2, 5)]).spans(),
    expect: [iv(1, 5)],
  },
  {
    group: "merge_touching",
    name: "closed-closed touch merges",
    run: (m: any) => new m.IntervalSet([iv(1, 2), iv(2, 3)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "merge_touching",
    name: "half-open touch merges",
    run: (m: any) => new m.IntervalSet([iv(1, 2, false, true), iv(2, 3)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "merge_touching",
    name: "open meets closed start merges",
    run: (m: any) => new m.IntervalSet([iv(1, 2), iv(2, 3, true, false)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "no_merge_open_touching",
    name: "open-open touch stays split",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2, false, true), iv(2, 3, true, false)]).spans(),
    expect: [iv(1, 2, false, true), iv(2, 3, true, false)],
  },
  {
    group: "no_merge_open_touching",
    name: "excluded point is not contained",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2, false, true), iv(2, 3, true, false)]).contains(2),
    expect: false,
  },
  {
    group: "remove_splitting",
    name: "open removal leaves closed edges",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(1, 4)]);
      s.remove(iv(2, 3, true, true));
      return s.spans();
    },
    expect: [iv(1, 2), iv(3, 4)],
  },
  {
    group: "remove_splitting",
    name: "closed removal leaves open edges",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(1, 4)]);
      s.remove(iv(2, 3));
      return s.spans();
    },
    expect: [iv(1, 2, false, true), iv(3, 4, true, false)],
  },
  {
    group: "remove_splitting",
    name: "removing a point splits open",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(0, 10)]);
      s.remove(iv(5, 5));
      return s.spans();
    },
    expect: [iv(0, 5, false, true), iv(5, 10, true, false)],
  },
  {
    group: "degenerate",
    name: "point interval is one point",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(5, 5)]);
      return [s.contains(5), s.contains(5.0001), s.spans()];
    },
    expect: [true, false, [iv(5, 5)]],
  },
  {
    group: "degenerate",
    name: "empty forms vanish",
    run: (m: any) =>
      new m.IntervalSet([
        iv(5, 5, true, true),
        iv(5, 5, false, true),
        iv(5, 5, true, false),
        iv(7, 3),
      ]).spans(),
    expect: [],
  },
  {
    group: "degenerate",
    name: "point fills an open gap",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2, false, true), iv(2, 3, true, false), iv(2, 2)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "contains_intersects",
    name: "open boundary excluded",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(1, 2, true, true)]);
      return [s.contains(1), s.contains(1.5), s.contains(2)];
    },
    expect: [false, true, false],
  },
  {
    group: "contains_intersects",
    name: "touching closed intervals intersect",
    run: (m: any) => new m.IntervalSet([iv(1, 2)]).intersects(iv(2, 3)),
    expect: true,
  },
  {
    group: "contains_intersects",
    name: "touching at excluded point does not intersect",
    run: (m: any) => new m.IntervalSet([iv(1, 2, false, true)]).intersects(iv(2, 3)),
    expect: false,
  },
  {
    group: "unbounded",
    name: "lower ray contains everything below",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(null, 0, true, false)]);
      return [s.contains(-1e9), s.contains(0), s.contains(0.001)];
    },
    expect: [true, true, false],
  },
  {
    group: "unbounded",
    name: "rays merge into the full line",
    run: (m: any) =>
      new m.IntervalSet([iv(null, 0), iv(0, null)]).spans(),
    expect: [iv(null, null, true, true)],
  },
  {
    group: "unbounded",
    name: "unbounded flags normalize to open",
    run: (m: any) => new m.IntervalSet([iv(null, 5, false, false)]).spans(),
    expect: [iv(null, 5, true, false)],
  },
  {
    group: "set_ops",
    name: "union does not modify operands",
    run: (m: any) => {
      const a = new m.IntervalSet([iv(1, 2)]);
      const b = new m.IntervalSet([iv(3, 4)]);
      const u = a.union(b);
      return [u.spans(), a.spans(), b.spans()];
    },
    expect: [[iv(1, 2), iv(3, 4)], [iv(1, 2)], [iv(3, 4)]],
  },
  {
    group: "set_ops",
    name: "intersection respects open edges",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 3, false, true)])
        .intersection(new m.IntervalSet([iv(2, 4, true, false)]))
        .spans(),
    expect: [iv(2, 3, true, true)],
  },
  {
    group: "set_ops",
    name: "touching closed sets intersect in a point",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2)])
        .intersection(new m.IntervalSet([iv(2, 3)]))
        .spans(),
    expect: [iv(2, 2)],
  },
];
```

- [ ] **Step 7: Run the pytest suite**

Run: `uv run pytest tests/test_example_code_authoring.py -k interval -v`
Expected: 4 PASS. If the reference fails any case, debug the reference or the case — the pytest failure output includes `details.failures` with expected-vs-got.

- [ ] **Step 8: Run the whole test suite and commit**

Run: `uv run pytest`
Expected: all green (existing 88 + new).

```bash
git add examples/code-authoring/interval-set tests/test_example_code_authoring.py
git commit -m "interval-set example eval: spec, hidden cases, reference and bug fixtures"
```

---

### Task 5: The usage-billing eval

**Files:**
- Create: `examples/code-authoring/usage-billing/.gitignore` (content: `runs`)
- Create: `examples/code-authoring/usage-billing/eval.yaml`
- Create: `examples/code-authoring/usage-billing/tasks/usage-billing.yaml`
- Create: `examples/code-authoring/usage-billing/configs/default.yaml`
- Create: `examples/code-authoring/usage-billing/graders/default.yaml`
- Create: `examples/code-authoring/usage-billing/tests/cases.ts`
- Create: `examples/code-authoring/usage-billing/reference/solution.ts`
- Create: `examples/code-authoring/usage-billing/reference/bug-exclusive-tier-bound.ts`
- Create: `examples/code-authoring/usage-billing/reference/bug-credits-before-tax.ts`
- Modify: `tests/test_example_code_authoring.py` (append)

**Interfaces:**
- Consumes: Task 3's case-file contract; Task 4's `fixture_check`/`grade_fixture` helpers.
- Produces: module exporting `computeInvoice(input: InvoiceInput): Invoice` with the exact shapes given in the task spec below.

- [ ] **Step 1: Write failing pytest fixtures test**

Append to `tests/test_example_code_authoring.py`:

```python
@requires_node
def test_usage_billing_reference_scores_1(tmp_path):
    proc, result = grade_fixture(tmp_path, "usage-billing", "solution.ts")
    assert result["score"] == 1.0, result["details"]
    assert proc.returncode == 0


@requires_node
def test_usage_billing_reference_typechecks(tmp_path):
    run_dir, ws = make_run(tmp_path, "unused")
    src = SUITE / "usage-billing" / "reference" / "solution.ts"
    (ws / "solution.ts").write_text(src.read_text())
    proc, _ = run_checker(
        "tsc-check", ws, run_dir, check={"typescript_version": "5.9"}
    )
    assert proc.returncode == 0


@requires_node
def test_usage_billing_bug_exclusive_tier_bound(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "usage-billing", "bug-exclusive-tier-bound.ts"
    )
    assert result["score"] < 1.0
    assert "fails_tier_bounds" in result["tags"]


@requires_node
def test_usage_billing_bug_credits_before_tax(tmp_path):
    proc, result = grade_fixture(
        tmp_path, "usage-billing", "bug-credits-before-tax.ts"
    )
    assert result["score"] < 1.0
    assert "fails_credits" in result["tags"]
```

Run: `uv run pytest tests/test_example_code_authoring.py -k billing -v`
Expected: FAIL (fixtures do not exist)

- [ ] **Step 2: Write eval.yaml, config, default grader, .gitignore**

`examples/code-authoring/usage-billing/eval.yaml`:

```yaml
name: usage-billing
description: >-
  Implement an invoice calculator in TypeScript from a precise spec
  full of skim-punishers: inclusive tier bounds, calendar-day
  proration, rounding at exactly two points, boundary-exact period
  filtering, credits after tax in a specified order. Every hidden test
  case targets one rule, so per-group metrics name the rule a model
  skimmed.
```

`examples/code-authoring/usage-billing/configs/default.yaml`:

```yaml
name: default
runner: ../../run-llm
model: gpt-4.1-mini
```

`examples/code-authoring/usage-billing/graders/default.yaml`:

```yaml
name: default
checks:
  - checker: ../../checkers/extract-ts
    creates: solution.ts
    required: true
  - checker: ../../checkers/tsc-check
    typescript_version: "5.9"
    required: true
  - checker: ../../checkers/run-tests
    cases: usage-billing/tests/cases.ts
scoring:
  pass_threshold: 1.0
```

`.gitignore`: `runs`

- [ ] **Step 3: Write the task spec**

`examples/code-authoring/usage-billing/tasks/usage-billing.yaml`:

```yaml
name: usage-billing
prompt: |
  Implement a TypeScript module that computes an invoice for one
  billing period of a metered subscription.

  Reply with a single fenced TypeScript code block containing the
  complete module. It must compile under `tsc --strict` with no
  dependencies and no imports.

  ## Types and export

  Export exactly this function and these types:

      export type Tier = {
        upTo: number | null;     // cumulative units, inclusive; null = unlimited
        centsPerUnit: number;    // may be fractional, e.g. 0.25
      };

      export type Plan = {
        code: string;
        baseCents: number;       // integer cents per full period
        tiers: Tier[];           // ascending upTo, last is null
      };

      export type InvoiceInput = {
        periodStart: string;     // "YYYY-MM-DD", inclusive
        periodEnd: string;       // "YYYY-MM-DD", exclusive
        segments: {
          startsOn: string;      // "YYYY-MM-DD"
          plan: Plan;
        }[];                     // ascending startsOn; first <= periodStart
        usage: {
          at: string;            // ISO 8601 UTC, e.g. "2026-03-07T09:30:00Z"
          units: number;         // non-negative integer
        }[];
        credits: {
          cents: number;         // integer, positive
          expiresOn: string;     // "YYYY-MM-DD"
        }[];
        taxRate: number;         // e.g. 0.0875
      };

      export type Line = {
        kind: "base" | "usage";
        planCode: string;
        cents: number;           // integer
      };

      export type Invoice = {
        lines: Line[];
        subtotalCents: number;
        taxCents: number;
        totalCents: number;
        creditAppliedCents: number;
        amountDueCents: number;
      };

      export function computeInvoice(input: InvoiceInput): Invoice;

  ## Rules

  Dates are calendar days in UTC. A day D is "in the period" when
  periodStart <= D < periodEnd. The plan active on day D is the plan
  of the last segment whose startsOn is on or before D - so on the day
  a plan change takes effect, the NEW plan is active.

  Base fees are prorated by calendar days. For each segment that is
  active for at least one day of the period, emit one base line:
  baseCents multiplied by (days the segment is active in the period)
  divided by (total days in the period), rounded half-up to integer
  cents. Base lines appear in segment order.

  A usage record belongs to the period when periodStart <= at <
  periodEnd, comparing instants (periodStart and periodEnd are
  midnight UTC). A record at exactly midnight of periodEnd is out. A
  record is priced by the plan active on its calendar day.

  For each plan segment with at least one in-period usage record, emit
  one usage line after all base lines, in segment order. Price the
  segment's total units on the segment plan's graduated tiers: the
  first tier covers cumulative units up to AND INCLUDING its upTo, the
  next tier covers the units above that, and so on. Tier progression
  is tracked per segment - each segment's usage starts again at the
  first tier. Sum units-times-centsPerUnit across tiers exactly (no
  rounding), then round the segment's usage total half-up to integer
  cents. That per-line rounding and the tax rounding below are the
  ONLY two roundings anywhere.

  subtotalCents is the sum of all lines. taxCents is subtotalCents
  times taxRate, rounded half-up. totalCents = subtotalCents +
  taxCents.

  Credits apply AFTER tax. A credit is usable when its expiresOn is on
  or after periodEnd. Apply usable credits in order of earliest
  expiresOn first, breaking ties by larger cents first, each up to the
  remaining balance, stopping at zero. creditAppliedCents is the total
  applied; amountDueCents = totalCents - creditAppliedCents and is
  never negative.

  Round half-up means: 0.5 rounds away from zero (all amounts here are
  non-negative, so upward). Half-up rounding of x is floor(x + 0.5).
```

- [ ] **Step 4: Write the reference solution**

`examples/code-authoring/usage-billing/reference/solution.ts`:

```ts
// Reference implementation for the usage-billing eval. Never shown to
// models; pytest asserts it scores 1.0 under the hidden cases.
export type Tier = { upTo: number | null; centsPerUnit: number };
export type Plan = { code: string; baseCents: number; tiers: Tier[] };
export type InvoiceInput = {
  periodStart: string;
  periodEnd: string;
  segments: { startsOn: string; plan: Plan }[];
  usage: { at: string; units: number }[];
  credits: { cents: number; expiresOn: string }[];
  taxRate: number;
};
export type Line = { kind: "base" | "usage"; planCode: string; cents: number };
export type Invoice = {
  lines: Line[];
  subtotalCents: number;
  taxCents: number;
  totalCents: number;
  creditAppliedCents: number;
  amountDueCents: number;
};

const DAY_MS = 86_400_000;
const dayNumber = (isoDate: string): number => Date.parse(`${isoDate}T00:00:00Z`) / DAY_MS;
const halfUp = (x: number): number => Math.floor(x + 0.5);

export function computeInvoice(input: InvoiceInput): Invoice {
  const start = dayNumber(input.periodStart);
  const end = dayNumber(input.periodEnd);
  const totalDays = end - start;

  // Each segment's active day range within the period. The segment is
  // active from max(startsOn, periodStart) until the next segment
  // starts (the change day belongs to the new plan) or the period ends.
  const active = input.segments.map((seg, i) => {
    const from = Math.max(dayNumber(seg.startsOn), start);
    const next = input.segments[i + 1];
    const to = Math.min(next ? dayNumber(next.startsOn) : end, end);
    return { seg, from, to };
  });

  const lines: Line[] = [];
  for (const { seg, from, to } of active) {
    if (to <= from) continue;
    lines.push({
      kind: "base",
      planCode: seg.plan.code,
      cents: halfUp((seg.plan.baseCents * (to - from)) / totalDays),
    });
  }

  // Total in-period units per segment; instants compare against
  // midnight-UTC period bounds, calendar day picks the segment.
  const unitsBySegment = new Map<number, number>();
  for (const rec of input.usage) {
    const instant = Date.parse(rec.at);
    if (instant < start * DAY_MS || instant >= end * DAY_MS) continue;
    const day = Math.floor(instant / DAY_MS);
    const index = active.findIndex(({ from, to }) => from <= day && day < to);
    if (index === -1) continue;
    unitsBySegment.set(index, (unitsBySegment.get(index) ?? 0) + rec.units);
  }

  for (const [index, units] of [...unitsBySegment.entries()].sort((a, b) => a[0] - b[0])) {
    const { seg } = active[index];
    let remaining = units;
    let covered = 0;
    let exactCents = 0;
    for (const tier of seg.plan.tiers) {
      if (remaining <= 0) break;
      const capacity = tier.upTo === null ? Infinity : tier.upTo - covered;
      const inTier = Math.min(remaining, capacity);
      exactCents += inTier * tier.centsPerUnit;
      covered += inTier;
      remaining -= inTier;
    }
    lines.push({ kind: "usage", planCode: seg.plan.code, cents: halfUp(exactCents) });
  }

  const subtotalCents = lines.reduce((sum, line) => sum + line.cents, 0);
  const taxCents = halfUp(subtotalCents * input.taxRate);
  const totalCents = subtotalCents + taxCents;

  const usable = input.credits
    .filter((c) => dayNumber(c.expiresOn) >= end)
    .sort(
      (a, b) => dayNumber(a.expiresOn) - dayNumber(b.expiresOn) || b.cents - a.cents
    );
  let balance = totalCents;
  let creditAppliedCents = 0;
  for (const credit of usable) {
    const applied = Math.min(credit.cents, balance);
    creditAppliedCents += applied;
    balance -= applied;
    if (balance === 0) break;
  }

  return {
    lines,
    subtotalCents,
    taxCents,
    totalCents,
    creditAppliedCents,
    amountDueCents: balance,
  };
}
```

- [ ] **Step 5: Write the planted-bug fixtures**

`reference/bug-exclusive-tier-bound.ts`: copy of `solution.ts` with the tier capacity treating `upTo` as exclusive — in the tier loop replace:

```ts
      const capacity = tier.upTo === null ? Infinity : tier.upTo - covered;
```

with:

```ts
      const capacity = tier.upTo === null ? Infinity : tier.upTo - 1 - covered;
```

(header comment: `Planted bug: treats tier upTo as exclusive - the unit landing exactly on the bound is priced by the next tier.`)

`reference/bug-credits-before-tax.ts`: copy of `solution.ts` that applies credits to the subtotal and taxes the reduced amount. Replace everything from the `const subtotalCents` line to the return with:

```ts
  const subtotalCents = lines.reduce((sum, line) => sum + line.cents, 0);
  const usable = input.credits
    .filter((c) => dayNumber(c.expiresOn) >= end)
    .sort(
      (a, b) => dayNumber(a.expiresOn) - dayNumber(b.expiresOn) || b.cents - a.cents
    );
  let balance = subtotalCents;
  let creditAppliedCents = 0;
  for (const credit of usable) {
    const applied = Math.min(credit.cents, balance);
    creditAppliedCents += applied;
    balance -= applied;
    if (balance === 0) break;
  }
  const taxCents = halfUp(balance * input.taxRate);
  const totalCents = subtotalCents + taxCents;

  return {
    lines,
    subtotalCents,
    taxCents,
    totalCents,
    creditAppliedCents,
    amountDueCents: balance + taxCents,
  };
```

(header comment: `Planted bug: credits applied before tax, so tax is computed on the reduced balance.`)

- [ ] **Step 6: Write the hidden cases**

`examples/code-authoring/usage-billing/tests/cases.ts`:

```ts
// Hidden test cases for the usage-billing eval. Each group targets one
// spec rule; expected invoices are exact objects, worked by hand.
type Plan = { code: string; baseCents: number; tiers: { upTo: number | null; centsPerUnit: number }[] };

const FLAT: Plan = { code: "flat", baseCents: 1000, tiers: [{ upTo: null, centsPerUnit: 2 }] };
const TIERED: Plan = {
  code: "tiered",
  baseCents: 3000,
  tiers: [
    { upTo: 100, centsPerUnit: 0 },
    { upTo: 1000, centsPerUnit: 2.5 },
    { upTo: null, centsPerUnit: 1 },
  ],
};
const PRO: Plan = { code: "pro", baseCents: 6200, tiers: [{ upTo: null, centsPerUnit: 1 }] };

// A 30-day March-alike period used throughout: 2026-04-01 .. 2026-05-01.
const P = { periodStart: "2026-04-01", periodEnd: "2026-05-01" };
const seg = (startsOn: string, plan: Plan) => ({ startsOn, plan });
const rec = (at: string, units: number) => ({ at, units });

export const cases = [
  {
    group: "base_case",
    name: "single plan, flat usage, no credits",
    // base 1000; usage 50*2=100; subtotal 1100; tax 8.75% = 96.25 -> 96
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [rec("2026-04-10T12:00:00Z", 50)],
        credits: [],
        taxRate: 0.0875,
      }),
    expect: {
      lines: [
        { kind: "base", planCode: "flat", cents: 1000 },
        { kind: "usage", planCode: "flat", cents: 100 },
      ],
      subtotalCents: 1100,
      taxCents: 96,
      totalCents: 1196,
      creditAppliedCents: 0,
      amountDueCents: 1196,
    },
  },
  {
    group: "base_case",
    name: "no usage means no usage line",
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [],
        credits: [],
        taxRate: 0,
      }),
    expect: {
      lines: [{ kind: "base", planCode: "flat", cents: 1000 }],
      subtotalCents: 1000,
      taxCents: 0,
      totalCents: 1000,
      creditAppliedCents: 0,
      amountDueCents: 1000,
    },
  },
  {
    group: "tier_bounds",
    name: "unit exactly at upTo stays in the free tier",
    // 100 units: all inside upTo=100 at 0 cents.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 100)],
        credits: [],
        taxRate: 0,
      }).lines,
    expect: [
      { kind: "base", planCode: "tiered", cents: 3000 },
      { kind: "usage", planCode: "tiered", cents: 0 },
    ],
  },
  {
    group: "tier_bounds",
    name: "unit 101 is the first paid unit",
    // 101 units: 1 unit at 2.5 -> 2.5 -> rounds to 3.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 101)],
        credits: [],
        taxRate: 0,
      }).lines[1].cents,
    expect: 3,
  },
  {
    group: "tier_bounds",
    name: "cumulative bound at 1000 inclusive",
    // 1000 units: 900 paid at 2.5 = 2250 exactly; unit 1001 would hit tier 3.
    run: (m: any) => {
      const at1000 = m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 1000)],
        credits: [],
        taxRate: 0,
      }).lines[1].cents;
      const at1001 = m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 1001)],
        credits: [],
        taxRate: 0,
      }).lines[1].cents;
      return [at1000, at1001];
    },
    expect: [2250, 2251],
  },
  {
    group: "proration",
    name: "mid-period change bills change day to the new plan",
    // 30-day period. flat active Apr 1-15 (14 days), pro active Apr 15-May 1 (16 days).
    // base flat: 1000*14/30 = 466.67 -> 467; base pro: 6200*16/30 = 3306.67 -> 3307.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT), seg("2026-04-15", PRO)],
        usage: [],
        credits: [],
        taxRate: 0,
      }).lines,
    expect: [
      { kind: "base", planCode: "flat", cents: 467 },
      { kind: "base", planCode: "pro", cents: 3307 },
    ],
  },
  {
    group: "proration",
    name: "usage on the change day prices on the new plan",
    // 10 units on Apr 15: pro plan (1c/unit) -> 10, not flat (2c/unit) -> 20.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT), seg("2026-04-15", PRO)],
        usage: [rec("2026-04-15T08:00:00Z", 10)],
        credits: [],
        taxRate: 0,
      }).lines.filter((l: any) => l.kind === "usage"),
    expect: [{ kind: "usage", planCode: "pro", cents: 10 }],
  },
  {
    group: "proration",
    name: "tier progression restarts per segment",
    // 80 units under tiered before change, 80 after change back to tiered:
    // each segment prices 80 units in the free tier -> 0 + 0.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [
          seg("2026-04-01", TIERED),
          seg("2026-04-10", FLAT),
          seg("2026-04-20", TIERED),
        ],
        usage: [rec("2026-04-05T00:00:00Z", 80), rec("2026-04-25T00:00:00Z", 80)],
        credits: [],
        taxRate: 0,
      }).lines.filter((l: any) => l.kind === "usage"),
    expect: [
      { kind: "usage", planCode: "tiered", cents: 0 },
      { kind: "usage", planCode: "tiered", cents: 0 },
    ],
  },
  {
    group: "rounding",
    name: "sum exactly then round once",
    // 3 records of 1 unit at 0.25c: per-record rounding gives 0 or 1s;
    // correct is round(0.75) = 1.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [
          seg("2026-04-01", {
            code: "micro",
            baseCents: 0,
            tiers: [{ upTo: null, centsPerUnit: 0.25 }],
          }),
        ],
        usage: [
          rec("2026-04-02T00:00:00Z", 1),
          rec("2026-04-03T00:00:00Z", 1),
          rec("2026-04-04T00:00:00Z", 1),
        ],
        credits: [],
        taxRate: 0,
      }).lines[1].cents,
    expect: 1,
  },
  {
    group: "rounding",
    name: "half rounds up in tax",
    // subtotal 1000 with 4.45% tax: 44.5 -> 45.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [],
        credits: [],
        taxRate: 0.0445,
      }).taxCents,
    expect: 45,
  },
  {
    group: "period_boundaries",
    name: "record at period start is in, at period end is out",
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [
          rec("2026-04-01T00:00:00Z", 5),
          rec("2026-05-01T00:00:00Z", 7),
          rec("2026-03-31T23:59:59Z", 11),
        ],
        credits: [],
        taxRate: 0,
      }).lines,
    expect: [
      { kind: "base", planCode: "flat", cents: 1000 },
      { kind: "usage", planCode: "flat", cents: 10 },
    ],
  },
  {
    group: "credits",
    name: "expiry order then size, applied after tax",
    // total 1196 (from base_case). Credits: 500 exp 2026-06-01, 300 exp
    // 2026-05-01, 400 exp 2026-05-01, 900 exp 2026-04-30 (expired -> unusable).
    // Order: 400 (05-01), 300 (05-01), 500 (06-01) -> applies 400+300+496.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [rec("2026-04-10T12:00:00Z", 50)],
        credits: [
          { cents: 500, expiresOn: "2026-06-01" },
          { cents: 300, expiresOn: "2026-05-01" },
          { cents: 400, expiresOn: "2026-05-01" },
          { cents: 900, expiresOn: "2026-04-30" },
        ],
        taxRate: 0.0875,
      }),
    expect: {
      lines: [
        { kind: "base", planCode: "flat", cents: 1000 },
        { kind: "usage", planCode: "flat", cents: 100 },
      ],
      subtotalCents: 1100,
      taxCents: 96,
      totalCents: 1196,
      creditAppliedCents: 1196,
      amountDueCents: 0,
    },
  },
  {
    group: "credits",
    name: "amount due floors at zero and credit application stops",
    // total 1000; credits 800 + 800 -> apply 800 + 200, due 0.
    run: (m: any) => {
      const inv = m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [],
        credits: [
          { cents: 800, expiresOn: "2026-05-02" },
          { cents: 800, expiresOn: "2026-05-03" },
        ],
        taxRate: 0,
      });
      return [inv.creditAppliedCents, inv.amountDueCents];
    },
    expect: [1000, 0],
  },
];
```

- [ ] **Step 7: Run the pytest suite**

Run: `uv run pytest tests/test_example_code_authoring.py -k billing -v`
Expected: 4 PASS. Hand-verify any failure against the case comments — the arithmetic in each comment is the ground truth to re-derive.

- [ ] **Step 8: Run everything and commit**

Run: `uv run pytest`
Expected: all green.

```bash
git add examples/code-authoring/usage-billing tests/test_example_code_authoring.py
git commit -m "usage-billing example eval: spec, hidden cases, reference and bug fixtures"
```

---

### Task 6: The `llm-judge-code` checker and judge graders

**Files:**
- Create: `examples/code-authoring/checkers/llm-judge-code`
- Create: `examples/code-authoring/interval-set/graders/judge.yaml`
- Create: `examples/code-authoring/usage-billing/graders/judge.yaml`

**Interfaces:**
- Consumes: `solution.ts` in the workspace (from `extract-ts`); `$SMEVALS_CHECK_MODEL`, `$SMEVALS_CHECK_RUBRIC`, and the optional `tags` list from `$SMEVALS_CHECK`.
- Produces: score 0.0-1.0 (judge's 0-10 normalized), tags from the controlled vocabulary, notes, `details.raw_score`; writes `judge-log.json`.

No pytest for this checker (LLM-in-the-loop; the schema/normalization pattern is proven by the pelican image judge). Verification is the live smoke in Step 3.

- [ ] **Step 1: Write llm-judge-code**

Modeled directly on `examples/pelican-riding-a-bicycle/checkers/llm-judge-image`, with the code inlined in the prompt instead of an attachment:

```python
#!/usr/bin/env python3
"""Scores the extracted solution's code quality with an LLM via llm.
The judge scores 0-10 and applies tags from a controlled vocabulary;
results are normalized to the check result contract. Quality only -
correctness is the default grader's job, and the rubric says so.
"""
import json
import os
import pathlib
import subprocess
import sys

allowed_tags = json.loads(os.environ["SMEVALS_CHECK"]).get("tags")
if allowed_tags:
    tag_items = {"type": "string", "enum": allowed_tags}
    tag_description = "every tag from the allowed list that is true of the code"
else:
    tag_items = {"type": "string"}
    tag_description = "short snake_case tags for notable properties of the code"

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "the score, 0 to 10"},
        "notes": {
            "type": "string",
            "description": "short explanation of the scoring",
        },
        "tags": {
            "type": "array",
            "items": tag_items,
            "description": tag_description,
        },
    },
    "required": ["score", "notes", "tags"],
    "additionalProperties": False,
}

code = pathlib.Path(os.environ.get("SMEVALS_CHECK_FILE", "solution.ts")).read_text()
prompt = f"{os.environ['SMEVALS_CHECK_RUBRIC']}\n\n```typescript\n{code}\n```"

result = subprocess.run(
    [
        "llm",
        "-m",
        os.environ["SMEVALS_CHECK_MODEL"],
        "--schema",
        json.dumps(SCHEMA),
        prompt,
    ],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    sys.exit(result.stderr.strip() or "llm call failed")
judged = json.loads(result.stdout)

with open("judge-log.json", "w") as fp:
    subprocess.run(["llm", "logs", "-c", "--json"], stdout=fp, check=True)

print(
    json.dumps(
        {
            "score": judged["score"] / 10,
            "tags": judged.get("tags") or [],
            "notes": judged.get("notes", ""),
            "details": {"raw_score": judged["score"]},
        }
    )
)
```

```bash
chmod +x examples/code-authoring/checkers/llm-judge-code
```

- [ ] **Step 2: Write the two judge graders**

`examples/code-authoring/interval-set/graders/judge.yaml` (and identically for `usage-billing/graders/judge.yaml`):

```yaml
name: judge
checks:
  # Re-extract into this grader's own workspace
  - checker: ../../checkers/extract-ts
    creates: solution.ts
    required: true

  # Quality only; deliberately no tsc gate - a quality read on
  # non-type-checking code is interesting when comparing graders.
  - checker: ../../checkers/llm-judge-code
    model: gpt-4.1
    tags:
      - idiomatic_types
      - any_abuse
      - dead_code
      - clear_naming
      - over_engineered
    rubric: >-
      Score this TypeScript module's code quality from 0 to 10. Judge
      only quality - clarity, naming, idiomatic use of the type
      system, economy of implementation - NOT whether the algorithm is
      correct; correctness is measured elsewhere. Award up to 4 points
      for clear structure and naming, up to 3 points for idiomatic,
      precise types (no unnecessary any, no type assertions papering
      over design problems), and up to 3 points for economy - no dead
      code, no over-engineering. Apply every tag from the allowed list
      that is true of the code.
```

- [ ] **Step 3: Live smoke test**

Grade the reference solution through the full judge path by hand:

```bash
cd /tmp && mkdir -p judge-smoke && cd judge-smoke
export SMEVALS_RUN_DIR=$PWD
cp <repo>/examples/code-authoring/interval-set/reference/solution.ts output.txt
SMEVALS_CHECK='{"checker":"llm-judge-code","model":"gpt-4.1","rubric":"Score this TypeScript module 0 to 10 for code quality."}' \
SMEVALS_CHECK_MODEL=gpt-4.1 \
SMEVALS_CHECK_RUBRIC="Score this TypeScript module 0 to 10 for code quality." \
SMEVALS_CHECK_FILE=$PWD/output.txt \
<repo>/examples/code-authoring/checkers/llm-judge-code
```

Expected: JSON on stdout with score between 0.0 and 1.0 and non-empty notes; `judge-log.json` created. (Use the repo's absolute path for `<repo>`; run from a scratch dir so artifacts stay out of the repo.)

- [ ] **Step 4: Commit**

```bash
git add examples/code-authoring/checkers/llm-judge-code \
        examples/code-authoring/interval-set/graders/judge.yaml \
        examples/code-authoring/usage-billing/graders/judge.yaml
git commit -m "code-authoring suite: LLM code-quality judge grader"
```

---

### Task 7: End-to-end verification

**Files:** none created; this task proves the whole pipeline.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest`
Expected: all green.

- [ ] **Step 2: Run each eval once, graded, with the default config**

```bash
uv run smevals run examples/code-authoring/interval-set -g
uv run smevals run examples/code-authoring/usage-billing -g
```

Expected: each executes one Run with gpt-4.1-mini and grades it. Any outcome (pass or fail) is fine — gpt-4.1-mini failing hidden cases is signal, not error — but the Run must not be a *failed Run* (runner exit non-zero), extract-ts and tsc results must appear in the grade, and `runs/<task>/.../grades/default/grade.yaml` must contain a score, per-group metrics, and (on partial scores) `details.failures`.

Inspect: `cat examples/code-authoring/interval-set/runs/*/default/*/*/grades/default/grade.yaml`

- [ ] **Step 3: Judge graders end to end**

```bash
uv run smevals grade examples/code-authoring/interval-set -g judge
uv run smevals grade examples/code-authoring/usage-billing -g judge
```

Expected: each grades the existing Run; `grades/judge/grade.yaml` has a score and quality tags; `judge-log.json` sits next to it.

- [ ] **Step 4: Suite UI check**

```bash
uv run smevals serve examples
```

Expected: both new evals appear alongside the existing three; each eval page shows the run with both graders selectable; the default grader's metrics table shows the per-group booleans.

- [ ] **Step 5: Reports render**

```bash
uv run smevals report examples/code-authoring/interval-set
uv run smevals report examples/code-authoring/usage-billing
```

Expected: leaderboard with gpt-4.1-mini; per-group metrics aggregated as rates.

- [ ] **Step 6: Add the new evals to the sweep script**

Modify `eval-sweep.sh`: in the per-model loop, change the deterministic-eval list from

```bash
    for eval in haiku markdown-tables; do
```

to

```bash
    for eval in haiku markdown-tables code-authoring/interval-set code-authoring/usage-billing; do
```

(the new default graders are deterministic — tsc + node cases — so inline `-g` grading is fine), and append the two judge passes to the deferred-grading section, after the existing three `smevals grade` lines:

```bash
uv run smevals grade examples/code-authoring/interval-set -g judge || FAILURES=$((FAILURES+1))
uv run smevals grade examples/code-authoring/usage-billing -g judge || FAILURES=$((FAILURES+1))
```

Verify: `bash -n eval-sweep.sh` parses. (Running the six-model sweep itself happens after merge, on Jesse's go — local models tie up LM Studio for an hour-plus.)

```bash
git add eval-sweep.sh
git commit -m "eval-sweep: include the code-authoring evals"
```

- [ ] **Step 7: Commit any stragglers, then merge**

```bash
git status --short   # should show only ignored runs/ content
```

Use the superpowers:finishing-a-development-branch skill to merge `code-authoring-evals` back to main.

---

## Deviations from the spec (intentional, small)

- Per-case timeouts became one whole-harness timeout (30s, configurable via the check's `timeout_ms`) with partial-result salvage — synchronous TS cannot be interrupted per-case in-process, and a child process per case would be slow. Behavior under timeout: score counts only confirmed passes, tag `timeout`.
- The spec's example tags `merges_touching_open_intervals`/`rounds_per_record` are realized through the group-name convention as `fails_no_merge_open_touching` and `fails_rounding` — one mechanism instead of two.
```
