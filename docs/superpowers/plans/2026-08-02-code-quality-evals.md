# Code-Quality Evals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three new evals — code-review-defects (find a planted defect in a commit), config-lexer (edge-case-dense tokenizer authoring), refactor-preserve (refactor without changing behavior) — validated to the same bar as the code-authoring suite.

**Architecture:** code-review-defects is a standalone eval at `examples/code-review/` with three new checkers (parse-findings, match-findings, llm-judge-review). config-lexer and refactor-preserve join `examples/code-authoring/` and reuse its checkers wholesale. Every eval ships reference + adversarial fixtures proven by pytest to score exactly as designed.

**Tech Stack:** as the code-authoring suite (Python checkers, Node ≥ 24 type-stripping, tsc 5.9 via npx `--package` form, llm CLI).

**Spec:** `docs/superpowers/specs/2026-08-02-code-quality-evals-design.md` — read the relevant eval's section before implementing; it governs intent.

## Global Constraints

- WIP branch `code-quality-evals`; commit per task.
- Checker contract exactly as README documents (JSON keys score/metrics/tags/notes/details only; exit 0 = pass; cwd = grade workspace).
- Node-dependent pytest skips without node (`requires_node` marker in tests/test_example_code_authoring.py; code-review tests live in a new tests/test_example_code_review.py with the same helpers imported or duplicated minimally — duplicate the 3 small helpers, do not import across test modules).
- Black-formatted Python; TDD for every checker and every eval (fixture tests first).
- **Authored-content tasks (3, 5, 6) carry creative latitude within their stated rails.** Where this plan states behavior, values, names, groups, or scoring, those are binding. Where it says "author," the implementer writes original content meeting the stated properties, and the task reviewer independently verifies the properties (reference scores 1.0, each fixture fails exactly its target, every case value derivable from the prompt/module actually shipped).
- The model-facing prompt never mentions answers/, reference/, tests/.
- Case-file contract (authoring evals): export `cases: [{group, name, run(m), expect}]`; groups lower_snake_case; sync run functions; finite numbers or the documented sentinel behavior.

## File Map

```
examples/code-review/
├── run-llm                        # Task 1 (copy of suite runner)
├── checkers/parse-findings        # Task 1
├── checkers/match-findings        # Task 2
├── checkers/llm-judge-review      # Task 4
├── eval.yaml, configs/default.yaml, graders/{default,judge}.yaml, .gitignore   # Tasks 1/4
├── tasks/<six>.yaml               # Task 3
├── answers/<six>.yaml             # Task 3
└── reference/                     # Task 3: per task, reference/decoy/shotgun findings JSON
examples/code-authoring/config-lexer/       # Task 5 (full eval dir, sibling of interval-set)
examples/code-authoring/refactor-preserve/  # Task 6
tests/test_example_code_review.py           # Tasks 1-3
tests/test_example_code_authoring.py        # Tasks 5-6 append
sweep-code-authoring.sh                     # Task 7
```

---

### Task 1: code-review scaffolding and `parse-findings`

**Files:** Create `examples/code-review/{run-llm,eval.yaml,configs/default.yaml,.gitignore}`, `examples/code-review/checkers/parse-findings`, `tests/test_example_code_review.py`.

**Interfaces produced:** parse-findings reads `$SMEVALS_RUN_DIR/output.txt`, locates the findings object — last ```json fence, else last `{...}` block that parses and has a `findings` array — validates each finding has integer `line` ≥ 1 and non-empty string `description`, writes normalized JSON to `$SMEVALS_CHECK_CREATES` (default findings.json), notes how many findings; fails (exit≠0) on no parseable object or invalid shape. Test helpers `run_checker`/`make_run` duplicated from test_example_code_authoring.py with CHECKERS pointing at examples/code-review/checkers.

- [ ] Step 1: `git checkout -b code-quality-evals`
- [ ] Step 2 (TDD): tests — fenced JSON extracted; bare JSON extracted; prose-wrapped last-object wins; missing findings key fails; line 0 fails; empty output fails; creates: honored. Write, watch fail.
- [ ] Step 3: implement parse-findings:

```python
#!/usr/bin/env python3
# Checker contract: see suite siblings. Extracts the model's review
# findings JSON. Prefers the last ```json fence; falls back to the last
# top-level {...} slice that parses and carries a findings array.
# Validation is strict: a review that cannot be parsed cannot be graded.
import json
import os
import pathlib
import re
import sys

