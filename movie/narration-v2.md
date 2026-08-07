# Tutorial v2 narration script

One block per scene, verbatim what the TTS (or Jesse, self-voiced) reads.
Word counts and durations at 2.5 words/sec. Total narrated runtime
target: ~4:40 including title cards.

Voice: Kokoro (af_heart or am_michael, Jesse's pick) via
scratchpad/kokoro_gen.py. If "smevals" comes out mangled, substitute
"S M evals" in the TTS input only.

---

**1. title** (4s, silent)

**2. shelf** (~60 words, ~24s)
This is smevals studio. Every card is an Eval: a plain directory of
tasks, configs, and graders, with an executable runner script. The cards
tell you where things stand - how many runs, how many graded, and the
best model so far. This one, code-review, hands a model a real commit
with planted defects and asks for a code review. Let's open it.

**3. eval-landing** (~43 words, ~17s)
An Eval opens on its summary: what was tested, and how it was graded -
every task's prompt and every grader's check pipeline, readable without
opening a single file. And across the top, the whole Eval is one tab
away: files, runs, results, compare, gallery.

**4. author-prompt** (~90 words, ~36s)
Here's the actual prompt. You are reviewing a single commit to a
TypeScript codebase - the complete pre-change file, then the unified
diff. Report every defect you're confident is real, and don't pad the
review with speculative nitpicks. That last line is doing real work:
it's what separates a model that reads code from one that hedges. The
prompt is just a field in a YAML file, so let's raise the bar - ask for
line numbers with every finding. Watch the environment mirror on the
right: every field you type becomes an environment variable the runner
will receive, exactly as shown.

**5. runner-contract** (~72 words, ~29s)
Who receives that prompt? The runner - a twenty-line shell script, not a
framework. smevals hands it the model name, the prompt, and a working
directory, all as environment variables. It calls the model, saves the
response, and exits non-zero if anything smells wrong - an empty
response is a harness problem, not model evidence, so it's failed and
excluded rather than quietly graded. Anything it writes to its directory
is kept as an artifact.

**6. run-it** (~52 words, ~21s)
The run form lives right on the task's page. Pick a model - we'll use a
small fast local one so nobody waits - and press Run. That's a real
subprocess, a real model, running right now. The runs list updates live
as it lands; no refresh button, no polling by hand.

**7. read-and-grade** (~72 words, ~29s)
Here's what the model wrote - an actual code review, findings and
reasoning, saved as plain text in the run directory. Grade it: the
default grader parses the findings out of the prose, then matches them
against the task's answer key. If you ever disagree with a grade, the
rubric is one click away - edit grader, right there. And if you want
another sample from the same model, Run again is one click too.

**8. results** (~63 words, ~25s)
The Results tab ranks every model that's run this Eval - mean score,
pass rate, and how sure we can be given the sample size. Scope it to a
single task and the ranking re-computes for just that task; the address
bar keeps the scope, so the view is a link you can share. Below, a feed
of what was graded most recently, each entry one click from its run.

**9. compare** (~49 words, ~20s)
Compare puts one task's output from every run on a single screen. Pin
two cards and you get the full reviews side by side - where one model
caught the floating-point money bug and the other wandered off into
style advice, you'll see it in the same breath.

**10. gallery** (~38 words, ~15s)
And when an Eval's output is a picture, the gallery is the whole Eval at
a glance - every run's image, best scores first. Eight models were asked
for a pelican riding a bicycle. Some of them even managed it.

**11. sweep-tease** (~40 words, ~16s)
When one run isn't enough, sweep this eval - the composer arrives
already scoped, pick your models and a sample size, and the matrix fills
itself in, live, cell by cell. That's a longer story; the README tells
it properly.

**12. closing** (5s, silent)

---

Total: ~4:40. Scene durations in scenes-tutorial-v2.yaml pause values
approximate these; assemble step stretches each clip to
max(narration, visuals) as before.

## Data gaps / pre-recording checklist

1. Scratch suite: cp -R examples/code-review and
   examples/pelican-riding-a-bicycle (runs included) into a fresh suite
   dir; record against that, never examples/.
2. Scene 9 needs two models with float-money runs in the copied data -
   examples has gemma-4-31b-qat, gemma-4-e4b, gpt-4.1-mini and five
   more: satisfied.
3. Scene 6/7 gap: after recording scene 6, wait for the
   lfm2.5-2.6b-mlx run to finish (1-3 min) before recording scene 7.
   LM Studio must have the model available (lms ls first).
4. Rebuild narrate.sh (Kokoro via kokoro_gen.py; the venv lives at
   scratchpad/tts-venv) and assemble.sh (v1's logic: per-scene
   max(narration, visual), concat, title cards) - the originals were
   lost to the tmp cleaner. record.js survives.
5. Judge grader stays OFF camera (OpenAI credits exhausted).
6. New selectors used by v2 actions that v1's record.js action verbs
   may not cover: click_text_end / type_append (scene 4 caret append),
   select (scene 8 dropdowns), click_text (scene 11). Extend record.js
   or replace with equivalent existing verbs at recording time.
