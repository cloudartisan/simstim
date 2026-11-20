#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

/* M5Stack CoreS3 / K151 board connections, verified against the local board source. */
#define I2C_SDA GPIO_NUM_12
#define I2C_SCL GPIO_NUM_11
#define I2C_PORT I2C_NUM_1
#define AXP2101_ADDR 0x34
#define AW9523_ADDR 0x58
#define PY32_ADDR 0x6f
#define LTR553_ADDR 0x23
#define SPI_HOST SPI3_HOST
#define LCD_MOSI GPIO_NUM_37
#define LCD_SCLK GPIO_NUM_36
#define LCD_CS GPIO_NUM_3
#define LCD_DC GPIO_NUM_35
#define LCD_WIDTH 320
#define LCD_HEIGHT 240
#define LCD_PCLK_HZ (40 * 1000 * 1000)
#define PY32_VM_EN_REG 0x05
#define LTR_COUNTS_REG 0x8d
#define TAG "simstim_board"

static i2c_master_bus_handle_t s_i2c;
static i2c_master_dev_handle_t s_py32;
static i2c_master_dev_handle_t s_ltr;
static i2c_master_dev_handle_t s_touch;
static spi_device_handle_t s_lcd;
static bool s_ltr_ready;
static SemaphoreHandle_t screen_lock;
static uint16_t s_frame[LCD_WIDTH * LCD_HEIGHT];

static esp_err_t add_i2c_device(uint8_t address, uint32_t speed,
                                i2c_master_dev_handle_t *out)
{
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        .scl_speed_hz = speed,
    };
    return i2c_master_bus_add_device(s_i2c, &cfg, out);
}

static esp_err_t reg_read(i2c_master_dev_handle_t dev, uint8_t reg,
                          uint8_t *data, size_t len, int timeout_ms)
{
    return i2c_master_transmit_receive(dev, &reg, 1, data, len, timeout_ms);
}

static esp_err_t reg_write(i2c_master_dev_handle_t dev, uint8_t reg,
                           uint8_t value)
{
    const uint8_t tx[2] = {reg, value};
    return i2c_master_transmit(dev, tx, sizeof(tx), 100);
}

static bool py32_update_bit(uint8_t reg, uint8_t bit, bool set)
{
    uint8_t value = 0;
    if (!s_py32 || reg_read(s_py32, reg, &value, 1, 100) != ESP_OK) return false;
    value = set ? (uint8_t)(value | (1u << bit)) : (uint8_t)(value & ~(1u << bit));
    return reg_write(s_py32, reg, value) == ESP_OK;
}

/* Establish the host bus only; no PMIC, expander, or panel writes here. */
static esp_err_t init_i2c_bus(void)
{
    const i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_PORT,
        .sda_io_num = I2C_SDA,
        .scl_io_num = I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = 1,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &s_i2c), TAG, "I2C bus init");

    return ESP_OK;
}