text = (pathlib.Path(os.environ["SMEVALS_RUN_DIR"]) / "output.txt").read_text()
target = os.environ.get("SMEVALS_CHECK_CREATES", "findings.json")

candidates = re.findall(r"```json[ \t]*\n(.*?)```", text, re.DOTALL)
if not candidates:
    # Last-resort: every top-level brace slice, right-to-left
    candidates = re.findall(r"\{.*\}", text, re.DOTALL)

obj = None
for chunk in reversed(candidates):
    try:
        parsed = json.loads(chunk)
    except json.JSONDecodeError:
        continue
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        obj = parsed
        break
if obj is None:
    sys.exit("no findings JSON object found in output.txt")

for finding in obj["findings"]:
    if (
        not isinstance(finding, dict)
        or not isinstance(finding.get("line"), int)
        or finding["line"] < 1
        or not isinstance(finding.get("description"), str)
        or not finding["description"].strip()
    ):
        sys.exit(f"malformed finding: {finding!r}")

pathlib.Path(target).write_text(json.dumps(obj, indent=2))
print(json.dumps({"notes": f"{len(obj['findings'])} finding(s) parsed"}))
```

The greedy `\{.*\}` fallback is deliberate (outermost braces of the last prose blob); tests pin the fenced path as primary. run-llm/eval.yaml/config/.gitignore mirror the code-authoring suite (`model: gpt-4.1-mini`; eval.yaml description: reviews a single commit to find one planted subtle defect).
- [ ] Step 4: pass, Black, commit.

---

### Task 2: `match-findings`

**Files:** Create `examples/code-review/checkers/match-findings`; append tests.

**Interfaces:** consumes findings.json (workspace) and `answers/$SMEVALS_TASK.yaml` resolved relative to the eval root (the checker's parent-of-parent). Answer file: `line: int`, `window: int` (± tolerance), `must_mention: [regex, ...]` (case-insensitive search; a description matches if ANY regex hits). Scoring, binding: planted defect is FOUND when some finding has |finding.line − line| ≤ window AND description matches → score 1.0, tag `found_planted`. Line-only match (window hit, no regex hit) → 0.5, tags `right_line_wrong_diagnosis`. Neither → 0.0, tag `missed_planted`. False positive = finding outside the window (description irrelevant). Metrics: `found_planted` bool, `false_positives` int, `findings_total` int. Extra tags: `noisy_review` when false_positives > 3, `silent_pass` when findings_total == 0. Exit 0 only on score 1.0. Multiple findings: best match wins; the others count toward false_positives only if outside the window.

- [ ] Step 1 (TDD): tests — exact-line correct-description 1.0/exit 0; window-edge line (line+window) still found; description matching second regex alternative found; right line generic description 0.5 with tag; all-wrong decoy 0.0 missed_planted with false_positives counted; empty findings silent_pass; >3 outliers noisy_review. Use a scratch answers file via a task named for a tmp answers dir — the checker must read the eval-root answers dir, so tests create `answers/test-task.yaml` under a tmp copy? No: simpler and binding — the checker resolves the answers dir from `$SMEVALS_CHECK_ANSWERS_DIR` when set (tests set it to tmp), else `<eval root>/answers`. Pin both paths in tests.
- [ ] Step 2: implement (complete):

```python
#!/usr/bin/env python3
# Grades a parsed review against the task's planted-defect answer key.
# Found = line within +/-window AND description matches one must_mention
# regex. Line-only match earns 0.5 (right place, wrong diagnosis).
import json
import os
import pathlib
import re
import sys

import yaml

here = pathlib.Path(__file__).resolve()
answers_dir = pathlib.Path(
    os.environ.get("SMEVALS_CHECK_ANSWERS_DIR", here.parent.parent / "answers")
)
answer = yaml.safe_load((answers_dir / f"{os.environ['SMEVALS_TASK']}.yaml").read_text())
findings = json.loads(pathlib.Path("findings.json").read_text())["findings"]

line, window = answer["line"], answer["window"]
patterns = [re.compile(p, re.IGNORECASE) for p in answer["must_mention"]]

best = 0.0
false_positives = 0
for finding in findings:
    if abs(finding["line"] - line) <= window:
        mentioned = any(p.search(finding["description"]) for p in patterns)
        best = max(best, 1.0 if mentioned else 0.5)
    else:
        false_positives += 1

tags = []
if best == 1.0:
    tags.append("found_planted")
elif best == 0.5:
    tags.append("right_line_wrong_diagnosis")
else:
    tags.append("missed_planted")
