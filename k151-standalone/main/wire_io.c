#include "wire_io.h"
#include "wifi_link.h"
#include "hal/usb_serial_jtag_ll.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <string.h>
#include <unistd.h>

static wire_owner_t s_owner = WIRE_USB;
static uint32_t s_session;
static bool s_active;
static SemaphoreHandle_t s_wire_lock;

void wire_io_init(void) { s_owner = WIRE_USB; s_session = 0; s_active = false; s_wire_lock = xSemaphoreCreateMutex(); }

bool wire_transaction_begin(wire_owner_t owner, uint32_t session)
{
    if (owner == WIRE_WIFI && session == 0) return false;
    if (!s_wire_lock || xSemaphoreTake(s_wire_lock, portMAX_DELAY) != pdTRUE) return false;
    if (s_active) { xSemaphoreGive(s_wire_lock); return false; }
    s_owner = owner;
    s_session = owner == WIRE_WIFI ? session : 0;
    s_active = true;
    return true;
}

void wire_transaction_end(void)
{
    s_active = false;
    s_session = 0;
    s_owner = WIRE_USB;
    if (s_wire_lock) xSemaphoreGive(s_wire_lock);
}

wire_owner_t wire_owner(void) { return s_active ? s_owner : WIRE_USB; }
uint32_t wire_session(void) { return s_active && s_owner == WIRE_WIFI ? s_session : 0; }

int wire_read(void *buffer, size_t length)
{
    if (!buffer || !length) return 0;
    if (wire_owner() == WIRE_WIFI) return wifi_link_read(s_session, buffer, length);
    int n = usb_serial_jtag_ll_read_rxfifo(buffer, length > 64 ? 64 : length);
    return n;
}

bool wire_read_exact(uint8_t *dst, size_t len, uint32_t timeout_ms)
{
    size_t got = 0;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    while (got < len && (int32_t)(deadline - xTaskGetTickCount()) > 0) {
        int n = wire_read(dst + got, len - got);
        if (n < 0) return false;
        if (n > 0) got += (size_t)n;
        else taskYIELD();
    }
    return got == len;
}

bool wire_read_line(char *line, size_t cap, uint32_t timeout_ms)
{
    if (!line || cap < 2) return false;
    size_t used = 0;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    while ((int32_t)(deadline - xTaskGetTickCount()) > 0) {
        uint8_t c;
        int n = wire_read(&c, 1);
        if (n < 0) return false;
        if (!n) { vTaskDelay(1); continue; }
        if (c == '\n' || c == '\r') {
            if (used) { line[used] = '\0'; return true; }
            continue;
        }
        if (c < 32 || c > 126 || used + 1 >= cap) return false;
        line[used++] = (char)c;
    }
    return false;
}

static bool wire_write(const char *data, size_t length)
{
    if (wire_owner() == WIRE_WIFI) {
        /* The link API is bounded; chunk large frame/text batches explicitly. */
        while (length) {
            size_t n = length > 256 ? 256 : length;
            if (!wifi_link_write(s_session, data, n)) return false;
            data += n; length -= n;
        }
        return true;
    }
    /* stdio is the USB console driver and handles arbitrarily large writes. */
    size_t sent = fwrite(data, 1, length, stdout);
    return sent == length && fflush(stdout) == 0;
}

int wire_vprintf(const char *format, va_list args)
{
    char buffer[1024];
    va_list copy;
    va_copy(copy, args);
    int needed = vsnprintf(buffer, sizeof(buffer), format, copy);
    va_end(copy);
    if (needed < 0) return needed;
    if ((size_t)needed >= sizeof(buffer)) return -1;
    return wire_write(buffer, (size_t)needed) ? needed : -1;
}

int wire_broadcast_printf(const char *format, ...)
{
    if (!s_wire_lock || xSemaphoreTake(s_wire_lock, portMAX_DELAY) != pdTRUE) return -1;
    char buffer[1024]; va_list args; va_start(args, format);
    int n = vsnprintf(buffer, sizeof(buffer), format, args); va_end(args);
    if (n < 0 || (size_t)n >= sizeof(buffer)) { xSemaphoreGive(s_wire_lock); return -1; }
    bool ok = fwrite(buffer, 1, (size_t)n, stdout) == (size_t)n && fflush(stdout) == 0;
    uint32_t session = wifi_link_session();
    if (session) ok = wifi_link_write(session, buffer, (size_t)n) && ok;
    xSemaphoreGive(s_wire_lock);
    return ok ? n : -1;
}

int wire_printf(const char *format, ...)
{
    va_list args; va_start(args, format);
    int result = wire_vprintf(format, args);
    va_end(args);
    return result;
}
