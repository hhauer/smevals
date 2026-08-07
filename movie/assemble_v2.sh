#!/usr/bin/env bash
# Rebuild of the lost assemble.sh: mux each scene's visuals with its
# narration and concatenate in scenes.yaml order. Segment duration =
# max(visual, narration) - video freeze-frames to cover longer narration,
# audio pads with silence to cover longer visuals (v1's rule, per its
# QA report and README).
#
# Usage: assemble_v2.sh <scenes.yaml> <workdir> <out.mp4>
# Expects: work/clips/<id>.mp4 (browser scenes, from record.js),
#          work/titles/<id>.png (title scenes, from record.js),
#          work/narration/<id>_000.wav|<id>.wav|<id>.mp3 (narrate step; optional per scene)
set -euo pipefail
scenes="${1:?usage: assemble_v2.sh <scenes.yaml> <workdir> <out.mp4>}"
work="${2:?usage: assemble_v2.sh <scenes.yaml> <workdir> <out.mp4>}"
out="${3:?usage: assemble_v2.sh <scenes.yaml> <workdir> <out.mp4>}"
command -v ffmpeg >/dev/null || { echo "ffmpeg required" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe required" >&2; exit 1; }

SCRATCH="$(cd "$(dirname "$0")/.." && pwd)"
PY="$SCRATCH/tts-venv/bin/python"

mkdir -p "$work/segments"

# scene table: id \t type \t title_duration (browser scenes: 0)
"$PY" - "$scenes" <<'EOF' > "$work/segments/scene-table.tsv"
import sys

import yaml

doc = yaml.safe_load(open(sys.argv[1]))
for scene in doc["scenes"]:
    print(f"{scene['id']}\t{scene['type']}\t{scene.get('duration', 0)}")
EOF
fps=$("$PY" -c "import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))['fps'])" "$scenes")

dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }

narration_for() {
  local sid="$1" f
  for f in "$work/narration/${sid}_000.wav" "$work/narration/${sid}.wav" \
           "$work/narration/${sid}.mp3"; do
    [[ -f "$f" ]] && { echo "$f"; return; }
  done
  echo ""
}

concat_list="$work/segments/concat.txt"
: > "$concat_list"

while IFS=$'\t' read -r sid stype tdur; do
  seg="$work/segments/${sid}.mp4"
  nar="$(narration_for "$sid")"
  ndur=0; [[ -n "$nar" ]] && ndur=$(dur "$nar")

  if [[ "$stype" == "title" ]]; then
    img="$work/titles/${sid}.png"
    [[ -f "$img" ]] || { echo "missing $img" >&2; exit 1; }
    D=$("$PY" -c "print(max(float('$tdur'), float('$ndur')))")
    if [[ -n "$nar" ]]; then
      ffmpeg -nostdin -y -v error -loop 1 -i "$img" -i "$nar" \
        -t "$D" -r "$fps" -pix_fmt yuv420p \
        -af "apad" -shortest \
        -c:v libx264 -preset medium -c:a aac -ar 44100 -ac 2 "$seg"
    else
      ffmpeg -nostdin -y -v error -loop 1 -i "$img" -f lavfi -i anullsrc=r=44100:cl=stereo \
        -t "$D" -r "$fps" -pix_fmt yuv420p \
        -c:v libx264 -preset medium -c:a aac -shortest "$seg"
    fi
  else
    clip="$work/clips/${sid}.mp4"
    [[ -f "$clip" ]] || { echo "missing $clip" >&2; exit 1; }
    cdur=$(dur "$clip")
    D=$("$PY" -c "print(max(float('$cdur'), float('$ndur')))")
    pad=$("$PY" -c "print(max(0.0, float('$D') - float('$cdur')))")
    if [[ -n "$nar" ]]; then
      ffmpeg -nostdin -y -v error -i "$clip" -i "$nar" \
        -filter_complex "[0:v]tpad=stop_mode=clone:stop_duration=${pad}[v];[1:a]apad[a]" \
        -map "[v]" -map "[a]" -t "$D" -r "$fps" -pix_fmt yuv420p \
        -c:v libx264 -preset medium -c:a aac -ar 44100 -ac 2 "$seg"
    else
      ffmpeg -nostdin -y -v error -i "$clip" -f lavfi -i anullsrc=r=44100:cl=stereo \
        -filter_complex "[0:v]tpad=stop_mode=clone:stop_duration=${pad}[v]" \
        -map "[v]" -map 1:a -t "$D" -r "$fps" -pix_fmt yuv420p \
        -c:v libx264 -preset medium -c:a aac "$seg"
    fi
  fi
  echo "segment: $sid ($(dur "$seg")s)"
  echo "file '$(cd "$(dirname "$seg")" && pwd)/$(basename "$seg")'" >> "$concat_list"
done < "$work/segments/scene-table.tsv"

ffmpeg -nostdin -y -v error -f concat -safe 0 -i "$concat_list" -c copy "$out"
echo "assembled: $out ($(dur "$out")s)"
