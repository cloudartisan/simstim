import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "host" / "motor_behavior_harness.c"
FAKES = ROOT / "host" / "fakes"
CONFIG_TEMPLATE = ROOT / "public_config.h.in"
SCRATCH = Path.home() / ".hermes" / "cache" / "scratch" / "k151-motor-behavior"

# Deliberately synthetic settings: 4 raw units/degree, zero at 500, and
# generous limits around the harness's bounded center/survey trajectory.
SYNTHETIC = {
    "CALIBRATION_ENABLED": 1,
    "YAW_ZERO_RAW": 500,
    "PITCH_ZERO_RAW": 500,
    "RAW_PER_DEG_NUM": 4,
    "RAW_PER_DEG_DEN": 1,
    "YAW_DIRECTION": 1,
    "PITCH_DIRECTION": 1,
    "YAW_MIN_DEG": -35,
    "YAW_MAX_DEG": 35,
    "PITCH_MIN_DEG": 0,
    "PITCH_MAX_DEG": 40,
}

CONFIGS = {
    "synthetic": SYNTHETIC,
    "omitted": {key: 0 for key in SYNTHETIC},
    "partial": {**{key: 0 for key in SYNTHETIC}, "YAW_ZERO_RAW": 500},
    "invalid": {**SYNTHETIC, "YAW_DIRECTION": 0},
}


def render_config(values):
    template = CONFIG_TEMPLATE.read_text()
    for name, value in values.items():
        template = template.replace(f"@SIMSTIM_MOTOR_{name}@", str(value))
    for name in (
        "LINK_HOST", "LINK_PORT", "LINK_IDENTITY", "DEVICE_HOSTNAME", "BLE_PEER_ADDRESS"
    ):
        template = template.replace(f"@SIMSTIM_{name}@", '"test"' if name != "LINK_PORT" else "1")
    if "@" in template:
        raise AssertionError("unexpanded placeholder in generated public config")
    return template


class MotorBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        obj = SCRATCH / "servo_position.o"
        subprocess.run(
            ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-c",
             str(ROOT / "main/servo_position.cpp"), "-o", str(obj)], check=True
        )
        cls.binaries = {}
        for name, values in CONFIGS.items():
            include = SCRATCH / name
            include.mkdir(parents=True, exist_ok=True)
            (include / "simstim_public_config.h").write_text(render_config(values))
            binary = SCRATCH / f"motor-{name}"
            compile_args = ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror"]
            if name in ("omitted", "partial"):
                # Public config intentionally renders an absent scale as zero;
                # the production source's guarded denial path is what is tested.
                compile_args.append("-Wno-error=div-by-zero")
            compile_args.extend(["-I", str(include), "-I", str(FAKES), str(HARNESS), str(obj), "-o", str(binary)])
            subprocess.run(compile_args, check=True, capture_output=True, text=True)
            cls.binaries[name] = binary

    def run_scenario(self, name, config="synthetic"):
        return subprocess.run([str(self.binaries[config]), name], capture_output=True,
                              text=True, timeout=15)

    def test_omitted_partial_and_invalid_calibration_fail_closed_before_cues_or_writes(self):
        for config in ("omitted", "partial", "invalid"):
            with self.subTest(config=config):
                result = self.run_scenario("calibration_denied", config)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("device_calibration_missing_or_invalid", result.stdout)

    def test_synthetic_calibration_is_explicitly_ready(self):
        result = self.run_scenario("synthetic_ready")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_probe_filters_self_echo_reads_both_servos_and_only_toggles_rail_gate(self):
        result = self.run_scenario("probe")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("action=probe", result.stdout)
        self.assertIn("gate_before_latch=0", result.stdout)
        self.assertIn("gate_after_latch=0", result.stdout)
        self.assertIn("vm_en_latch_off_verified=1", result.stdout)
        self.assertIn("yaw_id=1", result.stdout)
        self.assertIn("yaw_mode=position", result.stdout)
        self.assertIn("pitch_id=2", result.stdout)
        self.assertIn("writes=rail_gate_only", result.stdout)

    def test_bad_status_checksum_fails_closed_and_leaves_rail_off(self):
        result = self.run_scenario("probe_bad_checksum")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=failed", result.stdout)
        self.assertIn("vm_en_latch_off_verified=1", result.stdout)

    def test_probe_reports_nonposition_zero_limit_mode_without_writing_servo(self):
        result = self.run_scenario("probe_motor_mode")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("yaw_mode=non_position_zero_limits", result.stdout)
        self.assertIn("yaw_torque=0", result.stdout)

    def test_probe_refuses_to_interrupt_an_already_enabled_rail(self):
        result = self.run_scenario("probe_gate_already_on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("rail_was_already_on", result.stdout)
        self.assertIn("gate_after_latch=1", result.stdout)

    def test_start_pose_read_retries_for_delayed_wake_and_moves_from_fresh_pose(self):
        result = self.run_scenario("wake_retry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=ok", result.stdout)

    def test_scs_motor_mode_refuses_position_motion_without_goal_write(self):
        result = self.run_scenario("pwm_guard")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("position_mode_unconfirmed", result.stdout)
        self.assertNotIn("goal", result.stdout)

    def test_inverted_position_limits_refuse_motion(self):
        result = self.run_scenario("velocity_guard")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("position_mode_unconfirmed", result.stdout)
        self.assertNotIn("goal", result.stdout)

    def test_survey_rejects_poses_outside_synthetic_calibration_bounds(self):
        for scenario in ("outside_factory_low", "outside_factory_high"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_survey_enters_inward_slowly_from_near_boundary_and_releases(self):
        result = self.run_scenario("survey_entry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_survey_delayed_profile_onset(self):
        result = self.run_scenario("survey_delayed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_survey_ignored_goal_stops_without_retry(self):
        result = self.run_scenario("survey_ignored_goal")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stage=survey_waypoint_1", result.stdout)
        self.assertIn("yaw_raw=500", result.stdout)

    def test_survey_stall_stops_without_retry(self):
        result = self.run_scenario("survey_stalled")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stage=survey_waypoint_1", result.stdout)
        self.assertIn("yaw_raw=440", result.stdout)

    def test_absolute_home_from_offset(self):
        result = self.run_scenario("home")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_survey_keeps_both_waypoints_and_returns_to_synthetic_home(self):
        result = self.run_scenario("survey")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unsupported_calibrated_pose_is_rejected_before_cue_or_motion(self):
        result = self.run_scenario("unsupported_pose")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unsupported_start_pose", result.stdout)


if __name__ == "__main__":
    unittest.main()
