# smevals

[![PyPI](https://img.shields.io/pypi/v/smevals.svg)](https://pypi.org/project/smevals/)
[![Changelog](https://img.shields.io/github/v/release/prime-radiant-inc/smevals?include_prereleases&label=changelog)](https://github.com/prime-radiant-inc/smevals/releases)
[![Tests](https://github.com/prime-radiant-inc/smevals/workflows/Test/badge.svg)](https://github.com/prime-radiant-inc/smevals/actions?query=workflow%3ATest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/prime-radiant-inc/smevals/blob/main/LICENSE)

A framework for running evals against small (and large) models

## Installation

```bash
uv tool install smevals
```

Or `pip install smevals`, or just `uvx smevals --help`.

## Vocabulary used by this project

The top-level concept is an **Eval**: a collection of Tasks used to determine how good a particular model or model-and-harness configuration is at a specific high-level capability, such as text-to-SQL, drawing a pelican riding a bicycle, or evaluating whether an implementation satisfies a provided specification.

Evals can optionally be grouped into **Suites** of related Evals, primarily as a mechanism for organizing them on disk.

An **Eval** is a collection of **Tasks**. These are the individual exercises that a model must complete for its abilities to be evaluated.

A **Config** describes the setup used to attempt Tasks. It specifies a model and may include model parameters, system prompts, tools and other settings.

To gather evidence, we create a **Run**. A Run is the immutable record of executing one Task against one Config using a **Runner**. A Runner is a reusable CLI program that may send prompts directly to a model or build on an agent harness such as Codex or Pi.

The same Task and Config can be executed multiple times, producing multiple Runs to help account for non-deterministic results - `smevals run -n 5` tops each Task up to five Runs. Each Run includes a timestamp to help track these.

A Run whose Runner exits non-zero is a **failed Run**: a harness-level error such as a network failure, not evidence about the model. Failed Runs stay on disk for debugging but are never graded, are excluded from reports, and do not count towards `-n` targets.

Once we have gathered Runs, we apply a **Grader** to each Run to produce a **Grade**. A Grader is a configured sequence of **Checks**, plus rules for combining their results into that Grade.

Checks are individual assertions or measurements. Some may be simple, such as “does the output contain this text?” Others may be more complex, such as “render this SVG to an image and have an LLM judge assess it”. Each Check names the **Checker** that performs it, along with configuration for that Checker such as patterns, rubrics or expected values. A Check can be marked as required, in which case its failure halts the Grader and skips the remaining Checks.

A **Checker** is a named operation *or* a reusable CLI program that implements one kind of Check. `contains` and `xml-valid` are named operations, `../checkers/render-svg` might be a custom program. The same Checker can be used by many Checks across many Graders. The Checks in a Grader execute in order and share a working directory, so a Checker may create files - such as a rendered image - which are kept with the Grade as artifacts and available to later Checks in the sequence.

A **Grade** is the result of applying a Grader to a Run. It records the result of each Check and may contain an overall pass/fail outcome and/or a numeric score. Grades can also include additional notes which are not used for scoring but may help interpret the results in the future.

We may later change the Grader we use to evaluate Runs without executing the Runs again. A single Run can therefore be evaluated multiple times, producing multiple Grades using different Graders.

## Building an Eval

An Eval is any directory containing an `eval.yaml` file:

```
my-eval/
├── eval.yaml            # name and description
├── tasks/               # one YAML file per Task
├── configs/             # one YAML file per Config
├── graders/             # one YAML file per Grader
├── checkers/            # custom Checker executables (by convention)
├── run-llm              # Runner executable (any name, any location)
└── runs/                # created by smevals run - never edit by hand
```

### Example Eval: Grading Haikus

Here's how to structure a complete Eval that asks models to write haikus and grades them on their structure.

The Eval consists of five files.

`my-eval/eval.yaml` defines a name and description:

```yaml
name: haiku
description: >-
  Can the model write a haiku on demand? Graded on structure:
  the reply must be exactly three lines.
```
An Eval must have one or more Tasks. Each of these is defined as a `tasks/*.yaml` YAML file.

`my-eval/tasks/pelicans.yaml` must have a `name`; a `prompt` is the common case, but any other keys are allowed and are passed to the Runner as environment variables:

```yaml
name: pelicans
prompt: Write a haiku about pelicans. Reply with only the haiku, three lines.
```
An Eval also needs at least one Config, defined in `configs/*.yaml`. If there is just one of these it should be called `default`.

`my-eval/configs/default.yaml` - the Config named `default` is used when no `-c` option is passed to `smevals run`.

`runner` specifies a path to an executable program relative to this file:

```yaml
name: default
runner: ../run-llm
model: gpt-4.1-mini
```

Here's that Runner script:

`my-eval/run-llm` - This one uses the [llm](https://llm.datasette.io/) CLI, but any executable honoring the contract below works. Make it executable with `chmod +x`:

```bash
#!/usr/bin/env bash
set -euo pipefail

llm -m "$SMEVALS_MODEL" "$SMEVALS_PROMPT"
llm logs -c --json > log.json
```

The Eval also needs a default Grader, which will be used to grade the results of each Run:

`my-eval/graders/default.yaml`:

```yaml
name: default
checks:
  - checker: ../checkers/three-lines
    required: true
scoring:
  pass_threshold: 1.0
```
The `checker` can be a relative path to a script - similar to `runner:` above - or can be the name of a built-in checker, listed below.

`my-eval/checkers/three-lines` - a custom Checker, also `chmod +x`:

```python
#!/usr/bin/env python3
import json, os, pathlib, sys

raw = (pathlib.Path(os.environ["SMEVALS_RUN_DIR"]) / "output.txt").read_text()
lines = [line for line in raw.strip().splitlines() if line.strip()]
print(json.dumps({
    "score": 1.0 if len(lines) == 3 else 0.0,
    "metrics": {"line_count": len(lines)},
    "notes": f"{len(lines)} non-empty line(s)",
}))
sys.exit(0 if len(lines) == 3 else 1)
```

To run the eval, grade it and then view the results:

```bash
smevals run my-eval -g                 # run every task, grade as each finishes
smevals run my-eval -m gpt-4.1-nano -m gemini-2.5-flash -g   # more models
smevals run my-eval -n 5 -g            # top every task up to five graded runs
smevals report my-eval                 # markdown report in the terminal
smevals serve my-eval                  # live web UI on http://127.0.0.1:7001
```

## The Runner contract

`smevals run` executes the Runner once per Task/model combination, with no arguments. Everything arrives through environment variables:

- `SMEVALS_MODEL` - the model to use, from the Config or the `-m` option.
- `SMEVALS_TASK` - the Task's name.
- `SMEVALS_PROMPT` - the Task's `prompt`, only set if the Task has one.
- `SMEVALS_TASK_<KEY>` - every scalar key of the Task, uppercased: a Task with `submission: mutant-003` provides `SMEVALS_TASK_SUBMISSION=mutant-003`.
- `SMEVALS_CONFIG_<KEY>` - every scalar key of the Config other than `runner` and `model`, uppercased: a Config with `effort: medium` provides `SMEVALS_CONFIG_EFFORT=medium`, so Configs can carry run conditions beyond the model.
- `SMEVALS_RUN_DIR` - absolute path to the Run's directory.

The working directory is the Run's directory. The contract:

- Standard output is captured as the Run's `output.txt` - it should be the model's response.
- Standard error is captured as `stderr.txt`.
- A non-zero exit code marks the Run as **failed**. A failed Run is a harness error - a network drop, a crashed tool - not evidence about the model, so it is never graded, is excluded from reports, and does not count towards an `-n` target (re-running the same command executes a replacement). Exit non-zero only for infrastructure problems; exit 0 whenever the output is a real model response you want judged, however bad.
- Any other files the Runner writes to its working directory are kept as Run artifacts (the `log.json` in the example above).

A Runner that drives an agent harness instead of a plain model call follows the same contract: assemble whatever inputs the Task's keys describe, run the harness, print the final result to standard output.

## Graders

A Grader is a YAML file in `graders/`:

```yaml
name: default
checks:
  - checker: contains          # a built-in Checker, by name
    value: "<svg"
    required: true
  - checker: ../checkers/render-svg   # a custom Checker, by path
    input: extracted.svg
    creates: render.png        # smevals verifies this file gets created
    required: true
  - checker: ../checkers/llm-judge-image
    image: render.png
    model: gpt-4.1
    rubric: Score this image from 0 to 10 ...
scoring:
  pass_threshold: 0.5
```

Each entry in `checks` names a Checker plus its configuration. Reserved keys:

- `checker` - a built-in name, or a path to an executable relative to the Grader file.
- `required` - if true and the Check fails, grading halts and the remaining Checks are recorded as skipped.
- `creates` - a filename, or list of filenames, the Checker promises to create in the shared workspace; the Check fails if any of them do not appear. A Checker may write any number of additional files beyond those promised - everything in the workspace is kept as a Grade artifact.

All other keys are configuration for the Checker, passed through via environment variables.

### Built-in Checkers

- `contains` - passes if the Run's `output.txt` contains `value`.
- `xml-valid` - passes if `file` (looked up in the grade workspace, then the Run directory) parses as well-formed XML.

### Outcomes and scores

The Grade's outcome and score are computed as follows:

- The Grade's score is the last `score` emitted by any Check - typically the final, most expensive Check. However, if any Check fails without emitting a score of its own, the Grade's score is null: a stale score from an earlier Check never stands in for one that did not run.
- The outcome is `fail` if any Check failed, otherwise `pass` if the score meets `scoring.pass_threshold` (or if there is no threshold or no score).

## The Checker contract

`smevals grade` executes each Check's Checker with no arguments and these environment variables:

- `SMEVALS_RUN_DIR` - absolute path to the Run directory being graded. Read the model's output from `$SMEVALS_RUN_DIR/output.txt`.
- `SMEVALS_CHECK` - the full Check configuration as JSON, for structured values like lists.
- `SMEVALS_CHECK_<KEY>` - every scalar key of the Check, uppercased: `rubric:` becomes `SMEVALS_CHECK_RUBRIC`.
- `SMEVALS_TASK` and `SMEVALS_TASK_<KEY>` - the Task's name and scalar keys, so a Checker can locate per-Task resources such as expected-answer files.

The working directory is the grade workspace, shared by all Checks in the Grader in order: files written by one Check (a rendered image, an extracted document) are available to later Checks and are kept with the Grade as artifacts.

A Checker signals pass or fail with its exit code (0 is a pass). It can also emit a JSON object on standard output with up to five keys, which are recorded in the Grade:

- `score` - a float from 0.0 to 1.0.
- `metrics` - an object mapping names to numbers or booleans, e.g. `{"precision": 0.9, "status_correct": true}`. Reports aggregate numbers as mean ± stderr and booleans as rates.
- `tags` - a list of short labels, e.g. `["wearing_a_hat", "correct_bicycle_frame_shape"]`. Tags are open vocabulary and presence-only: an absent tag means "not observed", not "false". They are normalized to lowercase snake_case, and the Grade records the union of all its Checks' tags. Reports aggregate them as counts and shares, and the web UI uses them for filtering.
- `notes` - a human-readable string explaining the result. Never aggregated.
- `details` - an object of structured diagnostics, such as predicted-versus-expected lists. Kept with the Grade but ignored by aggregation.

Any other keys are folded into `details`. A Checker that fails may still emit a score (a partial-credit measurement); a Checker that crashes before scoring leaves the Grade unscored, as described above.

## Runs and Grades on disk

Every Run is a directory:

```
runs/<task>/<config>/<model>/<timestamp>/
├── run.yaml         # the record: full task, resolved config, timing, exit code
├── output.txt       # the model's response (runner stdout)
├── stderr.txt       # only present if the runner wrote to stderr
├── ...              # any other artifacts the runner wrote
└── grades/
    └── <grader>/
        ├── grade.yaml     # outcome, score, tags, per-check results
        ├── grader.yaml    # snapshot of the Grader that produced this Grade
        └── ...            # artifacts written by Checkers
```

The model name is slugified for the path; the exact name is in `run.yaml`. `run.yaml` is written last, so its presence marks a complete Run. Runs are immutable - grading only ever adds files under `grades/`.

Each Grade includes a byte-for-byte snapshot of its Grader. `smevals grade` uses this for repeatability:

- By default it grades only Runs that have no Grade from the named Grader, and reports how many existing Grades were produced by an older version of the Grader spec.
- `--regrade` deletes and re-creates every Grade for that Grader, so nothing stale survives. Use it after editing a Grader.
- Multiple Graders coexist: each grades into its own `grades/<name>/` directory, so an eval can have e.g. a cheap deterministic `default` grader and an LLM-judge `judge` grader side by side.

By default `runs/` lives inside the Eval directory. Pass `--runs-dir DIR` to `run`, `grade` and `report` to keep runs elsewhere; they are then namespaced by Eval name.

## Commands

```
smevals run EVAL [-m MODEL]... [-c CONFIG] [-t TASK]... [-n N] [-g [GRADER]] [--runs-dir DIR]
```

Executes every Task (or just those named with `-t`) against every model given with `-m` (default: the Config's model), using the Config named by `-c` (default: `default`). `-g` grades each Run the moment it finishes; `-g NAME` uses that Grader, bare `-g` uses `default`. Exits non-zero if any Run fails or grades as fail.

`-n N` is a target sample size: each task/model pair is topped up to at least N successful Runs, executing only the shortfall, so re-running the same command is a no-op once the target is met and an interrupted session can be resumed by repeating it. Runs execute in full passes over the pairs - interrupting partway leaves balanced samples rather than many Runs of the first Task and none of the last. Failed Runs (a non-zero Runner exit) do not count toward the target: re-running the command executes replacements for them, attempting each pair's shortfall once per invocation, so a persistently failing Runner never retries in a loop. Without `-n`, exactly one new Run is executed per pair.

```
smevals grade EVAL [-g GRADER] [--regrade] [--runs-dir DIR]
```

Applies the Grader to every ungraded Run. Failed Runs are skipped - a harness error is not evidence worth grading. `--regrade` discards and redoes existing Grades from this Grader.

```
smevals report EVAL [-g GRADER] [--by-task] [--json] [--runs-dir DIR]
```

Prints a markdown report: leaderboard of config × model with mean ± stderr scores and failure counts, tag shares, and per-model blocks with metrics. Failed Runs are excluded from all statistics; the header reports how many were left out. `--by-task` adds per-task scores. `--json` emits the raw grade rows instead.

```
smevals serve EVAL_OR_SUITE... [-p PORT] [--host HOST] [-g GRADER]
```

Serves a live web UI (default port 7001) over one or more Evals. Data is re-read from disk on every poll, so the pages update as new Runs and Grades land. A directory that is not itself an Eval is treated as a Suite and searched recursively for Evals.

```
smevals build EVAL_OR_SUITE... [-o DIR] [-g GRADER]
```

Builds the same web UI as a self-contained static site (default `build/`), copying run artifacts into it. Each invocation adds or refreshes the given Evals in the output directory and leaves other Evals already built there untouched, so one site can aggregate Evals from many repositories.

```
smevals docs
```

Outputs this document.
