import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "main" / "look_camera.c"


class CameraFailureReceiptContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text()

    def test_diagnostics_are_allowlisted_and_wire_routed(self):
        stages = set(re.findall(r'(?:camera_failure|CAM_FAIL)\("([a-z_]+)"', self.source))
        self.assertTrue({"video_init", "device_open", "buffer_alloc", "buffer_map"} <= stages)
        self.assertIn('wire_printf("LOOK_DIAG stage=%s esp=%d errno=%d dma_free=%u dma_largest=%u\\n"', self.source)
        self.assertIn("MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA", self.source)
        self.assertNotRegex(self.source, r'LOOK_DIAG[^\n]*(?:ssid|password|serial|base64|frame_data)')

    def test_synchronous_capture_removes_internal_worker_and_queue(self):
        self.assertNotRegex(self.source, r"xTaskCreate|xQueue|6144u|StackType_t|StaticTask_t")
        self.assertIn("if (index != s_frame_count || s_frame_count >= SURVEY_MAX_FRAMES)", self.source)
        self.assertIn("s_frames[s_frame_count++]", self.source)
        self.assertIn("CAM_TIMEOUT_MS 1500", self.source)
        self.assertIn("Called synchronously at each settled pose", self.source)
        self.assertIn("session_close(&survey_session)", self.source)
        self.assertIn("heap_caps_free(s_frames[i].data)", self.source)

    def test_survey_requires_exactly_three_ordered_captures(self):
        self.assertIn("#define SURVEY_MAX_FRAMES 3u", self.source)
        self.assertIn("s_survey_ok && s_frame_count==SURVEY_MAX_FRAMES", self.source)
        self.assertIn("index != s_frame_count", self.source)

    def test_worker_budget_is_zero_internal_stack_and_zero_task_objects(self):
        self.assertNotIn("SURVEY_WORKER_STACK_BYTES", self.source)
        self.assertNotIn("s_worker_stack", self.source)
        self.assertNotIn("s_worker_tcb", self.source)
        self.assertNotIn("s_glance_queue", self.source)
        self.assertNotIn("survey_worker", self.source)

    def test_survey_cleanup_frees_partial_frames_and_suppresses_partial_output(self):
        self.assertIn("bool ok=s_survey_ok && s_frame_count==SURVEY_MAX_FRAMES", self.source)
        finish = self.source[self.source.index("bool camera_survey_finish") : self.source.index("bool look_camera_capture")]
        self.assertLess(finish.index("session_close(&survey_session)"), finish.index("wire_printf(\"LOOK_GLANCE"))
        self.assertLess(finish.index("if (ok)"), finish.index("heap_caps_free(s_frames[i].data)"))
        self.assertIn("heap_caps_free(s_frames[i].data)", finish)

    def test_motor_choreography_invokes_captures_only_at_settled_points(self):
        motor = (SOURCE.parent / "look_motor.c").read_text()
        survey = motor[motor.index("bool motor_survey_impl("):]
        initial = survey.index("glance(0, context)")
        loop = survey.index("for (unsigned i = 0; i < MOTOR_VIEWS; ++i)", initial)
        move = survey.index("!move_pose(&requested, &actual)", loop)
        completed = survey.index("completed++", move)
        next_glance = survey.index("glance(completed, context)", completed)
        self.assertLess(initial, loop)
        self.assertLess(move, completed)
        self.assertLess(completed, next_glance)
        self.assertIn("#define MOTOR_VIEWS 2", motor)

    def test_usb_command_dispatch_is_single_task_and_media_waits_for_finish(self):
        speaker = (SOURCE.parent / "speaker.c").read_text()
        survey = speaker[speaker.index('if(!strcmp(cmd,"LOOK_SURVEY"))'):]
        self.assertLess(survey.index("motor_survey(bus,camera_survey_glance,NULL)"), survey.index("camera_survey_finish()"))
        self.assertLess(survey.index("camera_survey_finish()"), survey.index("board_look_end()"))
        self.assertIn("controls(void*bus)", speaker)
        self.assertNotIn("wire_printf", self.source[self.source.index("void camera_survey_glance"):self.source.index("bool camera_survey_finish")])

    def test_large_converted_frames_use_psram(self):
        self.assertIn("heap_caps_malloc(CAM_RAW_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)", self.source)
        self.assertIn("heap_caps_free(s_frames[i].data)", self.source)
        self.assertIn("heap_caps_free(data)", self.source)

    def test_calibrated_camera_configuration_is_unchanged(self):
        expected = [
            "CAM_D0 GPIO_NUM_39", "CAM_D1 GPIO_NUM_40", "CAM_D2 GPIO_NUM_41",
            "CAM_D3 GPIO_NUM_42", "CAM_D4 GPIO_NUM_15", "CAM_D5 GPIO_NUM_16",
            "CAM_D6 GPIO_NUM_48", "CAM_D7 GPIO_NUM_47", "CAM_VSYNC GPIO_NUM_46",
            "CAM_HREF GPIO_NUM_38", "CAM_PCLK GPIO_NUM_45", "CAM_XCLK_HZ 20000000u",
        ]
        for setting in expected:
            with self.subTest(setting=setting):
                self.assertIn(setting, self.source)


if __name__ == "__main__":
    unittest.main()