static esp_err_t init_power_and_io(void)
{

    i2c_master_dev_handle_t axp = NULL;
    ESP_RETURN_ON_ERROR(add_i2c_device(AXP2101_ADDR, 400000, &axp), TAG, "AXP attach");
    /* Preserve the established AXP2101 boot power sequence and exact register values. */
    uint8_t value = 0;
    ESP_RETURN_ON_ERROR(reg_read(axp, 0x90, &value, 1, 100), TAG, "AXP read 90");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x90, value | 0xb4), TAG, "AXP write 90a");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x99, 0x1e - 5), TAG, "AXP write 99");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x97, 0x1e - 2), TAG, "AXP write 97");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x69, 0x35), TAG, "AXP write 69");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x30, 0x3f), TAG, "AXP write 30");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x90, 0xbf), TAG, "AXP write 90b");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x94, 28), TAG, "AXP write 94");
    ESP_RETURN_ON_ERROR(reg_write(axp, 0x95, 28), TAG, "AXP write 95");
    /* Charging is left in the proven default-enabled state; no NVS is used. */

    i2c_master_dev_handle_t aw = NULL;
    ESP_RETURN_ON_ERROR(add_i2c_device(AW9523_ADDR, 400000, &aw), TAG, "AW9523 attach");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x02, 0x07), TAG, "AW P0 init");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x03, 0x8f), TAG, "AW P1 init");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x04, 0x18), TAG, "AW P0 config");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x05, 0x0c), TAG, "AW P1 config");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x11, 0x10), TAG, "AW GCR init");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x12, 0xff), TAG, "AW LED P0 init");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x13, 0xff), TAG, "AW LED P1 init");
    vTaskDelay(pdMS_TO_TICKS(50));

    if (add_i2c_device(LTR553_ADDR, 100000, &s_ltr) == ESP_OK) {
        uint8_t part = 0, manufacturer = 0;
        vTaskDelay(pdMS_TO_TICKS(100));
        if (reg_read(s_ltr, 0x86, &part, 1, 50) == ESP_OK &&
            reg_read(s_ltr, 0x87, &manufacturer, 1, 50) == ESP_OK &&
            part == 0x92 && manufacturer == 0x05 &&
            reg_write(s_ltr, 0x84, 0x02) == ESP_OK &&
            /* Lite-On: 60 kHz, 100% pulse duty, 100 mA peak. */
            reg_write(s_ltr, 0x82, 0x7c) == ESP_OK &&
            reg_write(s_ltr, 0x83, 0x04) == ESP_OK &&
            reg_write(s_ltr, 0x81, 0x23) == ESP_OK) {
            uint8_t cfg[4] = {0};
            s_ltr_ready = reg_read(s_ltr, 0x81, cfg, 4, 100) == ESP_OK &&
                          cfg[0] == 0x23 && cfg[1] == 0x7c && cfg[2] == 4 && cfg[3] == 2;
            ESP_LOGI(TAG, "LTR config readback: %02x %02x %02x %02x", cfg[0],cfg[1],cfg[2],cfg[3]);
            ESP_LOGI(TAG, "LTR-553 proximity initialized (IDs verified)");
        } else {
            ESP_LOGW(TAG, "LTR-553 unavailable or ID/config check failed");
            i2c_master_bus_rm_device(s_ltr);
            s_ltr = NULL;
        }
    }
    return ESP_OK;
}

static bool verify_py32_vm_en_low(void)
{
    uint8_t output_low = 0xff, direction_low = 0xff;
    if (!s_py32 || reg_read(s_py32, PY32_VM_EN_REG, &output_low, 1, 100) != ESP_OK ||
        reg_read(s_py32, 0x03, &direction_low, 1, 100) != ESP_OK ||
        (output_low & 1u) != 0 || (direction_low & 1u) == 0) {
        ESP_LOGE(TAG, "PY32 VM EN low readback not confirmed; refusing board-ready state");
        return false;
    }
    return true;
}