if false_positives > 3:
    tags.append("noisy_review")
if not findings:
    tags.append("silent_pass")

print(
    json.dumps(
        {
            "score": best,
            "metrics": {
                "found_planted": best == 1.0,
                "false_positives": false_positives,
                "findings_total": len(findings),
            },
            "tags": tags,
            "notes": f"planted@{line}±{window}: best={best}, {false_positives} false positive(s)",
        }
    )
)
sys.exit(0 if best == 1.0 else 1)
```

- [ ] Step 3: graders/default.yaml — parse-findings (required, creates findings.json) → match-findings; pass_threshold 1.0. Pass, Black, commit.

---

### Task 3: the six commits, answer keys, fixtures, validity tests

**Files:** Create `tasks/*.yaml` ×6, `answers/*.yaml` ×6, `reference/{reference,decoy,shotgun}-<task>.json` ×6; append tests.

**Rails (binding):** the six tasks and defect flavors are the spec's list (boundary-shift, stale-closure, float-money, mutated-default, sort-stability, swallowed-error). Each task YAML: `name`, `before` (complete working TS module, 40–80 lines, realistic domain, no two tasks sharing a domain), `diff` (unified diff, 15–40 changed lines, reads as one plausible refactor/feature commit, introduces EXACTLY one defect), `prompt` (uniform across tasks: you are reviewing this commit; here is the pre-change file and the diff; report findings as the exact JSON shape; report only real defects; line numbers refer to the post-change file). Answer window ≤ 3. `must_mention` regexes must be generous to phrasing but exclusive of generic filler — each list needs ≥3 alternatives, and the shotgun fixture's generic text ("this change may introduce a bug", "possible issue with logic") must NOT match any of them. Author the defects so the module still typechecks and superficial tests would pass (subtle by construction: the defect changes behavior only on a non-obvious path).

**Fixture contract (binding):** per task, reference JSON (one finding, right line, description a human reviewer would write naming the mechanism) scores 1.0; decoy (2–3 confident findings, all outside the window) scores 0.0 with missed_planted and false_positives ≥ 2; shotgun (a finding on every changed hunk including the defective line, all with generic descriptions drawn from the forbidden-filler list) scores exactly 0.5 with right_line_wrong_diagnosis, proving flag-everything doesn't win.

**Validity pytest (binding, write first):** parametrized over the six tasks: reference→1.0/exit 0; decoy→0.0+missed_planted; shotgun→0.5+right_line_wrong_diagnosis; every answers/*.yaml has window ≤ 3 and ≥ 3 must_mention entries; every task's `diff` applies cleanly to `before` (use difflib/patch verification in the test: apply the hunks and confirm the post file is consistent with the diff's own line numbers — a diff that doesn't apply is a broken task); the defective line named in answers falls inside a changed hunk.

- [ ] Steps: tests first (parametrized, will fail on missing files) → author all six task/answer/fixture sets → pytest green → self-check each diff by reading it as a reviewer (does exactly one defect exist? is anything else accidentally wrong?) → Black → commit.

---

### Task 4: `llm-judge-review`, judge grader, live smoke

**Files:** Create `examples/code-review/checkers/llm-judge-review`, `graders/judge.yaml`.

Mirror llm-judge-code's structure exactly (schema, tags-enum handling, normalization, judge-log.json, error path). Differences: prompt assembles three labeled sections — the commit diff (from `$SMEVALS_TASK_DIFF`), the model's findings.json (workspace), and the answer key (resolved as in match-findings, env override included); rubric (in judge.yaml): score 0–10 the review's explanation quality for the KNOWN planted defect — precision of location, correctness of mechanism, actionability; do not re-judge detection. Controlled tags: precise_location, correct_mechanism, suggests_fix, vague, wrong_mechanism. judge.yaml: parse-findings (required) → llm-judge-review (model gpt-4.1). No pytest; live smoke against the boundary-shift reference fixture (real gpt-4.1 call, from a scratch dir), actual JSON output captured in the report. Commit.

---

### Task 5: config-lexer eval

**Files:** full eval dir per the file map; append tests to tests/test_example_code_authoring.py.

**Rails (binding):** spec section "Eval B" governs the language: identifiers; integers with `_` separators and no leading zeros; double-quoted strings with `\n \t \\ \" \u{...}` escapes; single-quoted raw strings where `''` is the only escape; `#` line comments; NESTING `/* */` block comments; punctuation `= [ ] { } ,`; Token = `{kind, text, value, line, col}` (1-based, code-point columns, tab = 1 col, BOM skipped without affecting position); error tokens `{kind: "error", reason, line, col}` with reason from the closed enum (unterminated_string, bad_escape, unterminated_comment, bad_number) and the spec'd resynchronization (bad_escape resumes after the escape char; unterminated string/comment consume to EOF; bad_number consumes the alnum run). Whitespace and comments produce no tokens. `kind` enum: identifier, int, string, punct, error. The PROMPT must pin every one of these behaviors explicitly, with at least one worked example per tricky area (nested comment, `''` escape, `\u{1F600}` counting as one column, error recovery resuming). Case groups exactly: basics, numbers, string_escapes, raw_strings, nested_comments, positions, error_recovery, eof_edges; 20–28 cases; every expected value hand-derived in a comment. Fixtures: reference (score 1.0, tsc clean); bug-comments-dont-nest (depth-1 comment handling; must fail ONLY nested_comments); bug-col-counts-utf16 (UTF-16 code-unit columns; must fail ONLY positions — ensure at least one positions case puts an astral-plane char before a measured token). graders default+judge as interval-set's (judge rubric/tags identical). Validity pytest: reference 1.0 + typechecks; each fixture <1.0 failing exactly its group (assert the OTHER groups' metrics are all True).

- [ ] Steps: pytest first → prompt → reference (TDD against cases as they're authored) → cases → fixtures → full suite green → Black → commit.

---

### Task 6: refactor-preserve eval

**Files:** full eval dir; append tests.

**Rails (binding):** spec section "Eval C" governs. Author `legacy.ts` (~90 lines, shipping-cost calculator) with: three near-identical rate-table walk blocks (identical 8–10-line skeleton, different table/field names); stringly-typed region codes compared with raw string literals; a function taking 3+ boolean params; ≥1 dead branch; ≥2 misleading names; and the three quirks — a switch fall-through that double-applies a surcharge for one region pair, an order-dependent discount (percentage applied before flat rebate, so the order matters), and a NaN-propagating branch (missing weight yields NaN cost rather than a throw). The PROMPT embeds legacy.ts verbatim, pins the export surface (same function names/signatures), demands byte-identical observable behavior including the three quirks (pointed at by line, not explained), and asks for maximal quality improvement otherwise. Case groups exactly: core_paths (6+), quirk_fallthrough (2+), quirk_discount_order (2+), quirk_nan (2+), duplication (1: reads the solution source — `readFileSync(process.env.SMEVALS_SOLUTION ?? "solution.ts", "utf8")` from the case run fn, cwd is the workspace — normalizes whitespace, counts occurrences of the legacy walk skeleton's telltale normalized 4-line core, expects ≤ 1). run-tests must pass the resolved solution path to the harness env as SMEVALS_SOLUTION — add that one line to the run-tests checker (and a pytest for it) rather than hardcoding the filename in the case. Fixtures: reference (clean refactor, 1.0); bug-fixed-the-quirk (fixes the fall-through only; fails exactly quirk_fallthrough); legacy-verbatim (byte-copy of legacy.ts; passes all behavior groups, fails exactly duplication). Validity pytest mirrors Task 5's.

- [ ] Steps: pytest first → legacy.ts + prompt → cases (hand-derive expected costs; show arithmetic in comments) → reference → fixtures → full suite green (including the SMEVALS_SOLUTION run-tests addition, TDD'd) → Black → commit.

---

### Task 7: e2e, sweep, docs

- [ ] e2e: one real gpt-4.1-mini run+grade per eval (`smevals run examples/code-review -t boundary-shift -g`, then full-eval runs for the two authoring evals), judge grade each; inspect grade.yamls; verify all three evals in a scratch serve (`-p 7003`, don't touch 7001).
- [ ] Extend sweep-code-authoring.sh: EVALS+=(code-authoring/config-lexer code-authoring/refactor-preserve), and a code-review block in the model loop (`smevals run examples/code-review -m "$model" -n 5 -g`), plus judge passes for all three in the deferred section. `bash -n`.
- [ ] `git status --short` clean; report.

## Deviations from prior practice (deliberate, pre-authorized)

Tasks 3, 5, 6 author creative content under binding rails instead of
transcribing plan-inlined artifacts (the two-eval plan's approach). The
rails + fixture contracts + reviewer re-derivation carry the validity
burden. Authorized by Jesse's standing "use your best judgement" for this
autonomous phase; flagged in the ledger and the wrap-up report.
