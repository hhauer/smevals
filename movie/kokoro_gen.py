"""Kokoro TTS wrapper: pin espeak-ng paths before anything else imports
phonemizer, then generate. Usage: kokoro_gen.py <voice> <outprefix> <text>"""
import sys

import espeakng_loader
from phonemizer.backend.espeak.wrapper import EspeakWrapper

EspeakWrapper.set_library(espeakng_loader.get_library_path())
EspeakWrapper.set_data_path(espeakng_loader.get_data_path())


# The bundled espeak-ng dylib aborts on its baked-in CI data path; force
# the pipeline's graceful no-fallback branch instead (OOD words skipped).
from misaki import espeak as _misaki_espeak


class _EspeakDisabled:
    def __init__(self, *a, **k):
        raise RuntimeError("espeak fallback disabled: baked data path broken")


_misaki_espeak.EspeakFallback = _EspeakDisabled

from mlx_audio.tts.generate import generate_audio  # noqa: E402


voice, prefix, text = sys.argv[1], sys.argv[2], sys.argv[3]
generate_audio(
    text=text,
    model="mlx-community/Kokoro-82M-bf16",
    voice=voice,
    file_prefix=prefix,
    audio_format="wav",
    verbose=False,
    save=True,
)
print("done")
