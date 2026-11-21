/*
 * Conservative, opt-in K151/SCS0009 motion. Device calibration must be
 * supplied explicitly by the device owner; it is never read from or written
 * to device storage. Missing/invalid calibration disables all motion before
 * the servo power gate can be enabled.
 *
 * Motion timing follows the MIT-licensed stackchan.cc contract at pinned
 * upstream-stackchan-mcp 03fbd92774c37844e11ea097e632df28f2a5f4e5:
 * WriteHeadAngles (4757-4809) -> ServoDelegatedMotionDriver::StartMove
 * (1765-1809) -> SCS WritePos(pos, duration, 0) (2072-2082). This standalone
 * adapter keeps native SCS motion; it does not add a second interpolator.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "wire_io.h"
#include "simstim_public_config.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/uart.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define MOTOR_PY32_ADDR       0x6f
#define MOTOR_VM_EN_REG       0x05
#define MOTOR_UART            UART_NUM_1
#define MOTOR_UART_BAUD       1000000
#define MOTOR_UART_TX         GPIO_NUM_6
#define MOTOR_UART_RX         GPIO_NUM_7
#define MOTOR_YAW_ID          1
#define MOTOR_PITCH_ID        2
#define MOTOR_REG_TORQUE      40

#define MOTOR_REG_PRESENT_POS 56
#define MOTOR_REG_MOVING      66
#define MOTOR_REG_VERSION     3
#define MOTOR_REG_ID          5
/* Position mode requires valid nonzero limits; generic mode bytes are ignored. */
#define MOTOR_REG_MIN_ANGLE   9
#define MOTOR_REG_VOLTAGE     62
#define MOTOR_REG_TEMPERATURE 63
#define MOTOR_REG_LOAD        60
#define MOTOR_WAKE_BUDGET_MS  5000
#define MOTOR_REPLY_TIMEOUT_MS 100
#define MOTOR_RX_CAPACITY     64

#define MOTOR_YAW_ZERO_RAW SIMSTIM_MOTOR_YAW_ZERO_RAW
#define MOTOR_PITCH_ZERO_RAW SIMSTIM_MOTOR_PITCH_ZERO_RAW
#define MOTOR_RAW_PER_DEG_NUM SIMSTIM_MOTOR_RAW_PER_DEG_NUM
#define MOTOR_RAW_PER_DEG_DEN SIMSTIM_MOTOR_RAW_PER_DEG_DEN
#define MOTOR_YAW_DIRECTION SIMSTIM_MOTOR_YAW_DIRECTION
#define MOTOR_PITCH_DIRECTION SIMSTIM_MOTOR_PITCH_DIRECTION
#define MOTOR_YAW_MIN_DEG SIMSTIM_MOTOR_YAW_MIN_DEG
#define MOTOR_YAW_MAX_DEG SIMSTIM_MOTOR_YAW_MAX_DEG
#define MOTOR_PITCH_MIN_DEG SIMSTIM_MOTOR_PITCH_MIN_DEG
#define MOTOR_PITCH_MAX_DEG SIMSTIM_MOTOR_PITCH_MAX_DEG

/* Match stackchan.cc: WriteHeadAngles(speed_dps) and delegated StartMove. */
#define MOTOR_PAN_HALF_SPAN_DEG 30
#define MOTOR_TILT_HALF_SPAN_DEG 8
#define MOTOR_PROFILE_MS 1200

#define MOTOR_MOVE_TIMEOUT_MS 2200
#define MOTOR_POLL_MS 40
#define MOTOR_VIEWS 2

static i2c_master_dev_handle_t s_py32;
static bool s_uart_ready;
static bool s_busy;
static const char *s_stage = "preflight";
static TickType_t s_yaw_sample_tick, s_pitch_sample_tick;

static bool calibration_valid(void)
{
    return SIMSTIM_MOTOR_CALIBRATION_ENABLED &&
        MOTOR_YAW_ZERO_RAW >= 0 && MOTOR_YAW_ZERO_RAW <= 1023 &&
        MOTOR_PITCH_ZERO_RAW >= 0 && MOTOR_PITCH_ZERO_RAW <= 1023 &&
        MOTOR_RAW_PER_DEG_NUM > 0 && MOTOR_RAW_PER_DEG_NUM <= 1024 &&
        MOTOR_RAW_PER_DEG_DEN > 0 && MOTOR_RAW_PER_DEG_DEN <= 1024 &&
        (MOTOR_YAW_DIRECTION == -1 || MOTOR_YAW_DIRECTION == 1) &&
        (MOTOR_PITCH_DIRECTION == -1 || MOTOR_PITCH_DIRECTION == 1) &&
        MOTOR_YAW_MIN_DEG >= -180 && MOTOR_YAW_MAX_DEG <= 180 &&
        MOTOR_YAW_MIN_DEG < MOTOR_YAW_MAX_DEG &&
        MOTOR_PITCH_MIN_DEG >= -180 && MOTOR_PITCH_MAX_DEG <= 180 &&
        MOTOR_PITCH_MIN_DEG < MOTOR_PITCH_MAX_DEG &&
        (SIMSTIM_MOTOR_SURVEY_ENTRY_ENABLED == 0 ||
         (SIMSTIM_MOTOR_SURVEY_ENTRY_ENABLED == 1 &&
          SIMSTIM_MOTOR_SURVEY_ENTRY_MIN_PITCH_DEG >= -180 &&
          SIMSTIM_MOTOR_SURVEY_ENTRY_MIN_PITCH_DEG < MOTOR_PITCH_MIN_DEG));
}

