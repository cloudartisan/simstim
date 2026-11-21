#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD="${BUILD_DIR:-$ROOT/build/cues}"
VOICE_HOME="${SIMSTIM_VOICE_HOME:-$HOME/.local/share/simstim/voice}"
PYTHON="${SIMSTIM_CUE_PYTHON:-$VOICE_HOME/render/bin/python}"
MODEL="${SIMSTIM_CUE_MODEL:-$VOICE_HOME/kokoro-v1.0.onnx}"
VOICES="${SIMSTIM_CUE_VOICES:-$VOICE_HOME/voices-v1.0.bin}"
"$PYTHON" "$ROOT/k151-standalone/tools/generate_cues.py" --model "$MODEL" --voices "$VOICES" --out "$BUILD"
cc -std=c11 -O2 -Wall -Wextra -Werror \
  -I"$BUILD" "$ROOT/k151-standalone/host/test_cue_pcm.c" \
  "$BUILD/status_cues_data.c" -o "$BUILD/test_cue_pcm"
"$BUILD/test_cue_pcm" "$BUILD"
if [[ -n "${SIMSTIM_ASR_MODEL:-}" ]]; then
  "${SIMSTIM_ASR_PYTHON:-$PYTHON}" "$ROOT/k151-standalone/tools/verify_cues_asr.py" \
    --clips "$BUILD" --model "$SIMSTIM_ASR_MODEL"
else
  printf '%s\n' 'ASR skipped; set SIMSTIM_ASR_MODEL to a local faster-whisper model.'
fi
