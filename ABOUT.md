# smevals

> A framework for running evals against small (and large) models: define Tasks, Configs, and Graders as YAML, execute Runs via pluggable Runner executables, and grade them with reusable Checkers.

**Family:** eval-labs · **Type:** tool · **Lifecycle:** experimental · **Owner:** simonw

## What it does
smevals is a Python CLI (`pip install smevals`, published on PyPI) for building and running model evals. An Eval is a directory of YAML files describing Tasks (prompts), Configs (model + settings), and Graders (ordered Checks); Runs are executed by any CLI Runner program and graded by named or custom Checker executables, keeping Runs immutable and re-gradable. `smevals build` renders results into a static HTML report site.

## How it fits
- Depends on: —
- Used by: [smevals-haiku-example](https://github.com/prime-radiant-inc/smevals-haiku-example) — example Eval directory built with this framework
- External: whatever model providers the user's Runner scripts call (e.g. via the `llm` CLI); no provider is baked in

## Runtime & data
- Runs: local CLI (Python 3.10+, click + pyyaml)
- Data in: Eval directories (eval.yaml, tasks/, configs/, graders/, checkers/, a Runner executable)
- Data out: `runs/` directories of immutable Run + Grade records; static HTML report site from `smevals build`

## Links
- PyPI: https://pypi.org/project/smevals/

<!-- Maintained by the maintaining-project-map skill. Do not hand-edit; regenerated. -->
