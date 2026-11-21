#include <assert.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include "fake_esp.h"
#include "simstim_public_config.h"

/* Synthetic calibration used only by this deterministic host fixture. */
static int test_raw_at_deg(int zero, int deg, int direction) {
    if (SIMSTIM_MOTOR_RAW_PER_DEG_NUM <= 0 || SIMSTIM_MOTOR_RAW_PER_DEG_DEN <= 0) return zero;
    return zero + direction * deg * SIMSTIM_MOTOR_RAW_PER_DEG_NUM / SIMSTIM_MOTOR_RAW_PER_DEG_DEN;
}

static uint8_t expander[16] = {0};
static uint8_t regs[3][256] = {{0}};
static unsigned char uart_queue[4096];
static size_t queue_len = 0, queue_pos = 0;
static TickType_t fake_ticks = 0;
static int bad_checksum = 0;
static int goal_writes = 0;
static int cue_calls = 0;
static int torque_writes = 0;
static int other_servo_writes = 0;
static int read_failures_remaining = 0;
static int goal_history[2][16];
static int goal_profile_ms[2][16];
static int goal_profile_speed[2][16];
static int goal_history_count[2] = {0, 0};
static int delayed_onset_ms;
static int ignore_next_goal;
static int stall_next_goal;
static int motion_active[3];
static int motion_start_pos[3], motion_goal[3];
static TickType_t motion_started[3];
static int motion_duration[3], motion_delay[3], motion_stall[3];

#include "../../main/look_motor.c"
int wire_printf(const char *format, ...) {
    va_list args;
    va_start(args, format);
    int result = vprintf(format, args);
    va_end(args);
    return result;
}

