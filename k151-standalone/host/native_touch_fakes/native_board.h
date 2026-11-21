#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
typedef void *i2c_master_bus_handle_t; typedef void *i2c_master_dev_handle_t; typedef void *i2s_chan_handle_t;
typedef unsigned TickType_t;
typedef struct { int device_address; int scl_speed_hz; int dev_addr_length; } i2c_device_config_t;
#define I2C_ADDR_BIT_LEN_7 7
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t,const i2c_device_config_t*,i2c_master_dev_handle_t*);
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t,const uint8_t*,size_t,int);
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t,const uint8_t*,size_t,uint8_t*,size_t,int);
typedef struct { int auto_clear; } i2s_chan_config_t; typedef struct { int clk_cfg; int slot_cfg; struct {int mclk,bclk,ws,dout,din;} gpio_cfg; } i2s_std_config_t;
#define I2S_NUM_0 0
#define I2S_ROLE_MASTER 0
#define I2S_CHANNEL_DEFAULT_CONFIG(a,b) ((i2s_chan_config_t){0})
#define I2S_STD_CLK_DEFAULT_CONFIG(a) 0
#define I2S_DATA_BIT_WIDTH_16BIT 16
#define I2S_SLOT_MODE_STEREO 2
#define I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(a,b) 0
#define I2S_GPIO_UNUSED -1
#define pdPASS 1
#define CONFIG_BT_NIMBLE_ENABLED 0
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
void *heap_caps_malloc(size_t,int);
uint32_t esp_rom_crc32_le(uint32_t,const uint8_t*,size_t);
uint32_t esp_random(void);
int usb_serial_jtag_ll_read_rxfifo(uint8_t*,size_t);
esp_err_t i2s_new_channel(const i2s_chan_config_t*,i2s_chan_handle_t*,i2s_chan_handle_t*);
esp_err_t i2s_channel_init_std_mode(i2s_chan_handle_t,const i2s_std_config_t*);
esp_err_t i2s_channel_enable(i2s_chan_handle_t);
esp_err_t i2s_channel_disable(i2s_chan_handle_t);
esp_err_t i2s_channel_write(i2s_chan_handle_t,const void*,size_t,size_t*,uint32_t);
int xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,void*);
TickType_t xTaskGetTickCount(void);
void vTaskDelay(TickType_t);
#define portTICK_PERIOD_MS 1
#define pdMS_TO_TICKS(x) (x)
#define taskYIELD() ((void)0)
