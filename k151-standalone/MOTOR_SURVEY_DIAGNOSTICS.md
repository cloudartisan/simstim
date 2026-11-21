# Motion-only survey diagnostic

This firmware diagnostic runs the normal calibrated pan, tilt and return trajectory on the physical unit. Unlike `LOOK_SURVEY`, it does not call the camera, stage images or enable a spoken response. It is not a calibration procedure: use only within independently established travel limits. The ordinary survey does not emit the new per-waypoint raw records.

The operator protocol sends `LOOK_MOTION <nonce>` once; the nonce is 16 lower-case hexadecimal characters. Match `MOTION_BEGIN` and `MOTION_END` to that nonce on the same authenticated session. A missing or mismatched end is not success; do not send the motion again to recover its receipt.

Each `LOOK_MOTOR result=event` record carries a per-boot run number. `start` is an observation without a requested target (`-1` on both axes). `centre`, `view1`, `view2` and `return` identify the requested and last observed raw positions on both axes. `state=failed` does not establish that the waypoint was reached. A missing readback is `-1`; readings from a failed move need not be contemporaneous. Keep raw records private; publish the firmware revision, transport and outcome, not the unit's settings.

The `cleanup` record separates the return result, both servos' moving flags **before** torque release, torque release and rail-off. On a failed trajectory idle is unverified even if cleanup succeeds. No motion is replayed to obtain a missing measurement. Idle before release is not a post-release position measurement.

From the repository root, run `python3 -m unittest -v k151-standalone/host/test_motor_safety.py k151-standalone/host/test_look_camera_contract.py`. These tests compile the motor code with synthetic board I/O; they do not confirm physical movement.
