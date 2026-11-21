# K151 hardware acceptance

## Proximity on both radios — firmware source `3b45976`

On one K151, the same privately configured, motor-disabled build (ELF `b92780a3074bf95a9e56f08d12fd2626e964b261fd282bb04d86a5391ba93d72`) returned six fresh proximity readings on authenticated Wi-Fi and six on authenticated Bluetooth. Each group ran on one session of its specified transport. The previous firmware was restored and authenticated again after the trial.

This trial did not exercise motion, camera capture, touch, a spoken survey or any of the three prompts. No on-device listening result was collected.

## Bluetooth camera with standard discovery responders

A privately configured, motor-disabled candidate (ELF fingerprint `e7e56505`) authenticated over Bluetooth on one K151. Six fresh proximity queries succeeded on the same authenticated session, the last at 65.1 seconds. A single stationary camera request then delivered a complete 320 × 240 RGB565 frame (153,600 bytes) without a reconnect or retry. The image was not retained. The previous firmware and boot selectors were restored and verified, and the previous host pin authenticated the restored unit over Wi-Fi.

The candidate initialises the standard GAP and GATT services. This result does not establish why an earlier image disconnected, prove Wi-Fi authentication on this candidate, or certify an unconfigured build from the public tree. Motion, surveys and prompt playback were not exercised in this trial.

## Earlier tests — firmware source `400aaae`

These results are for a temporary, privately configured, **motor-disabled** build of `400aaaea42a9fbd727f701755e9630b5528d100b` on one K151, tested with its deployed companion host. They do not certify the default image or every host installation. The previous firmware and host pin were restored and the board authenticated again after testing. No unit settings, flash images or room media are included.

| Operation | Authenticated Wi-Fi | Authenticated Bluetooth |
|---|---|---|
| Proximity and touch-state queries | Fresh replies | Fresh replies |
| `Online` prompt | Device reported playback | Device reported playback |
| Stage a spoken test update, run diagnostic tap, read slot | 55,296 bytes staged; playback receipt; slot empty | 55,296 bytes staged; playback receipt; slot empty |
| Stationary camera frame | Complete 320 × 240 RGB565 frame, 153,600 bytes, not retained | **Failed**: negotiated MTU 247; connection broke during the frame request after 26 seconds. Cause undetermined. |
| Motion, return home, three-view spoken survey | Not tested on this build; motors disabled | Not tested on this build; motors disabled |

A diagnostic tap is not a physical screen-release event. Staged playback does not test fetching a new update on an empty slot. Neither is credited here. No listener confirmed audibility. See [cue verification](CUES.md) for the separate decoded-audio checks of *Online*, *No update* and *Check failed*. A successful simulated bulk transfer is not a physical camera pass.
