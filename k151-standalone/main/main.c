#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "esp_app_desc.h"
#include "wifi_link.h"
#include "wire_io.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* Implemented by the parallel K151 board worker in board.c. */
bool board_init(void);
void board_tick(uint32_t uptime_s);

void app_main(void)
{
    wire_io_init();
    wifi_link_start();
    const bool ready = board_init();
    /* The IDF panic-format string may be abbreviated. Encode the complete
       descriptor digest: the observer matches the linked ELF byte for byte. */
    static const char hex[] = "0123456789abcdef";
    const esp_app_desc_t *desc = esp_app_get_description();
    char elf_sha256[65];
    for (unsigned i = 0; i < 32; ++i) {
        elf_sha256[2*i] = hex[desc->app_elf_sha256[i] >> 4];
        elf_sha256[2*i+1] = hex[desc->app_elf_sha256[i] & 15u];
    }
    elf_sha256[64] = '\0';
    TickType_t wake_tick = xTaskGetTickCount();
    uint32_t uptime_s = 0;

    for (;;) {
        vTaskDelayUntil(&wake_tick, pdMS_TO_TICKS(1000));
        ++uptime_s;

        if (ready) {
            board_tick(uptime_s);
        }

        /* The IDF app descriptor carries the real linked ELF SHA-256. */
        wire_broadcast_printf("SIMSTIM_STANDALONE sha256=%s uptime_s=%u board_ready=%u\n",
               elf_sha256, (unsigned)uptime_s, ready ? 1U : 0U);
        fflush(stdout);
    }
}