static bool init_py32_servo_gate(void)
{
    if (add_i2c_device(PY32_ADDR, 100000, &s_py32) != ESP_OK) {
        ESP_LOGE(TAG, "PY32 unavailable: refusing board-ready state; servo remains unpowered");
        return false;
    }
    /* Drive the safety gate before version polling or any other peripheral writes. */
    if (!py32_update_bit(0x05, 0, false) ||  /* latch low */
        !py32_update_bit(0x09, 0, false) ||  /* disable pull-up */
        !py32_update_bit(0x0b, 0, true)  ||  /* pull-down */
        !py32_update_bit(0x03, 0, true)) {   /* output */
        ESP_LOGE(TAG, "PY32 VM EN low/output setup failed: refusing board-ready state");
        return false;
    }
    if (!verify_py32_vm_en_low()) return false;

    uint8_t version = 0;
    bool found = false;
    for (unsigned retry = 0; retry < 5; ++retry) {
        if (reg_read(s_py32, 0x02, &version, 1, 100) == ESP_OK &&
            version != 0x00 && version != 0xff) {
            found = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (!found) {
        ESP_LOGE(TAG, "PY32 version unavailable after VM EN low; refusing board-ready state");
        return false;
    }
    ESP_LOGI(TAG, "PY32 v%02x VM EN low verified (output latch and direction)", version);
    return true;
}

static esp_err_t spi_tx(const void *data, size_t len, bool data_mode)
{
    ESP_RETURN_ON_ERROR(gpio_set_level(LCD_DC, data_mode ? 1 : 0), TAG, "LCD DC");
    spi_transaction_t t = {0};
    t.length = len * 8;
    t.tx_buffer = data;
    return spi_device_transmit(s_lcd, &t);
}

static esp_err_t lcd_cmd(uint8_t command, const uint8_t *params, size_t n)
{
    ESP_RETURN_ON_ERROR(spi_tx(&command, 1, false), TAG, "LCD command");
    if (n) ESP_RETURN_ON_ERROR(spi_tx(params, n, true), TAG, "LCD parameters");
    return ESP_OK;
}

static esp_err_t apply_ili9342e_init(void)
{
    /* Panel sequence from the local K151 StackChan ILI9342E init table. */
    static const struct { uint8_t cmd, len, p[15]; uint16_t delay_ms; } init[] = {
        {0xdd,1,{0x01},0}, {0x3a,1,{0x55},0}, {0x21,0,{0},0},
        {0x36,1,{0x08},0}, {0xd5,1,{0x00},0}, {0xb1,1,{0x22},0},
        {0xc8,1,{0x38},0}, {0xcb,1,{0x1c},0}, {0xc9,1,{0x1a},0},
        {0xca,1,{0x1a},0}, {0xb7,4,{0x5a,0x41,0x11,0x19},0},
        {0xe4,15,{0x04,0x08,0x11,0x06,0x12,0x07,0x3a,0x76,0x47,0x07,0x0f,0x0a,0x11,0x19,0x05},0},
        {0xe5,15,{0x02,0x03,0x07,0x06,0x12,0x07,0x36,0x5f,0x48,0x06,0x10,0x0c,0x16,0x14,0x09},0},
        {0x11,0,{0},120}, {0x29,0,{0},120},
    };
    for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); ++i) {
        ESP_RETURN_ON_ERROR(lcd_cmd(init[i].cmd, init[i].p, init[i].len), TAG, "ILI9342E init");
        if (init[i].delay_ms) vTaskDelay(pdMS_TO_TICKS(init[i].delay_ms));
    }
    return ESP_OK;
}

