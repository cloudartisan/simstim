# Firmware speech cues

The three fixed cues are generated at configure/build time with Kokoro ONNX and the British `bm_daniel` voice. Firmware consumes generated mono signed-16-bit PCM at 16 kHz; `speaker.c` duplicates each sample to stereo and applies its signed integer `/2` gain immediately before I2S. Generated source, WAV and raw PCM live only under the ignored firmware build directory. No audio or model assets belong in Git.

## Pinned external inputs

- Renderer: `kokoro-onnx==0.6.1`; its full runtime dependency set is SHA-256 locked in [`../k151-standalone/host/requirements-cues.lock`](../k151-standalone/host/requirements-cues.lock). Upstream runtime project: [thewh1teagle/kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx).
- Model: [`kokoro-v1.0.onnx` from upstream `model-files-v1.0`](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx), SHA-256 `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5`.
- Voice pack: [`voices-v1.0.bin` from the same upstream `model-files-v1.0` release](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin), SHA-256 `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d`.
- These are direct versioned upstream asset URLs, not `latest` links. `tools/generate_cues.py` verifies the expected asset hashes before rendering, so any upstream artifact change fails closed.
- ASR acceptance uses `faster-whisper==1.2.1` and the complete hash-pinned lock [`../k151-standalone/host/requirements-cues-asr.lock`](../k151-standalone/host/requirements-cues-asr.lock). No ASR model files are included. The tested open-vocabulary model was `Systran/faster-whisper-small`.

The committed lock files include resolved transitive pins and hashes. From a clean checkout's repository root, install `uv` if needed, then create the isolated renderer environment and fetch the versioned assets:

```sh
python3 -m pip install --user uv
uv venv "$HOME/.local/share/simstim/voice/render"
uv pip install --python "$HOME/.local/share/simstim/voice/render/bin/python" \
  --require-hashes -r k151-standalone/host/requirements-cues.lock
mkdir -p "$HOME/.local/share/simstim/voice"
curl --fail --location --output "$HOME/.local/share/simstim/voice/kokoro-v1.0.onnx" \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl --fail --location --output "$HOME/.local/share/simstim/voice/voices-v1.0.bin" \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

If user-site pip installs are disabled, install `uv` with its official installer. Set `SIMSTIM_CUE_MODEL`, `SIMSTIM_CUE_VOICES`, or `SIMSTIM_CUE_PYTHON` to override the defaults; the CMake build passes these environment values through as cache variables and fails on missing or hash-mismatched assets.

## Generate, test and build

From the repository root, the pinned ESP-IDF build script generates the cues during CMake configure and places artifacts under the selected build directory only:

```sh
SIMSTIM_BUILD_DIR="$HOME/.cache/simstim-k151-dual" k151-standalone/build.sh dual
```

The standalone PCM gate is:

```sh
k151-standalone/host/test_cues.sh
```

It regenerates under `build/cues/`, compiles the exact generated firmware C data with warnings-as-errors, and checks all three clips are non-silent and distinct. Set `BUILD_DIR` to move these artifacts.

For another model/Python location, export the matching `SIMSTIM_CUE_*` variable before invoking `build.sh`. CMake fails if a dependency is missing or rendering fails; it does not fall back to silence. After the firmware build, inspect the linked ELF/map and verify all three symbols (`cue_online`, `cue_no_update`, `cue_check_failed`) are present.

## Public host verification (explicit one-shot command)

`wireless-control/cli.py verify-cue {online|no_update|check_failed}` is the opt-in public check. Invoke one cue explicitly, for example `python3 wireless-control/cli.py --socket "$SIMSTIM_OPERATOR_SOCKET" verify-cue no_update`. It sends one `VERIFY_CUE <cue> <nonce>` over the current authenticated session and never retries or reconnects. The command is serialized under the session I/O lock. It binds the exact named firmware playback receipt and transaction completion to the same TLS-authenticated peer identity, ELF digest matching the configured pin, transport, session object, and generation at dispatch and result. A timeout or disconnect is indeterminate; do not repeat automatically.

For `online`, firmware emits an exact `SIMSTIM_SPEAKER cue=online ... verify=<nonce>` playback receipt followed by `VERIFY_DONE`. For either tap cue, firmware first expires and checks its staged-brief slot; a nonempty slot rejects the check without consuming it. It then runs the existing `TAP_CHECK`/`TAP_FETCH` exchange. During this one command only, the authenticated host handler answers that fetch from an ephemeral provider (`TAP_NONE <id>` for `no_update`, `TAP_FAIL <id>` for `check_failed`); it does not call the normal attention/work-progress provider, read or change the brief queue, synthesize dynamic speech, or use staged audio. Acceptance requires the matching named cue playback record before the exact `TAP_DONE <id> none|failed` and `VERIFY_DONE <nonce> tap_id=<id> outcome=...` records. Wrong, absent, stale, duplicate, or reordered receipts fail closed.

The command is an actuation: it plays audio and is intended only for an explicitly selected, authenticated device with the expected ELF. It does not establish acoustic intelligibility or audibility. Do not run against a live service/state for test purposes; do not publish, stage, or alter queue data. Use an isolated host process/operator socket and its protected credentials/pin for any authorized device acceptance. Normal `speak`, `tap-check`, staged playback, and ordinary firmware receipt formats remain unchanged; only `verify-cue` uses the nonce-tagged records. No hardware, service, audio, or message operation is part of the repository tests.

## Intelligibility acceptance

The verifier uses ordinary open-vocabulary Whisper decoding, with no cue phrase prompt or restricted grammar. It tests both the mono PCM linked into firmware and software reconstruction of the speaker path: signed integer half-volume, duplicated stereo channels, and downmix for ASR. WAVs are written beside generated PCM under the build directory.

```sh
uv venv build/asr-venv
uv pip install --python build/asr-venv/bin/python \
  --require-hashes -r k151-standalone/host/requirements-cues-asr.lock
SIMSTIM_ASR_PYTHON="$PWD/build/asr-venv/bin/python" \
SIMSTIM_ASR_MODEL=Systran/faster-whisper-small \
  BUILD_DIR="$PWD/build/cues" k151-standalone/host/test_cues.sh
```

The recorded open-vocabulary transcripts from the verified PCM run:

| Intended cue | Firmware PCM | Half-volume stereo speaker-path PCM |
| --- | --- | --- |
| Online | `online` | `Online` |
| No update | `No update.` | `No update.` |
| Check failed | `Check failed.` | `Check failed.` |

Both PCM paths passed. This is software PCM/ASR evidence only; it does not assert physical playback or audibility. Firmware integration is verified separately by the clean-export ESP-IDF ELF build and linked-symbol check; neither test involves hardware.
