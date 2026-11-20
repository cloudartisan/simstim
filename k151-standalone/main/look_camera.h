#pragma once
#include <stdbool.h>
#include "driver/i2c_master.h"

/* One video session spans the survey. Capture runs synchronously on the
 * control task at each settled pose; frames remain in PSRAM until finish. */
bool camera_survey_begin(i2c_master_bus_handle_t bus);
void camera_survey_glance(unsigned index, void *ctx); /* bounded settled-pose capture */
bool camera_survey_finish(void); /* close sensor, emit only complete survey, free frames */

/* Optional single-frame capture outside a survey. */
bool look_camera_capture(i2c_master_bus_handle_t bus);
