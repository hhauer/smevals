# Code-authoring evals: interval-set and usage-billing

2026-08-01. Two new example evals that measure writing TypeScript from a
high-quality spec. Both share one architecture; they differ in what makes
them hard. A follow-up project (single-commit code review with planted
defects) will reuse parts of this machinery.

- **interval-set** - edge-case-dense algorithm. The spec is easy to read
  and hard to implement: correctness lives in boundary behavior.
- **usage-billing** - spec-lawyering. The implementation is easy once the
  spec is understood; the difficulty is reading precisely. Every rule is
  stated clearly exactly once, and every hidden test case targets a rule
  that punishes skimming.

## Layout: a code-authoring Suite

Both evals live under one Suite directory and share the runner and
checkers via relative paths (already supported: runner and checker paths
resolve relative to the YAML file naming them).

```
examples/code-authoring/
├── run-llm                     # llm-CLI runner, same contract as other examples
├── checkers/                   # shared by both evals (and the future code-review eval)
│   ├── extract-ts              # response -> solution.ts in the grade workspace
│   ├── tsc-check               # npx tsc --strict --noEmit, version pinned in check config
│   ├── run-tests               # data-driven case harness, emits score/metrics/tags/details
│   └── llm-judge-code          # code-quality judge for the judge graders
├── interval-set/
│   ├── eval.yaml
│   ├── tasks/interval-set.yaml # the complete spec, as the prompt
│   ├── configs/default.yaml    # runner: ../../run-llm, model: gpt-4.1-mini
│   ├── graders/default.yaml    # extract -> tsc -> tests (deterministic)
│   ├── graders/judge.yaml      # extract -> quality judge (gpt-4.1)
│   ├── tests/cases.ts          # hidden cases, grouped, data-driven
│   └── reference/              # correct + planted-bug solutions (never shown to models)
└── usage-billing/
    └── (same shape)
```

Trade-off, decided: these examples are not fully self-contained the way
haiku is - they share machinery at the suite level. The alternative
(duplicating four checkers per eval) loses to DRY, especially with the
code-review eval coming.

One meaty Task per eval. The per-task test-case convention keeps adding
Tasks later mechanical.

## The specs

Each Task's `prompt` is a complete spec for a single TypeScript module
with an exactly-pinned export surface (the hidden tests import it, so API
compliance is part of the spec). Node 26 runs TypeScript directly via
type stripping; no build step exists in the eval.

### interval-set

A module exporting an `IntervalSet` class over real numbers with mixed
open/closed endpoints. Operations: `add`, `remove`, `contains(point)`,
`intersects(interval)`, `spans()` returning the canonical disjoint list,
plus `union` and `intersection` of two sets. Intervals cross the API as
plain objects (specified shape) so the harness can feed and read them.

The spec pins down, explicitly:

- Canonicalization and merging: `[1,2]` + `[2,3]` merge to `[1,3]`;
  `[1,2)` + `(2,3]` do not (2 is missing); `[1,2)` + `[2,3]` do.
- Removal splitting: removing `(2,3)` from `[1,4]` leaves `[1,2]` and
  `[3,4]`.
- Degenerate intervals: `[5,5]` is the point 5; `(5,5)`, `[5,5)`, `(5,5]`
  are empty and vanish.
- Unbounded endpoints (negative/positive infinity), open by definition.

### usage-billing

A module exporting `computeInvoice(plan, subscription, usageRecords)`.
Skim-punishing rules, each stated once, each targeted by a case group:

- Graduated tiers with "up to and including" bounds.
- Mid-period plan changes prorated by calendar days; the change day bills
  to the new plan.
- All intermediate math in integer cents; rounding only at two named
  points, half-up.
- Records outside the period ignored; period start inclusive, end
  exclusive.
- Credits applied after tax, ordered by expiry then size, floor at zero.

## Runner and configs

`run-llm` is the standard example runner: `llm -m "$SMEVALS_MODEL"
"$SMEVALS_PROMPT"`, then `llm logs -c --json > log.json`. Each eval's
default config uses `model: gpt-4.1-mini` as a cheap, sane default;
sweeps override with `-m` as usual.

## Graders

### default (deterministic; the leaderboard grader)

1. `extract-ts` - required, `creates: solution.ts`. Takes the last fenced
   code block (```ts / ```typescript, then any fence, then the whole
   output as a last resort) - models often restate fragments before the
   final module.
2. `tsc-check` - required. `npx -y typescript@5.9 tsc --strict
   --noEmit` on `solution.ts`. The TypeScript version is a check config
   key (initially `5.9`), so the Grader snapshot fully determines
   behavior. The first five compiler errors go in notes.
3. `run-tests` - runs the harness: imports the eval's `tests/cases.ts`
   and the workspace `solution.ts`, executes each case in try/catch with
   a per-case timeout, prints the smevals result JSON itself (no test
   framework, no output parsing). Score = passed/total. Metrics: one
   boolean rate per case group. Tags for failure shapes
   (`throws_at_runtime`, plus per-eval tags such as
   `merges_touching_open_intervals` or `rounds_per_record`). Details: the
   first five failing cases with expected-vs-got.

`scoring.pass_threshold: 1.0` - the spec is fully specified; pass means
every case, partial credit stays visible in the score.

### judge (quality; side by side, like haiku's)

1. `extract-ts` - required (grade workspaces are per-grader).
2. `llm-judge-code` - sends `solution.ts` and a rubric to `gpt-4.1` with
   a JSON schema: score 0-10 (normalized to 0-1), controlled tags
   (`idiomatic_types`, `any_abuse`, `dead_code`, `clear_naming`,
   `over_engineered`), notes. The rubric says judge quality, not
   correctness - correctness is default's job. Deliberately no tsc gate:
   a quality read on non-type-checking code is interesting when comparing
   graders.

## Validating the evals themselves

Test-first, in the repo's pytest suite (`tests/test_example_code_authoring.py`),
skipping cleanly when `node` is not on PATH so pure-Python CI passes:

- **Reference solutions** (`reference/solution.ts`, never in the prompt):
  harness scores 1.0, tsc-check passes.
- **Planted-bug fixtures**: variants embodying specific misreadings (merge
  `[1,2)`+`(2,3]`; round per-record). Each must score below 1.0 and fail
  the case group aimed at it - proof the tests discriminate.
- **Checker contract tests**: extract-ts against prose-wrapped, multi-fence
  and bare outputs; the harness against a solution that throws (tags
  `throws_at_runtime`, partial credit, no Grader crash).

The llm-judge-code checker gets no mocked tests; its logic beyond
arg-passing lives in the schema and rubric, a pattern already proven by
the pelican image judge.

## Done means

- `smevals run examples/code-authoring/interval-set -g` (and
  usage-billing) work end to end.
- Both evals appear when serving `examples/`.
- pytest green, including the new fixture tests.
- The six-model sweep runs over both evals.