static esp_err_t init_lcd(void)
{
    const spi_bus_config_t bus = {
        .mosi_io_num = LCD_MOSI,
        .miso_io_num = GPIO_NUM_NC,
        .sclk_io_num = LCD_SCLK,
        .quadwp_io_num = GPIO_NUM_NC,
        .quadhd_io_num = GPIO_NUM_NC,
        .max_transfer_sz = LCD_WIDTH * LCD_HEIGHT * sizeof(uint16_t),
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "SPI bus");
    ESP_RETURN_ON_ERROR(gpio_set_direction(LCD_DC, GPIO_MODE_OUTPUT), TAG, "LCD DC output");
    const gpio_config_t cs_cfg = {
        .pin_bit_mask = 1ULL << LCD_CS,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&cs_cfg), TAG, "LCD CS output");
    ESP_RETURN_ON_ERROR(gpio_set_level(LCD_CS, 1), TAG, "LCD CS idle");
    const spi_device_interface_config_t dev = {
        .clock_speed_hz = LCD_PCLK_HZ,
        .mode = 2,
        .spics_io_num = LCD_CS,
        .queue_size = 1,
        .flags = SPI_DEVICE_HALFDUPLEX,
    };
    ESP_RETURN_ON_ERROR(spi_bus_add_device(SPI_HOST, &dev, &s_lcd), TAG, "LCD device");

    /* AW9523 controls the panel reset line; use the known K151 pulse sequence. */
    i2c_master_dev_handle_t aw = NULL;
    ESP_RETURN_ON_ERROR(add_i2c_device(AW9523_ADDR, 400000, &aw), TAG, "AW9523 panel reset attach");
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x03, 0x81), TAG, "LCD reset low");
    vTaskDelay(pdMS_TO_TICKS(20));
    ESP_RETURN_ON_ERROR(reg_write(aw, 0x03, 0x83), TAG, "LCD reset high");
    vTaskDelay(pdMS_TO_TICKS(10));

    vTaskDelay(pdMS_TO_TICKS(10));
    ESP_RETURN_ON_ERROR(lcd_cmd(0x01, NULL, 0), TAG, "LCD software reset");
    vTaskDelay(pdMS_TO_TICKS(20));
    ESP_RETURN_ON_ERROR(lcd_cmd(0x11, NULL, 0), TAG, "LCD sleep out");
    vTaskDelay(pdMS_TO_TICKS(100));
    const uint8_t madctl = 0x08; /* BGR, 320x240 unmirrored/unrotated */
    const uint8_t colmod = 0x55; /* RGB565 */
    ESP_RETURN_ON_ERROR(lcd_cmd(0x36, &madctl, 1), TAG, "LCD MADCTL");
    ESP_RETURN_ON_ERROR(lcd_cmd(0x3a, &colmod, 1), TAG, "LCD COLMOD");
    /* Proven ILI9341 baseline vendor sequence from the local IDF app component. */
    static const struct { uint8_t cmd, len, p[15]; } init[] = {
        {0xcf,3,{0x00,0xaa,0xe0}}, {0xed,4,{0x67,0x03,0x12,0x81}},
        {0xe8,3,{0x8a,0x01,0x78}}, {0xcb,5,{0x39,0x2c,0x00,0x34,0x02}},
        {0xf7,1,{0x20}}, {0xea,2,{0x00,0x00}}, {0xc0,1,{0x23}},
        {0xc1,1,{0x11}}, {0xc5,2,{0x43,0x4c}}, {0xc7,1,{0xa0}},
        {0xb1,2,{0x00,0x1b}}, {0xf2,1,{0x00}}, {0x26,1,{0x01}},
        {0xe0,15,{0x1f,0x36,0x36,0x3a,0x0c,0x05,0x4f,0x87,0x3c,0x08,0x11,0x35,0x19,0x13,0x00}},
        {0xe1,15,{0x00,0x09,0x09,0x05,0x13,0x0a,0x30,0x78,0x43,0x07,0x0e,0x0a,0x26,0x2c,0x1f}},
        {0xb7,1,{0x07}}, {0xb6,3,{0x08,0x82,0x27}},
    };
    for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); ++i)
        ESP_RETURN_ON_ERROR(lcd_cmd(init[i].cmd, init[i].p, init[i].len), TAG, "LCD init table");
    /* Read FT6336 ID only to select the known panel revision; no touch data/gestures. */
    i2c_master_dev_handle_t ft6336 = NULL;
    if (add_i2c_device(0x38, 400000, &ft6336) == ESP_OK) {
        const uint8_t work_mode[2] = {0x00, 0x00};
        const uint8_t id_reg = 0xa3;
        uint8_t id[6] = {0};
        if (i2c_master_transmit(ft6336, work_mode, sizeof(work_mode), 100) == ESP_OK &&
            reg_read(ft6336, id_reg, id, sizeof(id), 100) == ESP_OK &&
            id[0] != 0 && id[0] != 0xff && id[5] != 0 && id[5] != 0xff) {
            ESP_LOGI(TAG, "FT6336 panel ID firm=0x%02x vendor=0x%02x", id[3], id[5]);
            if (id[3] == 0x12 && id[5] == 0x11)
                ESP_RETURN_ON_ERROR(apply_ili9342e_init(), TAG, "ILI9342E panel sequence");
        } else {
            ESP_LOGW(TAG, "FT6336 panel ID unavailable; using baseline ILI9341 init");
        }
        s_touch = ft6336;
    }
    ESP_RETURN_ON_ERROR(lcd_cmd(0x21, NULL, 0), TAG, "LCD invert on");
    ESP_RETURN_ON_ERROR(lcd_cmd(0x29, NULL, 0), TAG, "LCD display on");
    return ESP_OK;
}

static esp_err_t lcd_flush(void)
{
    const uint8_t col[4] = {0, 0, (LCD_WIDTH - 1) >> 8, (LCD_WIDTH - 1) & 0xff};
    const uint8_t row[4] = {0, 0, (LCD_HEIGHT - 1) >> 8, (LCD_HEIGHT - 1) & 0xff};
    ESP_RETURN_ON_ERROR(lcd_cmd(0x2a, col, sizeof(col)), TAG, "LCD column window");
    ESP_RETURN_ON_ERROR(lcd_cmd(0x2b, row, sizeof(row)), TAG, "LCD row window");
    ESP_RETURN_ON_ERROR(lcd_cmd(0x2c, NULL, 0), TAG, "LCD RAM write");
    ESP_RETURN_ON_ERROR(gpio_set_level(LCD_DC, 1), TAG, "LCD data mode");
    /* Keep each RAMWR data transaction within the SPI hardware limit. */
    const size_t stripe = LCD_WIDTH * 4;
    for (size_t i = 0; i < LCD_WIDTH * LCD_HEIGHT; i += stripe) {
        spi_transaction_t t = {0};
        t.length = stripe * sizeof(uint16_t) * 8;
        t.tx_buffer = s_frame + i;
        ESP_RETURN_ON_ERROR(spi_device_transmit(s_lcd, &t), TAG, "LCD pixel stripe");
    }
    return ESP_OK;
}

