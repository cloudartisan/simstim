/* USB-only Wi-Fi provisioning for the standalone K151 image.
 * Wire format is deliberately fixed and bounded; never print credential data.
 */
#include "wifi_link.h"

#include <string.h>
#include <stdio.h>

#include "esp_err.h"
#include "esp_rom_crc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hal/usb_serial_jtag_ll.h"
#include "nvs.h"
#include "nvs_flash.h"

#define WIFI_NAMESPACE "simstim_wifi"
#define WIFI_RECORD_KEY "credential"
#define WIFI_RECORD_MAGIC 0x53495746u /* "SIWF" */
#define WIFI_RECORD_VERSION 1u
#define WIFI_WIRE_MIN 43u
#define WIFI_WIRE_MAX 130u
#define WIFI_USB_TIMEOUT_MS 10000u

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t version;
    uint8_t ssid_len;
    uint8_t pass_len;
    char ssid[33];
    char pass[65];
    uint8_t key[32];
    uint32_t crc;
} wifi_record_t;

static bool valid_creds(const simstim_wifi_creds *c)
{
    size_t ssid_len = strnlen(c->ssid, sizeof(c->ssid));
    size_t pass_len = strnlen(c->pass, sizeof(c->pass));
    return ssid_len > 0 && ssid_len <= 32 && pass_len >= 8 && pass_len <= 64;
}

static bool read_exact(uint8_t *dst, size_t len, TickType_t deadline)
{
    size_t received = 0;
    while (received < len && (int32_t)(deadline - xTaskGetTickCount()) > 0) {
        size_t request = len - received;
        if (request > 64) request = 64;
        int n = usb_serial_jtag_ll_read_rxfifo(dst + received, request);
        if (n > 0) received += (size_t)n;
        else vTaskDelay(1);
    }
    return received == len;
}

static bool write_status(const char *status)
{
    /* Status strings are fixed literals and never contain supplied data. */
    return fputs(status, stdout) >= 0 && fflush(stdout) == 0;
}

static bool ensure_nvs_ready(void)
{
    esp_err_t err = nvs_flash_init_partition("nvs");
    /* A second call in this boot means the partition is already initialized.
     * Capacity/version errors fail closed; never erase application NVS. */
    return err == ESP_OK || err == ESP_ERR_INVALID_STATE;
}

bool simstim_wifi_load(simstim_wifi_creds *out)
{
    if (out == NULL) return false;
    memset(out, 0, sizeof(*out));
    if (!ensure_nvs_ready()) return false;

    nvs_handle_t handle;
    if (nvs_open_from_partition("nvs", WIFI_NAMESPACE, NVS_READONLY, &handle) != ESP_OK)
        return false;
    wifi_record_t record;
    size_t size = sizeof(record);
    esp_err_t err = nvs_get_blob(handle, WIFI_RECORD_KEY, &record, &size);
    nvs_close(handle);
    if (err != ESP_OK || size != sizeof(record) ||
        record.magic != WIFI_RECORD_MAGIC || record.version != WIFI_RECORD_VERSION ||
        record.ssid_len == 0 || record.ssid_len > 32 ||
        record.pass_len < 8 || record.pass_len > 64 ||
        record.ssid[record.ssid_len] != '\0' ||
        record.pass[record.pass_len] != '\0' ||
        record.crc != esp_rom_crc32_le(0, (const uint8_t *)&record,
                                       sizeof(record) - sizeof(record.crc))) {
        memset(&record, 0, sizeof(record));
        return false;
    }
    memcpy(out->ssid, record.ssid, sizeof(out->ssid));
    memcpy(out->pass, record.pass, sizeof(out->pass));
    memcpy(out->key, record.key, sizeof(out->key));
    memset(&record, 0, sizeof(record));
    if (!valid_creds(out)) {
        memset(out, 0, sizeof(*out));
        return false;
    }
    return true;
}

void simstim_wifi_provision_usb(void)
{
    uint8_t header[8];
    uint8_t wire[WIFI_WIRE_MAX];
    simstim_wifi_creds creds = {0};
    wifi_record_t record = {0};

    /* Host sends <uint32_le length, uint32_le CRC32>, then a bounded body:
     * ssid_len:u8, pass_len:u8, ssid, pass, raw 32-byte device key.
     */
    if (!write_status("WIFI_HEADER\n")) return;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(WIFI_USB_TIMEOUT_MS);
    if (!read_exact(header, sizeof(header), deadline)) {
        (void)write_status("WIFI_ERROR timeout\n");
        return;
    }
    uint32_t body_len = (uint32_t)header[0] | ((uint32_t)header[1] << 8) |
                        ((uint32_t)header[2] << 16) | ((uint32_t)header[3] << 24);
    uint32_t expected_crc = (uint32_t)header[4] | ((uint32_t)header[5] << 8) |
                           ((uint32_t)header[6] << 16) | ((uint32_t)header[7] << 24);
    if (body_len < WIFI_WIRE_MIN || body_len > WIFI_WIRE_MAX) {
        (void)write_status("WIFI_ERROR length\n");
        return;
    }
    if (!write_status("WIFI_READY\n")) return;
    if (!read_exact(wire, body_len, deadline)) {
        (void)write_status("WIFI_ERROR timeout\n");
        goto done;
    }
    if (esp_rom_crc32_le(0, wire, body_len) != expected_crc) {
        (void)write_status("WIFI_ERROR crc\n");
        goto done;
    }
    uint8_t ssid_len = wire[0];
    uint8_t pass_len = wire[1];
    if (ssid_len == 0 || ssid_len > 32 || pass_len < 8 || pass_len > 64 ||
        body_len != (uint32_t)ssid_len + pass_len + 34) {
        (void)write_status("WIFI_ERROR length\n");
        goto done;
    }
    memcpy(creds.ssid, wire + 2, ssid_len);
    memcpy(creds.pass, wire + 2 + ssid_len, pass_len);
    memcpy(creds.key, wire + 2 + ssid_len + pass_len, sizeof(creds.key));
    if (memchr(creds.ssid, '\0', ssid_len) || memchr(creds.pass, '\0', pass_len) ||
        !valid_creds(&creds)) {
        (void)write_status("WIFI_ERROR data\n");
        goto done;
    }

    if (!ensure_nvs_ready()) {
        (void)write_status("WIFI_ERROR storage\n");
        goto done;
    }
    record.magic = WIFI_RECORD_MAGIC;
    record.version = WIFI_RECORD_VERSION;
    record.ssid_len = ssid_len;
    record.pass_len = pass_len;
    memcpy(record.ssid, creds.ssid, ssid_len);
    memcpy(record.pass, creds.pass, pass_len);
    memcpy(record.key, creds.key, sizeof(record.key));
    record.crc = esp_rom_crc32_le(0, (const uint8_t *)&record,
                                  sizeof(record) - sizeof(record.crc));

    nvs_handle_t handle;
    if (nvs_open_from_partition("nvs", WIFI_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) {
        (void)write_status("WIFI_ERROR storage\n");
        goto done;
    }
    esp_err_t err = nvs_set_blob(handle, WIFI_RECORD_KEY, &record, sizeof(record));
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err == ESP_OK) (void)write_status("WIFI_STORED reboot_required\n");
    else (void)write_status("WIFI_ERROR storage\n");

done:
    memset(wire, 0, sizeof(wire));
    memset(&creds, 0, sizeof(creds));
    memset(&record, 0, sizeof(record));
    memset(header, 0, sizeof(header));
}
