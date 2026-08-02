# Three invented evals: code-review-defects, config-lexer, refactor-preserve

2026-08-02. Jesse's standing directive: "invent 3 or 4 new evals related to
evaluating code quality or authoring tricky code," executed autonomously
with best judgment while he is away. Three evals, not four: each is built
to the same validity bar as interval-set/usage-billing (reference
solutions, planted-bug/discrimination fixtures, independent reviewer
re-derivation), and three well-validated evals beat four rushed ones.
Jesse separately named the first one as the intended next project
("a single-commit code review eval with planted really subtle defects"),
so it is the flagship.

Decisions made without Jesse (flagged for his review):

- Three evals, not four (budget: validity work per eval is the expensive
  part).
- TypeScript remains the working language everywhere, reusing the
  code-authoring suite's checkers and conventions.
- The review eval grades defect-finding deterministically (line-window
  match on structured findings) with an LLM judge only for explanation
  quality, not for detection.

## Eval A: code-review-defects (flagship)

An eval measuring whether a model can review a single commit and find the
one subtle planted defect. Lives at `examples/code-review/` as its own
eval (not inside the code-authoring suite - its checkers are different in
kind).

**Task shape.** Each Task is one commit to review: the task YAML carries
the full pre-change file (`before`), the unified diff (`diff`), and the
prompt instructs: review this commit; report every defect you find as
JSON `{"findings": [{"line": <post-change line number>, "description":
"..."}], "summary": "..."}`. The planted defect's location and nature
live in a per-task answer file under `answers/<task>.yaml` (never in the
prompt): `line`, an acceptable `window` (± lines), and `must_mention` -
a list of regex alternatives, at least one of which a correct description
matches (e.g. `off.by.one|boundary|last (day|element)|<=`).

**Six Tasks, six defect flavors** (all in small, realistic TS modules of
40-80 lines; each diff is a plausible refactor or feature commit that
introduces exactly one defect):

1. `boundary-shift` - a refactor of a paginated fetch changes `<` to `<=`
   deep inside an otherwise-mechanical rename commit.
2. `stale-closure` - extracting a callback captures a loop variable at
   the wrong scope; only fires on the retry path.
3. `float-money` - a "cleanup" replaces integer-cents arithmetic with
   dollars-as-float in one branch.
4. `mutated-default` - a shared default-options object gains an in-place
   mutation, poisoning later callers.
5. `sort-stability` - a comparator "simplification" collapses a
   two-level sort into subtraction of incomparable keys, breaking a
   documented tie-break.
6. `swallowed-error` - error handling reshuffled so one specific failure
   path returns success with a partial result.

**Grader `default` (deterministic):**

1. `parse-findings` (required, `creates: findings.json`) - extracts the
   JSON object from the response (fenced or bare), validates shape,
   writes it to the workspace. Fails when no parseable findings object
   exists.
2. `match-findings` - loads `answers/<task>.yaml` via `SMEVALS_TASK`,
   compares: a finding whose `line` is within the window AND whose
   description matches `must_mention` counts as found. Score 1.0 if
   found, else 0.0. Metrics: `found_planted` (bool),
   `false_positives` (count of findings outside the window),
   `findings_total`. Tags: `found_planted`, `missed_planted`,
   `noisy_review` (>3 false positives), `silent_pass` (zero findings).
   Partial credit 0.5 when the line matches but the description doesn't
   (right place, wrong diagnosis).

`scoring.pass_threshold: 1.0`.

**Grader `judge`:** parse-findings, then an `llm-judge-review` checker
(gpt-4.1): given the diff, the model's findings JSON, and the answer
key, score 0-10 the review's explanation quality and actionability;
controlled tags `precise_location`, `correct_mechanism`,
`suggests_fix`, `vague`, `wrong_mechanism`. The judge sees the answer
key, so it evaluates explanation quality of a known defect rather than
re-detecting it.

**Validity fixtures.** For every task: a `reference/` review (correct
finding, correct description) that must score 1.0, plus two adversarial
fixtures: `decoy-<task>.json` (confident findings that are all wrong -
must score 0.0 with `missed_planted`) and `shotgun-<task>.json` (flags
every hunk indiscriminately including the right line but with a generic
description - must land at 0.5, proving `must_mention` does real work).
The shotgun fixture is the key one: it proves the eval cannot be gamed
by flagging everything.