bool motor_calibration_ready(void) { return calibration_valid(); }

typedef struct {
    int yaw_raw;
    int pitch_raw;
    int yaw_deg;
    int pitch_deg;
} motor_pose_t;

static int raw_for_deg(int zero, int deg, int direction)
{
#if SIMSTIM_MOTOR_RAW_PER_DEG_NUM > 0 && SIMSTIM_MOTOR_RAW_PER_DEG_DEN > 0
    return zero + direction * deg * MOTOR_RAW_PER_DEG_NUM / MOTOR_RAW_PER_DEG_DEN;
#else
    (void)zero; (void)deg; (void)direction;
    return 0;
#endif
}

static int deg_for_raw(int zero, int raw, int direction)
{
#if SIMSTIM_MOTOR_RAW_PER_DEG_NUM > 0 && SIMSTIM_MOTOR_RAW_PER_DEG_DEN > 0
    return direction * (raw - zero) * MOTOR_RAW_PER_DEG_DEN / MOTOR_RAW_PER_DEG_NUM;
#else
    (void)zero; (void)raw; (void)direction;
    return 0;
#endif
}

static bool pose_from_raw(int yaw_raw, int pitch_raw, motor_pose_t *pose,
                          bool allow_survey_entry)
{
    if (!pose || yaw_raw < 0 || yaw_raw > 1000 ||
        pitch_raw < 0 || pitch_raw > 1000) return false;
    pose->yaw_raw = yaw_raw;
    pose->pitch_raw = pitch_raw;
    pose->yaw_deg = deg_for_raw(MOTOR_YAW_ZERO_RAW, yaw_raw, MOTOR_YAW_DIRECTION);
    pose->pitch_deg = deg_for_raw(MOTOR_PITCH_ZERO_RAW, pitch_raw, MOTOR_PITCH_DIRECTION);
    bool normal_pitch = pose->pitch_deg >= MOTOR_PITCH_MIN_DEG &&
                        pose->pitch_deg <= MOTOR_PITCH_MAX_DEG;
    bool entry_pitch = allow_survey_entry && SIMSTIM_MOTOR_SURVEY_ENTRY_ENABLED &&
                       pose->pitch_deg >= SIMSTIM_MOTOR_SURVEY_ENTRY_MIN_PITCH_DEG &&
                       pose->pitch_deg < MOTOR_PITCH_MIN_DEG;
    return pose->yaw_deg >= MOTOR_YAW_MIN_DEG && pose->yaw_deg <= MOTOR_YAW_MAX_DEG &&
           (normal_pitch || entry_pitch);
}

static bool add_py32(i2c_master_bus_handle_t bus)
{
    if (s_py32) return true;
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = MOTOR_PY32_ADDR,
        .scl_speed_hz = 100000,
    };
    return i2c_master_bus_add_device(bus, &cfg, &s_py32) == ESP_OK;
}

static bool py32_read(uint8_t reg, uint8_t *value)
{
    return s_py32 && value &&
        i2c_master_transmit_receive(s_py32, &reg, 1, value, 1, 100) == ESP_OK;
}

static bool py32_write(uint8_t reg, uint8_t value)
{
    const uint8_t tx[2] = {reg, value};
    return s_py32 && i2c_master_transmit(s_py32, tx, sizeof(tx), 100) == ESP_OK;
}

static bool pull_bit(uint8_t reg,bool on){
    uint8_t v,w;
    if(!py32_read(reg,&v))return false;
    v=on?(uint8_t)(v|1u):(uint8_t)(v&~1u);
    return py32_write(reg,v) && py32_read(reg,&w) && (w&1u)==(unsigned)on;
}
static bool rail_set(bool enabled)
{
    uint8_t latch = 0xff, direction = 0xff;
    if (!py32_read(MOTOR_VM_EN_REG, &latch) || !py32_read(0x03, &direction) ||
        (direction & 1u) == 0) return false;
    latch = enabled ? (uint8_t)(latch | 1u) : (uint8_t)(latch & ~1u);
    /* Match Hal::io_expander_init's pull-up for active VM_EN; our boot
       path deliberately selected pull-down. Restore it after switch-off. */
    if(enabled && (!pull_bit(0x0b,false)||!pull_bit(0x09,true)))return false;
    if (!py32_write(MOTOR_VM_EN_REG, latch)) return false;
    if(!enabled && (!pull_bit(0x09,false)||!pull_bit(0x0b,true)))return false;
    uint8_t verify_latch = 0xff, verify_dir = 0xff;
    return py32_read(MOTOR_VM_EN_REG, &verify_latch) &&
           py32_read(0x03, &verify_dir) && (verify_dir & 1u) &&
           (((verify_latch & 1u) != 0) == enabled);
}

static uint8_t checksum(const uint8_t *packet, size_t n)
{
    uint32_t sum = 0;
    for (size_t i = 2; i < n; ++i) sum += packet[i];
    return (uint8_t)(~sum & 0xffu);
}

