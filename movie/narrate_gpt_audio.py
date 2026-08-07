#!/usr/bin/env python3
"""Narrate every scene of a scenes yaml with OpenAI gpt-audio-1.5 (Jesse's
pick over gpt-4o-mini-tts and Kokoro). It's a chat model reading a prompt,
not a bare TTS endpoint, so every block is verified against the returned
transcript and retried once on drift.

Usage: narrate_gpt_audio.py <scenes.yaml> <outdir>
Key: `llm keys get openai`. Writes <outdir>/<scene-id>.wav.
"""

import base64
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

MODEL = "gpt-audio-1.5"
VOICE = "nova"
STYLE = ("Read the following tutorial narration aloud. Warm, clear "
         "narrator, conversational pace, natural emphasis, not salesy. "
         "Read ONLY the narration text verbatim, nothing else:\n\n")


def norm(s):
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).split()


def generate(key, text):
    body = json.dumps({
        "model": MODEL,
        "modalities": ["text", "audio"],
        "audio": {"voice": VOICE, "format": "wav"},
        "messages": [{"role": "user", "content": STYLE + text}],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        doc = json.load(resp)
    audio = doc["choices"][0]["message"]["audio"]
    return base64.b64decode(audio["data"]), audio.get("transcript", "")


def main(scenes_path, outdir):
    key = subprocess.run(["llm", "keys", "get", "openai"],
                         capture_output=True, text=True,
                         check=True).stdout.strip()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    doc = yaml.safe_load(open(scenes_path))
    failures = []
    for scene in doc["scenes"]:
        text = (scene.get("narration") or "").strip().replace("\n", " ")
        if not text:
            continue
        sid = scene["id"]
        out = outdir / f"{sid}.wav"
        if out.exists():
            print(f"{sid}: cached")
            continue
        for attempt in (1, 2):
            data, transcript = generate(key, text)
            want, got = norm(text), norm(transcript)
            # verbatim within tolerance: allow tiny punctuation-driven
            # drift but not dropped/invented sentences
            drift = abs(len(want) - len(got)) + sum(
                1 for a, b in zip(want, got) if a != b)
            if drift <= max(2, len(want) // 25):
                out.write_bytes(data)
                print(f"{sid}: ok ({len(data)//1024}KB, drift {drift})")
                break
            print(f"{sid}: transcript drifted (attempt {attempt}, "
                  f"drift {drift}): {transcript[:80]}...")
        else:
            failures.append(sid)
    if failures:
        print("FAILED verbatim delivery:", failures)
        return 1
    print("narration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