## Eval B: config-lexer (edge-case-dense authoring)

Joins the code-authoring suite at `examples/code-authoring/config-lexer/`,
reusing run-llm, extract-ts, tsc-check, run-tests unchanged.

**The task.** Implement `tokenize(source: string): Token[]` for a small
config language, from a fully-specified prompt: identifiers, integers
(with `_` separators, no leading zeros), strings (double-quoted with
`\n \t \\ \" \u{...}` escapes; single-quoted raw with `''` as the only
escape), line comments `#`, block comments `/* */` that NEST, and
punctuation `= [ ] { } ,`. Tokens carry `kind`, `text` (raw source
slice), `value` (decoded for strings/ints), `line`, `col` (1-based,
counting by code points, tabs = 1 col). Errors are tokens too:
`{kind: "error", ...}` at the offending position with a specified
`reason` from a closed enum (`unterminated_string`, `bad_escape`,
`unterminated_comment`, `bad_number`), after which lexing resumes at a
specified resynchronization point. The error-token contract is what
makes this hard: most implementations throw; the spec demands recovery.

**Case groups** (~24 cases): `basics`, `numbers`, `string_escapes`,
`raw_strings`, `nested_comments`, `positions`, `error_recovery`,
`eof_edges` (unterminated things at EOF, empty input, BOM handling -
BOM is specified as skipped without affecting col).

**Fixtures:** reference plus `bug-comments-dont-nest.ts` (fails only
`nested_comments`) and `bug-col-counts-utf16.ts` (uses UTF-16 units so
astral-plane chars break `positions` - fails only `positions`).

**Graders:** `default` and `judge`, identical in structure to
interval-set's.

## Eval C: refactor-preserve (code quality under a behavioral contract)

Joins the code-authoring suite at
`examples/code-authoring/refactor-preserve/`.

**The task.** The prompt embeds `legacy.ts`: a working but deliberately
awful ~90-line module (a shipping-cost calculator: three copy-pasted
rate-table walks, stringly-typed region codes, a boolean-parameter pyramid,
dead branches, misleading names). The model must produce a refactored
module with the SAME exports and byte-identical observable behavior -
including three explicitly quirky behaviors the prompt points at but does
not explain (a deliberate fall-through, an order-dependent discount, a
NaN-propagating branch). The prompt says: preserve behavior exactly,
including anything you think is a bug; improve everything else.

**Grader `default`:** extract-ts → tsc-check → run-tests with ~18 cases:
`core_paths`, `quirk_fallthrough`, `quirk_discount_order`, `quirk_nan`
(the quirk groups prove the model didn't "fix" the quirks), plus
`duplication` - a meta-case: the checker-side case file reads the
solution source and asserts the three near-identical rate-walk blocks
did not survive (crude but effective: the legacy module's telltale
9-line walk appears three times; the case counts occurrences of its
normalized skeleton and expects <= 1). Fixtures: reference (a clean
refactor scoring 1.0), `bug-fixed-the-quirk.ts` (an otherwise-excellent
refactor that "fixes" the fall-through - fails exactly the quirk group,
proving the trap works), and `legacy-verbatim.ts` (the original
unrefactored module - passes all behavior groups but fails
`duplication`, proving a model cannot pass by parroting the input).

**Grader `judge`:** the existing llm-judge-code with a rubric weighted
toward duplication removal and naming; same 5-tag vocabulary.

## Shared mechanics

- All three evals: one Runner (`run-llm` pattern), configs default to
  `gpt-4.1-mini`, `.gitignore` with `runs`.
- code-review needs two new checkers (`parse-findings`,
  `match-findings`) and one judge variant (`llm-judge-review`), all in
  `examples/code-review/checkers/`.
- pytest: every reference scores 1.0; every fixture scores exactly its
  designed score and fails exactly its targeted group/tag; checker
  contract tests for parse-findings (fenced/bare/absent JSON) and
  match-findings (window edges, must_mention alternatives,
  false-positive counting). Node-dependent tests skip without node.
- Sweep integration: extend sweep-code-authoring.sh's EVALS array with
  the two new authoring evals and add a code-review loop (same models,
  `-n 5 -g`), plus judge passes.

## Done means

pytest green; each eval e2e-verified with one real gpt-4.1-mini run;
all three appear in the served UI; the 9-model sweep extended to cover
them; results reported with the same leaderboard treatment as the
existing evals.