/* Compact 5x7 ASCII subset; adequate for the fixed status screen. */
static const uint8_t *glyph(char c)
{
    static const uint8_t blank[5] = {0,0,0,0,0};
    static const uint8_t digits[10][5] = {
        {0x3e,0x51,0x49,0x45,0x3e},{0,0x42,0x7f,0x40,0},
        {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4b,0x31},
        {0x18,0x14,0x12,0x7f,0x10},{0x27,0x45,0x45,0x45,0x39},
        {0x3c,0x4a,0x49,0x49,0x30},{1,0x71,9,5,3},
        {0x36,0x49,0x49,0x49,0x36},{6,0x49,0x49,0x29,0x1e}
    };
    static const uint8_t letters[26][5] = {
        {0x7e,0x11,0x11,0x11,0x7e},{0x7f,0x49,0x49,0x49,0x36},
        {0x3e,0x41,0x41,0x41,0x22},{0x7f,0x41,0x41,0x22,0x1c},
        {0x7f,0x49,0x49,0x49,0x41},{0x7f,9,9,9,1},{0x3e,0x41,0x49,0x49,0x7a},
        {0x7f,8,8,8,0x7f},{0,0x41,0x7f,0x41,0},{0x20,0x40,0x41,0x3f,1},
        {0x7f,8,0x14,0x22,0x41},{0x7f,0x40,0x40,0x40,0x40},
        {0x7f,2,0x0c,2,0x7f},{0x7f,4,8,0x10,0x7f},{0x3e,0x41,0x41,0x41,0x3e},
        {0x7f,9,9,9,6},{0x3e,0x41,0x51,0x21,0x5e},{0x7f,9,0x19,0x29,0x46},
        {0x46,0x49,0x49,0x49,0x31},{1,1,0x7f,1,1},{0x3f,0x40,0x40,0x40,0x3f},
        {0x1f,0x20,0x40,0x20,0x1f},{0x3f,0x40,0x38,0x40,0x3f},
        {0x63,0x14,8,0x14,0x63},{7,8,0x70,8,7},{0x61,0x51,0x49,0x45,0x43}
    };
    if (c >= '0' && c <= '9') return digits[c-'0'];
    if (c >= 'A' && c <= 'Z') return letters[c-'A'];
    if (c == ':') { static const uint8_t colon[5]={0,0x36,0x36,0,0}; return colon; }
    if (c == '-') { static const uint8_t dash[5]={8,8,8,8,8}; return dash; }
    return blank;
}

static void draw_text(int x, int y, const char *text, unsigned scale, uint16_t color)
{
    for (; *text; ++text, x += (int)(6 * scale)) {
        const uint8_t *g = glyph(*text);
        for (unsigned col = 0; col < 5; ++col) {
            for (unsigned row = 0; row < 7; ++row) {
                if ((g[col] >> row) & 1u) {
                    for (unsigned dy = 0; dy < scale; ++dy)
                        for (unsigned dx = 0; dx < scale; ++dx) {
                            const int px = x + (int)(col * scale + dx);
                            const int py = y + (int)(row * scale + dy);
                            if (px >= 0 && px < LCD_WIDTH && py >= 0 && py < LCD_HEIGHT) {
                                /* INVON is required by the K151 panel; send inverted RGB565, MSB first. */
                                const uint16_t inverted = (uint16_t)~color;
                                s_frame[py * LCD_WIDTH + px] = (uint16_t)((inverted << 8) | (inverted >> 8));
                            }
                        }
                }
            }
        }
    }
}