static uint8_t check_sum(const uint8_t *p, size_t n) {
    unsigned sum = 0;
    for (size_t i = 2; i < n; ++i) sum += p[i];
    return (uint8_t)~sum;
}
static void queue_byte(uint8_t b) { assert(queue_len < sizeof(uart_queue)); uart_queue[queue_len++] = b; }
static void queue_frame(uint8_t id, const uint8_t *data, uint8_t n) {
    uint8_t f[32] = {0xff, 0xff, id, (uint8_t)(n + 2), 0};
    if (n) memcpy(&f[5], data, n);
    f[n + 5] = check_sum(f, n + 5);
    if (bad_checksum) f[n + 5] ^= 0x01;
    for (size_t i = 0; i < (size_t)n + 6; ++i) queue_byte(f[i]);
}
static void update_motion(uint8_t id) {
    if (!motion_active[id]) return;
    TickType_t elapsed = fake_ticks - motion_started[id];
    if (elapsed < (TickType_t)motion_delay[id]) {
        regs[id][66] = 0; /* deliberately expose pre-onset idle */
        return;
    }
    elapsed -= (TickType_t)motion_delay[id];
    regs[id][66] = 1;
    TickType_t duration = (TickType_t)motion_duration[id];
    TickType_t travel = motion_stall[id] ? duration / 2 : duration;
    if (elapsed >= travel) {
        int pos = motion_start_pos[id] +
            (motion_goal[id] - motion_start_pos[id]) * (int)travel / (int)duration;
        regs[id][56] = (uint8_t)(pos >> 8); regs[id][57] = (uint8_t)pos;
        if (!motion_stall[id]) {
            regs[id][56] = (uint8_t)(motion_goal[id] >> 8);
            regs[id][57] = (uint8_t)motion_goal[id];
            regs[id][66] = 0; motion_active[id] = 0;
        }
    } else {
        int pos = motion_start_pos[id] +
            (motion_goal[id] - motion_start_pos[id]) * (int)elapsed / (int)duration;
        regs[id][56] = (uint8_t)(pos >> 8); regs[id][57] = (uint8_t)pos;
    }
}
static void init_servo(uint8_t id, int pos) {
    regs[id][3] = 0x12; regs[id][4] = 0x34; regs[id][5] = id;
    regs[id][33] = 0;                    /* Feetech mode: position */
    regs[id][9] = 0; regs[id][10] = 0;   /* min-angle word */
    regs[id][11] = 0x03; regs[id][12] = 0xff; /* max-angle word */
    regs[id][40] = 0; regs[id][56] = (uint8_t)(pos >> 8); regs[id][57] = (uint8_t)pos;
    regs[id][60] = 0; regs[id][61] = 10; regs[id][62] = 75; regs[id][63] = 30;
    regs[id][66] = 0;
}
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t bus, const i2c_device_config_t *cfg, i2c_master_dev_handle_t *out) {
    (void)bus; assert(cfg->device_address == 0x6f); *out = (void *)0x2; return ESP_OK;
}
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t dev, const uint8_t *tx, size_t txlen, uint8_t *rx, size_t rxlen, int timeout) {
    (void)dev; (void)timeout; assert(txlen == 1 && rxlen == 1); rx[0] = expander[tx[0]]; return ESP_OK;
}
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t dev, const uint8_t *tx, size_t n, int timeout) {
    (void)dev; (void)timeout; assert(n == 2); expander[tx[0]] = tx[1]; return ESP_OK;
}
esp_err_t uart_param_config(uart_port_t u, const uart_config_t *c) { (void)u; assert(c->baud_rate == 1000000); return ESP_OK; }
esp_err_t uart_set_pin(uart_port_t u, int tx, int rx, int rts, int cts) { (void)u; (void)rts; (void)cts; assert(tx == 6 && rx == 7); return ESP_OK; }
bool uart_is_driver_installed(uart_port_t u) { (void)u; return true; }
esp_err_t uart_driver_install(uart_port_t u, int rx, int tx, int q, void *event, int flags) { (void)u;(void)rx;(void)tx;(void)q;(void)event;(void)flags;return ESP_OK; }
void uart_flush_input(uart_port_t u) { (void)u; queue_len = queue_pos = 0; }
esp_err_t uart_wait_tx_done(uart_port_t u, TickType_t t) { (void)u;(void)t;return ESP_OK; }
int uart_write_bytes(uart_port_t u, const char *data, size_t n) {
    (void)u; const uint8_t *tx = (const uint8_t *)data;
    assert(n >= 8 && tx[0] == 0xff && tx[1] == 0xff);
    for (size_t i=0; i<n; ++i) queue_byte(tx[i]); /* model TX self-echo */
    uint8_t id = tx[2], inst = tx[4];
    if (inst == 0x02) {
        uint8_t count = tx[6], payload[32] = {0};
        assert(id > 0 && id < 3 && count <= sizeof(payload));
        if (tx[5] == 56 && count == 2 && read_failures_remaining > 0) {
            read_failures_remaining--;
            return (int)n;
        }
        if (tx[5] == 66 || tx[5] == 56) update_motion(id);
        for (uint8_t i=0; i<count; ++i) payload[i] = regs[id][(uint8_t)(tx[5] + i)];
        queue_frame(id, payload, count);
    } else if (inst == 0x03) {
        uint8_t addr = tx[5], count = (uint8_t)(n - 7);
        assert(id > 0 && id < 3);
        if (addr == 42 && count == 6) {
            int position = (tx[6] << 8) | tx[7];
            int duration = (tx[8] << 8) | tx[9];
            goal_writes++;
            if (goal_history_count[id-1] < 16) {
                int n = goal_history_count[id-1]++;
                goal_history[id-1][n] = position;
                goal_profile_ms[id-1][n] = duration;
                goal_profile_speed[id-1][n] = (tx[10] << 8) | tx[11];
            }
            if (duration == 0) {
                regs[id][56] = tx[6]; regs[id][57] = tx[7]; regs[id][66] = 0;
            } else if (ignore_next_goal) {
                ignore_next_goal = 0;
            } else {
                motion_start_pos[id] = (regs[id][56] << 8) | regs[id][57];
                motion_goal[id] = position; motion_started[id] = fake_ticks;
                motion_duration[id] = duration; motion_delay[id] = delayed_onset_ms;
                motion_stall[id] = stall_next_goal; delayed_onset_ms = 0; stall_next_goal = 0;
                motion_active[id] = 1; regs[id][66] = 0;
            }
        } else if (addr == 40 && count == 1) {
            torque_writes++; regs[id][40] = tx[6];
        } else {
            other_servo_writes++;
        }
        queue_frame(id, NULL, 0);
    }
    return (int)n;
}
int uart_read_bytes(uart_port_t u, uint8_t *out, uint32_t n, TickType_t wait) {
    (void)u;
    if (queue_pos == queue_len) { fake_ticks += wait ? wait : 1; return 0; }
    size_t got = queue_len - queue_pos; if (got > n) got = n;
    memcpy(out, uart_queue + queue_pos, got); queue_pos += got; return (int)got;
}
TickType_t xTaskGetTickCount(void) { return fake_ticks; }
void vTaskDelay(TickType_t ticks) { fake_ticks += ticks ? ticks : 1; }

