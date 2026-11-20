#ifndef FAKE_ESP_H
#define FAKE_ESP_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

typedef int esp_err_t;
#define ESP_OK 0
#define UART_NUM_MAX 3
typedef int uart_port_t;
#define UART_NUM_1 1
#define UART_DATA_8_BITS 8
#define UART_PARITY_DISABLE 0
#define UART_STOP_BITS_1 1
#define UART_HW_FLOWCTRL_DISABLE 0
#define UART_SCLK_DEFAULT 0
#define UART_PIN_NO_CHANGE -1
typedef int gpio_num_t;
#define GPIO_NUM_6 6
#define GPIO_NUM_7 7
typedef struct { int baud_rate, data_bits, parity, stop_bits, flow_ctrl, source_clk; } uart_config_t;
typedef void *i2c_master_bus_handle_t;
typedef void *i2c_master_dev_handle_t;
#define I2C_ADDR_BIT_LEN_7 7
typedef struct { int dev_addr_length; uint16_t device_address; uint32_t scl_speed_hz; } i2c_device_config_t;
typedef uint32_t TickType_t;
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define portTICK_PERIOD_MS 1

esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t, const i2c_device_config_t *, i2c_master_dev_handle_t *);
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t, const uint8_t *, size_t, uint8_t *, size_t, int);
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t, const uint8_t *, size_t, int);
esp_err_t uart_param_config(uart_port_t, const uart_config_t *);
esp_err_t uart_set_pin(uart_port_t, int, int, int, int);
bool uart_is_driver_installed(uart_port_t);
esp_err_t uart_driver_install(uart_port_t, int, int, int, void *, int);
void uart_flush_input(uart_port_t);
int uart_write_bytes(uart_port_t, const char *, size_t);
esp_err_t uart_wait_tx_done(uart_port_t, TickType_t);
int uart_read_bytes(uart_port_t, uint8_t *, uint32_t, TickType_t);
TickType_t xTaskGetTickCount(void);
void vTaskDelay(TickType_t);
#endif