static bool poll_ltr(uint16_t *counts)
{
    if (!s_ltr_ready || !counts) return false;
    uint8_t status = 0, raw[2] = {0,0};
    if (reg_read(s_ltr, 0x8c, &status, 1, 50) != ESP_OK || !(status & 1u) ||
        reg_read(s_ltr, LTR_COUNTS_REG, raw, sizeof(raw), 50) != ESP_OK)
        return false;
    if (raw[1] & 0x80u) return false; /* saturation flag, not count data */
    *counts = (uint16_t)raw[0] | ((uint16_t)(raw[1] & 0x07u) << 8);
    return true;
}

bool board_init(void)
{
    screen_lock = xSemaphoreCreateMutex();
    if (!screen_lock) return false;
    /* PY32 VM EN is the first peripheral touched after the bus comes up. */
    if (init_i2c_bus() != ESP_OK) return false;
    if (!init_py32_servo_gate()) return false;
    if (init_power_and_io() != ESP_OK) return false;
    if (init_lcd() != ESP_OK) return false;
    /* Catch unexpected changes during PMIC/expander/panel bring-up before ready. */
    if (!verify_py32_vm_en_low()) return false;
    for (size_t i = 0; i < LCD_WIDTH * LCD_HEIGHT; ++i) s_frame[i] = 0xffff;
    draw_text(44, 45, "SIMSTIM", 6, 0xffff);
    draw_text(42, 112, "UPTIME 0", 3, 0x07ff);
    draw_text(42, 158, "PROX --", 2, 0xffe0);
    draw_text(42, 190, "TOUCH --", 2, 0xffff);
    if(lcd_flush() != ESP_OK) return false;
    extern bool speaker_init(i2c_master_bus_handle_t);
    return speaker_init(s_i2c);
}

void board_tick(uint32_t uptime_s)
{
    if (!xSemaphoreTake(screen_lock,0)) return;
    static uint32_t last_log_s;
    uint16_t counts = 0;
    const bool count_valid = poll_ltr(&counts);
    for (size_t i = 0; i < LCD_WIDTH * LCD_HEIGHT; ++i) s_frame[i] = 0xffff;
    draw_text(44, 45, "SIMSTIM", 6, 0xffff);
    char line[32];
    snprintf(line, sizeof(line), "UPTIME %lu", (unsigned long)uptime_s);
    draw_text(42, 112, line, 3, 0x07ff);
    static uint16_t peak;
    if (count_valid && counts > peak) peak = counts;
    if (count_valid) snprintf(line, sizeof(line), "PROX %u MAX %u", (unsigned)counts, (unsigned)peak);
    else snprintf(line, sizeof(line), "LTR COUNT --");
    draw_text(42, 158, line, 2, 0xffe0);
    uint8_t touches=0; bool touch_valid=s_touch && reg_read(s_touch,0x02,&touches,1,50)==ESP_OK && touches<=1;
    draw_text(42, 190, touch_valid ? (touches ? "TOUCH 1" : "TOUCH 0") : "TOUCH --", 2, 0xffff);
    draw_text(42, 218, "TAP UPDATE", 2, 0xffff);
    if (lcd_flush() != ESP_OK) ESP_LOGE(TAG, "LCD refresh failed");
    if (uptime_s != last_log_s) {
        last_log_s = uptime_s;
        if (count_valid) ESP_LOGI(TAG, "uptime_s=%lu ltr553_counts=%u", (unsigned long)uptime_s, (unsigned)counts);
        else ESP_LOGI(TAG, "uptime_s=%lu ltr553_counts=unavailable", (unsigned long)uptime_s);
    }
    xSemaphoreGive(screen_lock);
}

/* Serial command task holds the display until the one-shot activity ends. */
bool board_look_cue(bool camera) {
    if (!xSemaphoreTake(screen_lock,pdMS_TO_TICKS(500))) return false;
    memset(s_frame,0xff,sizeof(s_frame));
    draw_text(30,80,camera ? "CAMERA ON" : "MOVING",4,0xffe0);
    if (lcd_flush()==ESP_OK) return true;
    xSemaphoreGive(screen_lock);
    return false;
}
void board_look_end(void) { xSemaphoreGive(screen_lock); }

/* Query paths share the same verified live peripheral state as the display. */
bool board_proximity_status(uint16_t *counts, bool *saturated)
{
    if (saturated) *saturated = false;
    return poll_ltr(counts);
}
