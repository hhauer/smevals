# smevals studio tutorial movie pipeline (v2)

Script-driven pipeline that turns `scenes-tutorial-v2.yaml` into the
narrated tutorial mp4. Edit the movie by editing the scenes yaml, not
these scripts. The v2 cut centers on the real `code-review` eval: the
actual prompt edited on camera, the runner contract, a genuine local
model run graded on screen, then the results/compare/gallery/sweep tour.

```
setup_scratch_suite_v2.sh  copies code-review + pelican (runs included)
                           into a throwaway suite the movie may write to
scenes-*.yaml --record.js------------> work/clips/*.mp4, work/titles/*.png
scenes-*.yaml --narrate_gpt_audio.py-> work/narration/<id>.wav
both ----------assemble_v2.sh--------> smevals-studio-tutorial-v2.mp4
```

Requires: node + `npm install playwright-core js-yaml` in this dir,
ffmpeg/ffprobe, Google Chrome, uv, and an OpenAI key under
`llm keys set openai` for narration (model `gpt-audio-1.5`, voice nova,
each block verified verbatim against the returned transcript and
retried once on drift). `kokoro_gen.py` is the offline fallback voice
(needs `uv venv` with mlx-audio + misaki[en] + espeak workarounds it
documents; note it silently drops out-of-vocabulary words like "eval").

Recording rules that bite (learned the hard way, see the scenes yaml
header for the full list): never record against the repo's `examples/`
(the movie writes for real); the run started by the run-it scene dies
with that pass's studio, so the finished run the next scene opens is
produced off-camera with `smevals run` between passes; `type` only into
empty fields, `append` for caret-end edits of existing ones; no
bool-lookalike scalars in form fields.

Recording is split into passes (pass1/2/3 subsets of the main yaml)
because scene 7 needs the model run from scene 6 to have finished:
record pass1, run `smevals run <suite>/code-review -t float-money -m
<model>`, record pass2, record pass3, then narrate + assemble against
the FULL scenes yaml.