static void defaults(void) {
    expander[0x03] = 0x01; expander[0x05] = 0x00;
    init_servo(1, test_raw_at_deg(SIMSTIM_MOTOR_YAW_ZERO_RAW, 0, SIMSTIM_MOTOR_YAW_DIRECTION));
    init_servo(2, test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 30, SIMSTIM_MOTOR_PITCH_DIRECTION));
}
static void synthetic_cue(unsigned view, void *context) { (void)view; (void)context; cue_calls++; }
int main(int argc, char **argv) {
    assert(argc == 2); defaults();
    if (strcmp(argv[1], "writepos_wire_words") == 0) {
        assert(simstim_timed_position(1, 0x123, 0x456));
        assert(goal_writes == 1 && goal_history_count[0] == 1);
        assert(goal_history[0][0] == 0x123);
        assert(goal_profile_ms[0][0] == 0x456);
        assert(goal_profile_speed[0][0] == 0);
        return 0;
    }
    if (strcmp(argv[1], "synthetic_ready") == 0) {
        assert(motor_calibration_ready());
        return 0;
    }
    if (strcmp(argv[1], "calibration_denied") == 0) {
        assert(!motor_calibration_ready());
        assert(!motor_survey((void *)0x1, synthetic_cue, NULL));
        assert(!motor_look((void *)0x1, "RIGHT"));
        assert(cue_calls == 0);
        assert((expander[0x05] & 1) == 0);
        assert(goal_writes == 0 && torque_writes == 0 && other_servo_writes == 0);
        return 0;
    }
    if (strcmp(argv[1], "unsupported_pose") == 0) {
        assert(motor_calibration_ready());
        int pitch = test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW,
            SIMSTIM_MOTOR_PITCH_MAX_DEG + 1, SIMSTIM_MOTOR_PITCH_DIRECTION);
        regs[2][56] = (uint8_t)(pitch >> 8); regs[2][57] = (uint8_t)pitch;
        assert(!motor_survey((void *)0x1, synthetic_cue, NULL));
        assert(cue_calls == 0);
        assert((expander[0x05] & 1) == 0);
        assert(goal_writes == 0 && torque_writes >= 2);
        return 0;
    }
    if (strcmp(argv[1], "probe") == 0) {
        int ok = motor_look((void *)0x1, "PROBE");
        assert(ok); assert((expander[0x05] & 1) == 0);
        assert(goal_writes == 0 && torque_writes == 0 && other_servo_writes == 0);
        return 0;
    }
    if (strcmp(argv[1], "probe_bad_checksum") == 0) {
        bad_checksum = 1;
        int ok = motor_look((void *)0x1, "PROBE");
        assert(!ok); assert((expander[0x05] & 1) == 0);
        assert(goal_writes == 0 && torque_writes == 0 && other_servo_writes == 0);
        return 0;
    }
    if (strcmp(argv[1], "probe_motor_mode") == 0) {
        regs[1][9] = regs[1][10] = regs[1][11] = regs[1][12] = 0;
        int ok = motor_look((void *)0x1, "PROBE");
        assert(ok); assert((expander[0x05] & 1) == 0);
        assert(goal_writes == 0 && torque_writes == 0 && other_servo_writes == 0);
        return 0;
    }
    if (strcmp(argv[1], "probe_gate_already_on") == 0) {
        expander[0x05] = 1;
        int ok = motor_look((void *)0x1, "PROBE");
        assert(!ok); assert((expander[0x05] & 1) == 1);
        assert(goal_writes == 0 && torque_writes == 0 && other_servo_writes == 0);
        return 0;
    }
    if (strcmp(argv[1], "wake_retry") == 0) {
        int pitch = test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 38, SIMSTIM_MOTOR_PITCH_DIRECTION);
        regs[2][56] = (uint8_t)(pitch >> 8); regs[2][57] = (uint8_t)pitch;
        read_failures_remaining = 8;
        int ok = motor_look((void *)0x1, "RIGHT");
        assert(ok); assert(read_failures_remaining == 0);
        assert(goal_history_count[0] >= 2 && goal_history[0][1] ==
               test_raw_at_deg(SIMSTIM_MOTOR_YAW_ZERO_RAW, 5, SIMSTIM_MOTOR_YAW_DIRECTION));
        assert((expander[0x05] & 1) == 0);
        return 0;
    }
    if (strcmp(argv[1], "outside_factory_low") == 0 ||
        strcmp(argv[1], "outside_factory_high") == 0) {
        int pitch = test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW,
            strcmp(argv[1], "outside_factory_low") == 0 ? SIMSTIM_MOTOR_PITCH_MIN_DEG - 1 : SIMSTIM_MOTOR_PITCH_MAX_DEG + 1,
            SIMSTIM_MOTOR_PITCH_DIRECTION);
        regs[2][56] = (uint8_t)(pitch >> 8); regs[2][57] = (uint8_t)pitch;
        int ok = motor_survey((void *)0x1, NULL, NULL);
        assert(!ok && goal_writes == 0);
        assert((expander[0x05] & 1) == 0);
        return 0;
    }
    if (strcmp(argv[1], "home") == 0) {
        assert(motor_look((void *)1, "HOME"));
        assert(goal_history[0][goal_history_count[0]-1] == test_raw_at_deg(SIMSTIM_MOTOR_YAW_ZERO_RAW, 0, SIMSTIM_MOTOR_YAW_DIRECTION));
        assert(goal_history[1][goal_history_count[1]-1] == test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 30, SIMSTIM_MOTOR_PITCH_DIRECTION));
        assert(regs[1][40] == 0 && regs[2][40] == 0 && !(expander[5] & 1)); return 0;
    }
    if (strcmp(argv[1], "survey") == 0) {
        int pitch = test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 30, SIMSTIM_MOTOR_PITCH_DIRECTION);
        regs[2][56] = (uint8_t)(pitch >> 8); regs[2][57] = (uint8_t)pitch;
        int ok = motor_survey((void *)0x1, NULL, NULL);
        assert(ok); assert((expander[0x05] & 1) == 0);
        assert(goal_history_count[0] >= 4 && goal_history_count[1] >= 4);
        for (int axis = 0; axis < 2; ++axis) {
            for (int i = 0; i < goal_history_count[axis]; ++i) {
                int raw = goal_history[axis][i];
                int zero = axis == 0 ? SIMSTIM_MOTOR_YAW_ZERO_RAW : SIMSTIM_MOTOR_PITCH_ZERO_RAW;
                int direction = axis == 0 ? SIMSTIM_MOTOR_YAW_DIRECTION : SIMSTIM_MOTOR_PITCH_DIRECTION;
                int deg = direction * (raw - zero) * SIMSTIM_MOTOR_RAW_PER_DEG_DEN / SIMSTIM_MOTOR_RAW_PER_DEG_NUM;
                int low = axis == 0 ? SIMSTIM_MOTOR_YAW_MIN_DEG : SIMSTIM_MOTOR_PITCH_MIN_DEG;
                int high = axis == 0 ? SIMSTIM_MOTOR_YAW_MAX_DEG : SIMSTIM_MOTOR_PITCH_MAX_DEG;
                assert(deg >= low && deg <= high);
            }
        }
        return 0;
    }
    if (strcmp(argv[1], "survey_entry") == 0) {
        int ok = motor_survey((void *)0x1, NULL, NULL);
        assert(ok && (expander[0x05] & 1) == 0);
        assert(goal_history_count[0] >= 4 && goal_history_count[1] >= 4);
        assert(goal_history[1][1] == test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 38, SIMSTIM_MOTOR_PITCH_DIRECTION));
        assert(goal_history[1][2] == test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 22, SIMSTIM_MOTOR_PITCH_DIRECTION));
        for (int i = 0; i < goal_history_count[0]; ++i)
            assert(goal_history[0][i] >= test_raw_at_deg(SIMSTIM_MOTOR_YAW_ZERO_RAW, SIMSTIM_MOTOR_YAW_MIN_DEG, SIMSTIM_MOTOR_YAW_DIRECTION) &&
                   goal_history[0][i] <= test_raw_at_deg(SIMSTIM_MOTOR_YAW_ZERO_RAW, SIMSTIM_MOTOR_YAW_MAX_DEG, SIMSTIM_MOTOR_YAW_DIRECTION));
        for (int i = 0; i < goal_history_count[1]; ++i)
            assert(goal_history[1][i] >= test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, SIMSTIM_MOTOR_PITCH_MIN_DEG, SIMSTIM_MOTOR_PITCH_DIRECTION) &&
                   goal_history[1][i] <= test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, SIMSTIM_MOTOR_PITCH_MAX_DEG, SIMSTIM_MOTOR_PITCH_DIRECTION));
        assert(regs[1][40] == 0 && regs[2][40] == 0);
        return 0;
    }
    if (strcmp(argv[1], "survey_delayed") == 0 ||
        strcmp(argv[1], "survey_ignored_goal") == 0 ||
        strcmp(argv[1], "survey_stalled") == 0) {
        if (strcmp(argv[1], "survey_delayed") == 0) delayed_onset_ms = 150;
        if (strcmp(argv[1], "survey_ignored_goal") == 0) ignore_next_goal = 1;
        if (strcmp(argv[1], "survey_stalled") == 0) stall_next_goal = 1;
        int ok = motor_survey((void *)0x1, NULL, NULL);
        if (strcmp(argv[1], "survey_delayed") == 0) {
            assert(ok && goal_history_count[1] >= 2);
        } else {
            assert(!ok && goal_history_count[1] == 2);
            assert(goal_history[1][1] == test_raw_at_deg(SIMSTIM_MOTOR_PITCH_ZERO_RAW, 38, SIMSTIM_MOTOR_PITCH_DIRECTION));
            assert(regs[1][40] == 0 && regs[2][40] == 0);
            assert((expander[0x05] & 1) == 0);
        }
        return 0;
    }
    if (strcmp(argv[1], "pwm_guard") == 0) {
        regs[1][9] = regs[1][10] = regs[1][11] = regs[1][12] = 0;
        int ok = motor_look((void *)0x1, "RIGHT");
        assert(!ok); assert(goal_writes == 0); assert((expander[0x05] & 1) == 0);
        return 0;
    }
    if (strcmp(argv[1], "velocity_guard") == 0) {
        regs[1][9] = 0x03; regs[1][10] = 0x84;
        regs[1][11] = 0; regs[1][12] = 100;
        int ok = motor_look((void *)0x1, "RIGHT");
        assert(!ok); assert(goal_writes == 0); assert((expander[0x05] & 1) == 0);
        return 0;
    }
    return 2;
}