static bool uart_start(void)
{
    if (s_uart_ready) return true;
    uart_config_t cfg = {
        .baud_rate = MOTOR_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_param_config(MOTOR_UART, &cfg) != ESP_OK ||
        uart_set_pin(MOTOR_UART, MOTOR_UART_TX, MOTOR_UART_RX,
                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK)
        return false;
    if (!uart_is_driver_installed(MOTOR_UART) &&
        uart_driver_install(MOTOR_UART, 256, 0, 0, NULL, 0) != ESP_OK)
        return false;
    s_uart_ready = true;
    return true;
}

/*
 * Read one complete SCS status packet, discarding local half-duplex TX echo by
 * framing and checksum rather than assuming the echo is absent.  The scan is
 * bounded, resynchronises on FF FF, and accepts only the requested ID/length.
 */
static unsigned wire_rx, wire_echo, wire_bad_status;
static bool servo_exchange(const uint8_t *tx, size_t tx_len, uint8_t id,
                           uint8_t data_count, uint8_t *reply,
                           uint32_t timeout_ms)
{
    uint8_t rx[MOTOR_RX_CAPACITY];
    size_t used = 0;
    const size_t expected = (size_t)data_count + 6u;
    TickType_t started = xTaskGetTickCount();
    TickType_t budget = pdMS_TO_TICKS(timeout_ms);
    if (!tx || !reply || expected > MOTOR_RX_CAPACITY) return false;
    uart_flush_input(MOTOR_UART);
    if (uart_write_bytes(MOTOR_UART, (const char *)tx, tx_len) != (int)tx_len ||
        uart_wait_tx_done(MOTOR_UART, pdMS_TO_TICKS(100)) != ESP_OK) return false;
    while ((xTaskGetTickCount() - started) < budget) {
        uint8_t byte;
        if (uart_read_bytes(MOTOR_UART, &byte, 1, pdMS_TO_TICKS(2)) != 1) continue;
        if (used == sizeof(rx)) {
            memmove(rx, rx + 1, --used);
        }
        rx[used++] = byte;
        wire_rx++;
        if(used==tx_len && !memcmp(rx,tx,tx_len))wire_echo++;
        for (size_t i = 0; i + expected <= used; ++i) {
            if (rx[i] != 0xff || rx[i + 1] != 0xff || rx[i + 2] != id ||
                rx[i + 3] != data_count + 2) continue;
            uint32_t sum = rx[i + 2] + rx[i + 3] + rx[i + 4];
            for (uint8_t n = 0; n < data_count; ++n) sum += rx[i + 5 + n];
            if (rx[i + expected - 1] != (uint8_t)(~sum & 0xffu)) continue;
            if(rx[i+4]!=0){if(expected!=tx_len||memcmp(rx+i,tx,tx_len))wire_bad_status++;continue;}
            memcpy(reply, rx + i, expected);
            return true;
        }
    }
    return false;
}

static bool servo_read_timeout(uint8_t id, uint8_t reg, uint8_t count,
                               uint8_t *out, uint32_t timeout_ms)
{
    uint8_t tx[8] = {0xff, 0xff, id, 4, 0x02, reg, count, 0};
    uint8_t reply[16] = {0};
    const size_t reply_len = 6u + count;
    if (!out || count == 0 || reply_len > sizeof(reply)) return false;
    tx[7] = checksum(tx, 7);
    if (!servo_exchange(tx, sizeof(tx), id, count, reply, timeout_ms) || reply[4] != 0)
        return false;
    memcpy(out, &reply[5], count);
    return true;
}

static bool servo_read(uint8_t id, uint8_t reg, uint8_t count, uint8_t *out)
{
    return servo_read_timeout(id, reg, count, out, MOTOR_REPLY_TIMEOUT_MS);
}

extern bool simstim_timed_position(uint8_t, uint16_t, uint16_t);
bool simstim_servo_write(uint8_t id, uint8_t reg, const uint8_t *data, uint8_t n)
{
    uint8_t packet[16] = {0xff, 0xff, id, (uint8_t)(n + 3), 0x03, reg};
    uint8_t reply[6] = {0};
    size_t total = (size_t)n + 7;
    if (total > sizeof(packet) || (n && !data)) return false;
    if (n) memcpy(&packet[6], data, n);
    packet[6 + n] = checksum(packet, 6 + n);
    return servo_exchange(packet, total, id, 0, reply, MOTOR_REPLY_TIMEOUT_MS) &&
           reply[4] == 0;
}

/* The active local Feetech SCS0009 driver documents big-endian wire words. */
static bool read_word(uint8_t id, uint8_t reg, int *value)
{
    uint8_t b[2];
    if (!value || !servo_read(id, reg, sizeof(b), b)) return false;
    *value = ((int)b[0] << 8) | b[1];
    return *value <= 1023;
}

static bool read_pose(motor_pose_t *pose)
{
    int yaw, pitch;
    if (!read_word(MOTOR_YAW_ID, MOTOR_REG_PRESENT_POS, &yaw) ||
        !read_word(MOTOR_PITCH_ID, MOTOR_REG_PRESENT_POS, &pitch) ||
        !pose_from_raw(yaw, pitch, pose, false)) return false;
    s_yaw_sample_tick = s_pitch_sample_tick = xTaskGetTickCount();
    return true;
}

static bool torque_set(uint8_t id, bool enabled)
{
    uint8_t value = enabled ? 1 : 0, actual = 0xff;
    if (!simstim_servo_write(id, MOTOR_REG_TORQUE, &value, 1) ||
        !servo_read(id, MOTOR_REG_TORQUE, 1, &actual)) return false;
    return actual == value;
}

static bool torque_release(void)
{
    bool yaw_ok = torque_set(MOTOR_YAW_ID, false);
    bool pitch_ok = torque_set(MOTOR_PITCH_ID, false);
    return yaw_ok && pitch_ok;
}

/* StartMove delegates to SCS WritePos(pos, duration, speed=0). */
static bool target_profile(uint8_t id, int raw, uint16_t duration_ms)
{
    if (raw < 0 || raw > 1000) return false;
    return simstim_timed_position(id,(uint16_t)raw,duration_ms);
}

static bool target(uint8_t id, int raw)
{
    return target_profile(id, raw, MOTOR_PROFILE_MS);
}

/* Match the firmware-local ReadPos -> same-position hold snap suppressor. */
static bool hold_observed_pose(const motor_pose_t *pose)
{
    return target_profile(MOTOR_YAW_ID,pose->yaw_raw,0) && target_profile(MOTOR_PITCH_ID,pose->pitch_raw,0);
}

/* A zero MOVING bit immediately after WritePos is not completion: some SCS
 * units assert it only after starting the profile. Wait out the requested
 * profile, then require a fresh in-tolerance position and idle indication.
 * Never resend the goal. Reads/status faults fail closed. */
static bool await_profiled_position(uint8_t id, int goal_raw,
                                    uint16_t profile_ms, uint32_t grace_ms,
                                    motor_pose_t *last_pose, bool pitch_axis)
{
    const TickType_t started = xTaskGetTickCount();
    const TickType_t profile = pdMS_TO_TICKS(profile_ms);
    const TickType_t budget = pdMS_TO_TICKS((uint32_t)profile_ms + grace_ms);
    while ((xTaskGetTickCount() - started) < profile)
        vTaskDelay(pdMS_TO_TICKS(MOTOR_POLL_MS));

    while ((xTaskGetTickCount() - started) < budget) {
        uint8_t moving = 0xff;
        int raw = -1;
        if (!servo_read(id, MOTOR_REG_MOVING, 1, &moving) || moving > 1)
            return false;
        if (!read_word(id, MOTOR_REG_PRESENT_POS, &raw))
            return false;
        if (pitch_axis) {
            last_pose->pitch_raw = raw;
            last_pose->pitch_deg = deg_for_raw(MOTOR_PITCH_ZERO_RAW, raw, MOTOR_PITCH_DIRECTION);
            s_pitch_sample_tick = xTaskGetTickCount();
            if (raw < 0 || raw > 1000) return false;
        } else {
            last_pose->yaw_raw = raw;
            last_pose->yaw_deg = deg_for_raw(MOTOR_YAW_ZERO_RAW, raw, MOTOR_YAW_DIRECTION);
            s_yaw_sample_tick = xTaskGetTickCount();
            if (raw < 0 || raw > 1000) return false;
        }
        if (moving == 0 && abs(raw - goal_raw) <= 9) return true;
        vTaskDelay(pdMS_TO_TICKS(MOTOR_POLL_MS));
    }
    return false;
}

static bool torque_engage_if_released(uint8_t id)
{
    uint8_t torque = 0xff;
    if (!servo_read(id, MOTOR_REG_TORQUE, 1, &torque) || torque > 1) return false;
    return torque == 1 || torque_set(id, true);
}

static bool servo_position_limits(uint8_t id, int *min_limit, int *max_limit)
{
    uint8_t b[4];
    if (!servo_read(id, MOTOR_REG_MIN_ANGLE, sizeof(b), b)) return false;
    int low = ((int)b[0] << 8) | b[1];
    int high = ((int)b[2] << 8) | b[3];
    if (min_limit) *min_limit = low;
    if (max_limit) *max_limit = high;
    /* SCSCL/PWMMode marks continuous motor mode by zeroing both limits. */
    return !(low == 0 && high == 0) && high > low && high <= 1023;
}

static bool move_pose(const motor_pose_t *target_pose, motor_pose_t *actual)
{
    if (actual) *actual = (motor_pose_t){-1, -1, -999, -999};
    bool reached = false;
    if (!target_pose || !actual || target_pose->yaw_deg < -120 || target_pose->yaw_deg > 120 ||
        target_pose->pitch_deg < MOTOR_PITCH_MIN_DEG ||
        target_pose->pitch_deg > MOTOR_PITCH_MAX_DEG) goto done;
    int yaw_min, yaw_max, pitch_min, pitch_max;
    if (!servo_position_limits(MOTOR_YAW_ID, &yaw_min, &yaw_max) ||
        !servo_position_limits(MOTOR_PITCH_ID, &pitch_min, &pitch_max) ||
        target_pose->yaw_raw < yaw_min || target_pose->yaw_raw > yaw_max ||
        target_pose->pitch_raw < pitch_min || target_pose->pitch_raw > pitch_max) goto done;
    if (!torque_engage_if_released(MOTOR_YAW_ID) ||
        !torque_engage_if_released(MOTOR_PITCH_ID)) goto done;
    if (!target(MOTOR_YAW_ID, target_pose->yaw_raw) ||
        !target(MOTOR_PITCH_ID, target_pose->pitch_raw)) goto done;
    if (!await_profiled_position(MOTOR_YAW_ID, target_pose->yaw_raw,
                                 MOTOR_PROFILE_MS, MOTOR_MOVE_TIMEOUT_MS,
                                 actual, false) ||
        !await_profiled_position(MOTOR_PITCH_ID, target_pose->pitch_raw,
                                 MOTOR_PROFILE_MS, MOTOR_MOVE_TIMEOUT_MS,
                                 actual, true)) goto done;
    reached = read_pose(actual) && abs(actual->yaw_raw-target_pose->yaw_raw)<=9 &&
              abs(actual->pitch_raw-target_pose->pitch_raw)<=9;
done:
    /* On any failed path report a fresh observed position if one is available. */
    if (!reached && actual && actual->yaw_raw < 0) (void)read_pose(actual);
    return reached;
}

static bool pose_at(int yaw_deg, int pitch_deg, motor_pose_t *pose)
{
    if (!pose || yaw_deg < MOTOR_YAW_MIN_DEG || yaw_deg > MOTOR_YAW_MAX_DEG ||
        pitch_deg < MOTOR_PITCH_MIN_DEG || pitch_deg > MOTOR_PITCH_MAX_DEG)
        return false;
    pose->yaw_deg = yaw_deg;
    pose->pitch_deg = pitch_deg;
    pose->yaw_raw = raw_for_deg(MOTOR_YAW_ZERO_RAW, yaw_deg, MOTOR_YAW_DIRECTION);
    pose->pitch_raw = raw_for_deg(MOTOR_PITCH_ZERO_RAW, pitch_deg, MOTOR_PITCH_DIRECTION);
    return pose->yaw_raw >= 0 && pose->yaw_raw <= 1000 &&
           pose->pitch_raw >= 0 && pose->pitch_raw <= 1000;
}

static void telemetry(const char *action, const char *result,
                      const motor_pose_t *pose, const char *reason)
{
    TickType_t now = xTaskGetTickCount();
    uint32_t yaw_age = (uint32_t)(now - s_yaw_sample_tick) * portTICK_PERIOD_MS;
    uint32_t pitch_age = (uint32_t)(now - s_pitch_sample_tick) * portTICK_PERIOD_MS;
    wire_printf("LOOK_MOTOR result=%s action=%s stage=%s reason=%s yaw_raw=%d yaw_deg=%d yaw_sample_age_ms=%lu pitch_raw=%d pitch_deg=%d pitch_sample_age_ms=%lu sample=last_valid_per_axis\n",
           result, action, s_stage, reason ? reason : "none",
           pose ? pose->yaw_raw : -1, pose ? pose->yaw_deg : -999,
           (unsigned long)yaw_age,
           pose ? pose->pitch_raw : -1, pose ? pose->pitch_deg : -999,
           (unsigned long)pitch_age);
    fflush(stdout);
}

static bool finish_session(bool ok, const char *action, motor_pose_t *actual,
                           const char *reason)
{
    /* `actual` is the last completed, verified ReadPos sample; never read after rail-off. */
    bool released = torque_release();
    bool rail_off = rail_set(false);
    s_busy = false;
    bool result = ok && released && rail_off && actual &&
                  actual->yaw_raw >= 0 && actual->pitch_raw >= 0;
    telemetry(action, result ? "ok" : "failed", actual,
              !released ? "torque_release_unconfirmed" :
              !rail_off ? "rail_off_unconfirmed" :
              !actual ? "final_readback_unavailable" : reason);
    return result;
}

static bool begin_session(i2c_master_bus_handle_t bus, const char *action,
                          motor_pose_t *start)
{
    if (!calibration_valid()) {
        telemetry(action ? action : "invalid", "failed", NULL, "device_calibration_missing_or_invalid");
        return false;
    }
    if (!bus || !action || !start || s_busy) {
        telemetry(action ? action : "invalid", "failed", NULL, "invalid_or_busy");
        return false;
    }
    s_busy = true;
    if (!add_py32(bus)) {
        s_busy = false;
        telemetry(action, "failed", NULL, "py32_unavailable");
        return false;
    }
    /* Fail closed: only an already verified low rail may be raised explicitly. */
    uint8_t rail = 0xff, dir = 0xff;
    if (!py32_read(MOTOR_VM_EN_REG, &rail) || !py32_read(0x03, &dir) ||
        (dir & 1u) == 0 || (rail & 1u) != 0 || !uart_start() || !rail_set(true)) {
        (void)rail_set(false);
        s_busy = false;
        telemetry(action, "failed", NULL, "rail_or_uart_preflight");
        return false;
    }
    /* SCS0009 wake-up can be delayed; poll read-only position registers boundedly. */
    TickType_t wake_started = xTaskGetTickCount();
    TickType_t wake_budget = pdMS_TO_TICKS(MOTOR_WAKE_BUDGET_MS);
    int yaw_raw = -1, pitch_raw = -1;
    bool start_read = false;
    do {
        if (read_word(MOTOR_YAW_ID, MOTOR_REG_PRESENT_POS, &yaw_raw) &&
            read_word(MOTOR_PITCH_ID, MOTOR_REG_PRESENT_POS, &pitch_raw)) {
            start_read = true;
            break;
        }
        if ((xTaskGetTickCount() - wake_started) < wake_budget)
            vTaskDelay(pdMS_TO_TICKS(200));
    } while ((xTaskGetTickCount() - wake_started) < wake_budget);
    if (!start_read) {
        bool released = torque_release();
        bool rail_off = rail_set(false);
        s_busy = false;
        telemetry(action, "failed", NULL,
                  !released || !rail_off ? "shutdown_unconfirmed" : "start_readback_failed");
        return false;
    }
    bool entry_action = strcmp(action, "survey") == 0 || strcmp(action, "HOME") == 0;
    if (!pose_from_raw(yaw_raw, pitch_raw, start, entry_action)) {
        bool released = torque_release();
        bool off = rail_set(false);
        s_busy = false;
        telemetry(action, "failed", start,
                  !released ? "unsafe_pose_torque_release_failed" :
                  !off ? "unsafe_pose_rail_off_failed" : "unsupported_start_pose");
        return false;
    }
    s_yaw_sample_tick = s_pitch_sample_tick = xTaskGetTickCount();
    int yaw_min, yaw_max, pitch_min, pitch_max;
    if (!servo_position_limits(MOTOR_YAW_ID, &yaw_min, &yaw_max) ||
        !servo_position_limits(MOTOR_PITCH_ID, &pitch_min, &pitch_max) ||
        yaw_raw < yaw_min || yaw_raw > yaw_max ||
        pitch_raw < pitch_min || pitch_raw > pitch_max) {
        bool released = torque_release();
        bool off = rail_set(false);
        s_busy = false;
        telemetry(action, "failed", start,
                  !released ? "unsupported_mode_torque_release_failed" :
                  !off ? "unsupported_mode_rail_off_failed" : "position_mode_unconfirmed");
        return false;
    }
    if (!hold_observed_pose(start)) {
        bool off = rail_set(false);
        s_busy = false;
        telemetry(action, "failed", start, off ? "initial_hold_unconfirmed" : "rail_off_unconfirmed");
        return false;
    }
    /* The sampled raw protocol envelope was checked before degree conversion. */
    if (!torque_release()) {
        (void)rail_set(false);
        s_busy = false;
        telemetry(action, "failed", start, "initial_torque_release_unconfirmed");
        return false;
    }
    return true;
}

/* Optional, bounded low-pitch correction is only reachable from survey/HOME. */
static bool survey_entry(const motor_pose_t *start, motor_pose_t *actual)
{
    if (!start || !actual || !calibration_valid() ||
        start->yaw_deg < MOTOR_YAW_MIN_DEG || start->yaw_deg > MOTOR_YAW_MAX_DEG)
        return false;
    if (start->pitch_deg >= MOTOR_PITCH_MIN_DEG && start->pitch_deg <= MOTOR_PITCH_MAX_DEG)
        return true;
    if (!SIMSTIM_MOTOR_SURVEY_ENTRY_ENABLED ||
        start->pitch_deg < SIMSTIM_MOTOR_SURVEY_ENTRY_MIN_PITCH_DEG ||
        start->pitch_deg >= MOTOR_PITCH_MIN_DEG)
        return false;
    motor_pose_t entry_goal;
    if (!pose_at(start->yaw_deg, MOTOR_PITCH_MIN_DEG, &entry_goal)) return false;
    s_stage = "survey_entry_inward";
    return move_pose(&entry_goal, actual);
}

typedef struct {
    int version;
    int servo_id;
    int min_angle;
    int max_angle;
    int present_raw;
    int torque;
    int moving;
    int load;
    int voltage;
    int temperature;
    bool critical_ok;
} motor_axis_probe_t;

static bool read_probe_axis(uint8_t id, motor_axis_probe_t *axis)
{
    uint8_t b[4];
    int version = -1, servo_id = -1, pos = -1, torque = -1, moving = -1;
    int load = -1, voltage = -1, temperature = -1;
    int min_angle = -1, max_angle = -1;
    bool ok = true;
    if (servo_read(id, MOTOR_REG_VERSION, 2, b)) version = (b[0] << 8) | b[1]; else ok = false;
    if (servo_read(id, MOTOR_REG_ID, 1, b)) servo_id = b[0]; else ok = false;
    if (servo_read(id, MOTOR_REG_MIN_ANGLE, 4, b)) {
        min_angle = (b[0] << 8) | b[1];
        max_angle = (b[2] << 8) | b[3];
    } else ok = false;
    if (servo_read(id, MOTOR_REG_TORQUE, 1, b) && b[0] <= 1) torque = b[0]; else ok = false;
    if (servo_read(id, MOTOR_REG_MOVING, 1, b) && b[0] <= 1) moving = b[0]; else ok = false;
    if (servo_read(id, MOTOR_REG_PRESENT_POS, 2, b)) pos = (b[0] << 8) | b[1]; else ok = false;
    if (servo_read(id, MOTOR_REG_LOAD, 2, b)) load = (b[0] << 8) | b[1];
    if (servo_read(id, MOTOR_REG_VOLTAGE, 1, b)) voltage = b[0];
    if (servo_read(id, MOTOR_REG_TEMPERATURE, 1, b)) temperature = b[0];
    axis->version = version;
    axis->servo_id = servo_id;
    axis->min_angle = min_angle;
    axis->max_angle = max_angle;
    axis->present_raw = pos;
    axis->torque = torque;
    axis->moving = moving;
    axis->load = load;
    axis->voltage = voltage;
    axis->temperature = temperature;
    axis->critical_ok = ok && servo_id == id && pos >= 0 && pos <= 1023;
    return axis->critical_ok;
}

static void print_probe_axis(const char *name, const motor_axis_probe_t *a)
{
    const char *mode = a->min_angle == 0 && a->max_angle == 0 ? "non_position_zero_limits" :
                       a->max_angle > a->min_angle ? "position" : "invalid_limits";
    wire_printf(" %s_id=%d %s_fw=%d %s_mode=%s %s_limits=%d:%d %s_torque=%d %s_moving=%d %s_pos_raw=%d %s_load=%d %s_mv=%d %s_temp_c=%d",
           name, a->servo_id, name, a->version, name, mode, name, a->min_angle,
           a->max_angle, name, a->torque, name, a->moving, name, a->present_raw,
           name, a->load, name, a->voltage, name, a->temperature);
}

/* Bounded read-only servo probe. Only VM_EN is toggled; no servo writes. */
static bool motor_probe(i2c_master_bus_handle_t bus)
{
    uint8_t before_latch = 0xff, before_dir = 0xff;
    uint8_t after_latch = 0xff, after_dir = 0xff;
    motor_axis_probe_t yaw = {-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,false};
    motor_axis_probe_t pitch = {-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,false};
    bool gate_before_ok = false, gate_on_ok = false, gate_off_ok = false;
    bool bus_ok = false, axes_ok = false, rail_may_have_changed = false;
    wire_rx=wire_echo=wire_bad_status=0;
    if (!bus || s_busy || !add_py32(bus)) goto done;
    s_busy = true;
    gate_before_ok = py32_read(MOTOR_VM_EN_REG, &before_latch) &&
                     py32_read(0x03, &before_dir) && (before_dir & 1u);
    /* Do not interrupt a rail that was already enabled, or alter expander mode. */
    if (!gate_before_ok || (before_latch & 1u) || !uart_start()) goto finalize;
    rail_may_have_changed = true;
    gate_on_ok = rail_set(true);
    if (!gate_on_ok) goto cleanup;
    vTaskDelay(pdMS_TO_TICKS(300));
    TickType_t started = xTaskGetTickCount();
    TickType_t budget = pdMS_TO_TICKS(MOTOR_WAKE_BUDGET_MS);
    do {
        bool yaw_ok = read_probe_axis(MOTOR_YAW_ID, &yaw);
        bool pitch_ok = read_probe_axis(MOTOR_PITCH_ID, &pitch);
        axes_ok = yaw_ok && pitch_ok;
        if (axes_ok) break;
        vTaskDelay(pdMS_TO_TICKS(250));
    } while ((xTaskGetTickCount() - started) < budget);
    bus_ok = axes_ok;
cleanup:
    if (rail_may_have_changed) gate_off_ok = rail_set(false);
finalize:
    (void)py32_read(MOTOR_VM_EN_REG, &after_latch);
    (void)py32_read(0x03, &after_dir);
    gate_off_ok = gate_off_ok && (after_dir & 1u) && !(after_latch & 1u);
    s_busy = false;
done:
    wire_printf("LOOK_MOTOR result=%s action=probe reason=%s gate_before_ok=%d gate_before_latch=%d gate_before_dir=%d gate_enable_latch_verified=%d gate_after_latch=%d gate_after_dir=%d vm_en_latch_off_verified=%d rail_voltage=not_sensed model=SCS0009_board_config model_id=not_readable powerup_restore_motion=possible yaw_critical=%d",
           gate_off_ok && bus_ok ? "ok" : "failed",
           !gate_before_ok ? "gate_before_unavailable" :
           (before_latch & 1u) ? "rail_was_already_on" :
           !gate_on_ok ? "rail_enable_unconfirmed" :
           !bus_ok ? "servo_readback_incomplete" : "complete",
           gate_before_ok, before_latch & 1u, before_dir & 1u, gate_on_ok,
           after_latch & 1u, after_dir & 1u, gate_off_ok, yaw.critical_ok);
    print_probe_axis("yaw", &yaw);
    wire_printf(" pitch_critical=%d", pitch.critical_ok);
    print_probe_axis("pitch", &pitch);
    wire_printf(" writes=rail_gate_only rx_bytes=%u echoes=%u status_faults=%u\n",wire_rx,wire_echo,wire_bad_status);
    fflush(stdout);
    return gate_off_ok && bus_ok;
}

/* Fixed base-relative centre, not the last resting pose. */
static bool centre_goal(motor_pose_t *goal){return pose_at(0,30,goal);}
static bool at_goal(const motor_pose_t *p,const motor_pose_t *g){return abs(p->yaw_raw-g->yaw_raw)<=9 && abs(p->pitch_raw-g->pitch_raw)<=9;}
/* Relative glances keep their bounded envelope; HOME is absolute. */
bool motor_look(i2c_master_bus_handle_t bus, const char *direction)
{
    static motor_pose_t scan_home;
    static bool scan_home_valid;
    if (!direction) {
        telemetry("invalid", "failed", NULL, "null_direction");
        return false;
    }
    bool is_home = strcmp(direction, "HOME") == 0;
    bool is_left = strcmp(direction, "LEFT") == 0;
    bool is_right = strcmp(direction, "RIGHT") == 0;
    bool is_up = strcmp(direction, "UP") == 0;
    bool is_down = strcmp(direction, "DOWN") == 0;
    if (strcmp(direction, "PROBE") == 0) {
        if (!calibration_valid()) { telemetry(direction, "failed", NULL, "device_calibration_missing_or_invalid"); return false; }
        return motor_probe(bus);
    }
    if (!is_home && !is_left && !is_right && !is_up && !is_down) {
        telemetry(direction, "failed", NULL, "unsupported_direction");
        return false;
    }
    motor_pose_t start, goal, actual = {-1, -1, -999, -999};
    if (!begin_session(bus, direction, &start)) return false;
    if (!scan_home_valid) { scan_home = start; scan_home_valid = true; }
    if (is_home) {
        actual=start; s_stage="home_entry";
        if(!survey_entry(&start,&actual) || !centre_goal(&goal)) return finish_session(false,direction,&actual,"home_entry_failed");
        if(at_goal(&actual,&goal)){scan_home_valid=false;return finish_session(true,direction,&actual,"already_centred");}
        s_stage="home_centre";
    }
    else {
        int yaw = start.yaw_deg + (is_left ? -5 : is_right ? 5 : 0);
        int pitch = start.pitch_deg + (is_up ? 5 : is_down ? -5 : 0);
        int min_yaw = scan_home.yaw_deg - MOTOR_PAN_HALF_SPAN_DEG;
        int max_yaw = scan_home.yaw_deg + MOTOR_PAN_HALF_SPAN_DEG;
        int min_pitch = scan_home.pitch_deg - MOTOR_TILT_HALF_SPAN_DEG;
        int max_pitch = scan_home.pitch_deg + MOTOR_TILT_HALF_SPAN_DEG;
        if (yaw < min_yaw || yaw > max_yaw || pitch < min_pitch || pitch > max_pitch ||
            !pose_at(yaw, pitch, &goal))
            return finish_session(false, direction, &actual, "relative_envelope_refused");
    }
    bool ok = move_pose(&goal, &actual);
    bool result = finish_session(ok, direction, &actual, ok ? "complete" : "move_or_readback_failed");
    if (is_home && result) scan_home_valid = false;
    return result;
}

/*
 * Device-owned short survey. Callback runs only at settled views and is
 * strictly notification-only: it must return promptly and perform no waits.
 */
bool motor_survey(i2c_master_bus_handle_t bus,
                  void (*glance)(unsigned, void *), void *context)
{
    static const char *const action = "survey";
    motor_pose_t start, goal, actual = {-1, -1, -999, -999};
    s_stage = "survey_begin";
    if (!begin_session(bus, action, &start)) return false;
    actual = start;
    s_stage = "survey_entry";
    if (!survey_entry(&start, &actual))
        return finish_session(false, action, &actual, "survey_entry_failed");
    if (!centre_goal(&start)) return finish_session(false,action,&actual,"invalid_home");
    s_stage="survey_centre";
    if (!at_goal(&actual,&start) && !move_pose(&start,&actual)) return finish_session(false,action,&actual,"centre_not_reached");
    uint32_t elapsed_start = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
    const int pan = MOTOR_PAN_HALF_SPAN_DEG;
    const int tilt = MOTOR_TILT_HALF_SPAN_DEG;
    const int views[MOTOR_VIEWS][2] = {
        {start.yaw_deg - pan, start.pitch_deg > MOTOR_PITCH_MAX_DEG-tilt ? start.pitch_deg : start.pitch_deg+tilt},
        {start.yaw_deg + pan, start.pitch_deg < MOTOR_PITCH_MIN_DEG+tilt ? start.pitch_deg : start.pitch_deg-tilt},
    };
    bool ok = true;
    int min_yaw = start.yaw_deg, max_yaw = start.yaw_deg;
    int min_pitch = start.pitch_deg, max_pitch = start.pitch_deg;
    unsigned completed = 0;
    /* Refuse the entire trajectory before motion if any pose is unsupported. */
    for (unsigned i = 0; i < MOTOR_VIEWS; ++i) {
        if (views[i][0] < start.yaw_deg - pan || views[i][0] > start.yaw_deg + pan ||
            views[i][1] < start.pitch_deg - tilt || views[i][1] > start.pitch_deg + tilt ||
            !pose_at(views[i][0], views[i][1], &goal)) {
            return finish_session(false, action, &actual, "unsupported_survey_pose");
        }
    }
    if (glance) glance(0, context);
    for (unsigned i = 0; i < MOTOR_VIEWS; ++i) {
        s_stage = i == 0 ? "survey_waypoint_1" : "survey_waypoint_2";
        if (!pose_at(views[i][0], views[i][1], &goal) || !move_pose(&goal, &actual)) {
            ok = false;
            break;
        }
        completed++;
        if (actual.yaw_deg < min_yaw) min_yaw = actual.yaw_deg;
        if (actual.yaw_deg > max_yaw) max_yaw = actual.yaw_deg;
        if (actual.pitch_deg < min_pitch) min_pitch = actual.pitch_deg;
        if (actual.pitch_deg > max_pitch) max_pitch = actual.pitch_deg;
        if (glance) glance(completed, context);
    }
    /* Attempt source-proven return only when the last readback remained safe. */
    if (ok && completed == MOTOR_VIEWS) {
        s_stage = "survey_return";
        if (!move_pose(&start, &actual)) ok = false;
    }
    uint32_t elapsed = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS) - elapsed_start;
    bool result = finish_session(ok && completed == MOTOR_VIEWS, action, &actual,
                                  ok ? "complete" : "survey_stopped_on_first_error");
    wire_printf("LOOK_MOTOR result=%s action=survey views=%u yaw_span_deg=%d pitch_span_deg=%d elapsed_ms=%lu profile_ms=%u\n",
           result ? "ok" : "failed", completed, max_yaw - min_yaw,
           max_pitch - min_pitch, (unsigned long)elapsed, MOTOR_PROFILE_MS);
    fflush(stdout);
    return result;
}
