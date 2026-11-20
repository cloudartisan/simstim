#include "ble_channel.h"
#include "simstim_public_config.h"

#include <stdio.h>
#include <string.h>

#include "ble_stream.h"
#include "nimble_gatt_client.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

#define RADIO_CONNECTED BIT0
#define RADIO_FAILED    BIT1
#define RADIO_FRAME_HEADER 4u

static EventGroupHandle_t radio_events;
static SemaphoreHandle_t stream_lock;
#include "esp_attr.h"
static EXT_RAM_BSS_ATTR simstim_ble_stream_t radio_stream;
static simstim_nimble_gatt_t radio_gatt;
static volatile uint16_t radio_conn = BLE_HS_CONN_HANDLE_NONE;
static volatile uint16_t radio_mtu = 23;
static volatile bool radio_disconnected;
static bool radio_stack_started;
static bool radio_stream_initialized;
static bool radio_opened;
static uint8_t staged_frame[SIMSTIM_BLE_FRAGMENT_MAX + RADIO_FRAME_HEADER];
static size_t staged_len;
static ack_pump uplink;
static int submit_uplink(void *arg,const void *data,size_t len,ack_cell *cell) {
    (void)arg; return simstim_nimble_write_fragment(radio_conn,radio_gatt.uplink_handle,data,len,cell);
}
static uint16_t staged_sequence;
static uintptr_t attempt, next_attempt;
static bool current(void *arg) { return (uintptr_t)arg && (uintptr_t)arg==__atomic_load_n(&attempt,__ATOMIC_ACQUIRE); }

/* The public config uses conventional display order; NimBLE stores LSB first. */
static bool configured_peer(ble_addr_t *out)
{
    unsigned octet[6];
    if (!out || sscanf(SIMSTIM_BLE_PEER_ADDRESS, "%2x:%2x:%2x:%2x:%2x:%2x",
                       &octet[0], &octet[1], &octet[2], &octet[3],
                       &octet[4], &octet[5]) != 6) return false;
    out->type = BLE_ADDR_PUBLIC;
    for (unsigned i = 0; i < 6; ++i) out->val[i] = (uint8_t)octet[5 - i];
    return true;
}

static int gap_event(struct ble_gap_event *event, void *arg);

static void host_task(void *arg)
{
    (void)arg;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void sync_callback(void)
{
    /* The single opener initiates every attempt, not just first host sync. */
}

static int mtu_exchange_done(uint16_t conn_handle,
                             const struct ble_gatt_error *error,
                             uint16_t mtu, void *arg)
{
    if (!current(arg) || conn_handle != radio_conn) return 0;
    if (error->status != 0 || mtu < 23) {
        xEventGroupSetBits(radio_events, RADIO_FAILED);
        return 0;
    }
    radio_mtu = mtu;
    if (simstim_nimble_discover(conn_handle, &radio_gatt) != 0)
        xEventGroupSetBits(radio_events, RADIO_FAILED);
    return 0;
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    if (!current(arg)) {
        if (event->type==BLE_GAP_EVENT_CONNECT && !event->connect.status)
            (void)ble_gap_terminate(event->connect.conn_handle,BLE_ERR_REM_USER_CONN_TERM);
        return 0;
    }
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status != 0) {
            xEventGroupSetBits(radio_events, RADIO_FAILED);
            return 0;
        }
        radio_conn = event->connect.conn_handle;
        radio_disconnected = false;
        radio_mtu = 23;
        xSemaphoreTake(stream_lock, portMAX_DELAY);
        simstim_ble_stream_begin_generation(&radio_stream);
        xSemaphoreGive(stream_lock);
        xEventGroupSetBits(radio_events, RADIO_CONNECTED);
        if (ble_gattc_exchange_mtu(radio_conn, mtu_exchange_done, arg) != 0)
            xEventGroupSetBits(radio_events, RADIO_FAILED);
        return 0;

    case BLE_GAP_EVENT_NOTIFY_RX:
        if (event->notify_rx.conn_handle == radio_conn &&
            event->notify_rx.attr_handle == radio_gatt.downlink_handle &&
            !event->notify_rx.indication && event->notify_rx.om) {
            xSemaphoreTake(stream_lock, portMAX_DELAY);
            int rc = simstim_nimble_notification(&radio_stream, event->notify_rx.om);
            xSemaphoreGive(stream_lock);
            if (rc != 0) {
                /* A gap or overflow invalidates the TLS byte stream. */
                xEventGroupSetBits(radio_events, RADIO_FAILED);
                return ble_gap_terminate(radio_conn, BLE_ERR_REM_USER_CONN_TERM);
            }
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        if (event->disconnect.conn.conn_handle == radio_conn) {
            radio_disconnected = true;
            simstim_nimble_disconnect(&radio_gatt, radio_conn, event->disconnect.reason);
            radio_conn = BLE_HS_CONN_HANDLE_NONE;
            xEventGroupSetBits(radio_events, RADIO_FAILED);
        }
        return 0;

    case BLE_GAP_EVENT_MTU:
        if (event->mtu.conn_handle == radio_conn && event->mtu.value >= 23)
            radio_mtu = event->mtu.value;
        return 0;

    default:
        return 0;
    }
}

static bool ready(void)
{
    if (radio_disconnected || radio_conn == BLE_HS_CONN_HANDLE_NONE) return false;
    if (!radio_gatt.ready) return false;
    xSemaphoreTake(stream_lock, portMAX_DELAY);
    radio_stream.connected = true;
    xSemaphoreGive(stream_lock);
    return true;
}

/* Keep an encoded fragment until NimBLE accepts it. In particular, EBUSY must
 * not advance either the byte stream or the on-wire sequence number. */
static int flush_one_frame(void)
{
    if (radio_conn == BLE_HS_CONN_HANDLE_NONE || !radio_gatt.ready) return 0;
    if (staged_len == 0) {
        size_t att_payload = (size_t)radio_mtu - 3u;
        if (att_payload <= RADIO_FRAME_HEADER) return 0;
        size_t payload_cap = att_payload - RADIO_FRAME_HEADER;
        if (payload_cap > SIMSTIM_BLE_FRAGMENT_MAX) payload_cap = SIMSTIM_BLE_FRAGMENT_MAX;
        uint8_t payload[SIMSTIM_BLE_FRAGMENT_MAX];
        xSemaphoreTake(stream_lock, portMAX_DELAY);
        size_t n = simstim_ble_stream_egress(&radio_stream, payload, payload_cap);
        xSemaphoreGive(stream_lock);
        if (!n) return 0;
        staged_sequence = radio_stream.tx_next_sequence;
        staged_frame[0] = (uint8_t)(staged_sequence >> 8);
        staged_frame[1] = (uint8_t)staged_sequence;
        staged_frame[2] = (uint8_t)(n >> 8);
        staged_frame[3] = (uint8_t)n;
        memcpy(staged_frame + RADIO_FRAME_HEADER, payload, n);
        staged_len = n + RADIO_FRAME_HEADER;
    }
    int rc=ack_step(&uplink,xTaskGetTickCount(),pdMS_TO_TICKS(8000),staged_frame,staged_len,
                    submit_uplink,NULL,BLE_HS_EBUSY,BLE_HS_ENOMEM,BLE_HS_ETIMEOUT);
    if(rc==0) return 0;
    if(rc>0){ radio_stream.tx_next_sequence++; staged_len=0; return 1; }
    xSemaphoreTake(stream_lock,portMAX_DELAY);
    simstim_ble_stream_record_failure(&radio_stream,SIMSTIM_BLE_CLOSE_WRITE,uplink.reason);
    xSemaphoreGive(stream_lock);
    xEventGroupSetBits(radio_events, RADIO_FAILED);
    return -1;
}

bool ble_radio_open(unsigned timeout_ms)
{
    if (timeout_ms == 0 || radio_opened) return false;
    if (!radio_events) radio_events = xEventGroupCreate();
    if (!stream_lock) stream_lock = xSemaphoreCreateMutex();
    if (!radio_events || !stream_lock) return false;

    xEventGroupClearBits(radio_events, RADIO_CONNECTED | RADIO_FAILED);
    radio_conn = BLE_HS_CONN_HANDLE_NONE;
    radio_mtu = 23;
    radio_disconnected = false;
    staged_len = 0;
    if (!radio_stream_initialized) {
        simstim_ble_stream_init(&radio_stream);
        radio_stream_initialized = true;
        simstim_nimble_gatt_init(&radio_gatt, &radio_stream);
    }

    if (!radio_stack_started) {
        if (nimble_port_init() != ESP_OK) return false;
        ble_hs_cfg.sync_cb = sync_callback;
        nimble_port_freertos_init(host_task);
        radio_stack_started = true;
    }

    TickType_t start = xTaskGetTickCount();
    TickType_t timeout = pdMS_TO_TICKS(timeout_ms);
    while (!ble_hs_synced() && (xTaskGetTickCount()-start)<timeout) vTaskDelay(1);
    TickType_t elapsed=xTaskGetTickCount()-start;
    if (!ble_hs_synced() || elapsed>=timeout) return false;
    if (++next_attempt==0) ++next_attempt;
    __atomic_store_n(&attempt,next_attempt,__ATOMIC_RELEASE);
    ble_addr_t peer;
    if (!configured_peer(&peer) ||
        simstim_nimble_connect_peer(&peer,gap_event,(void *)next_attempt,
                                    pdTICKS_TO_MS(timeout-elapsed))!=0) goto fail;
    EventBits_t bits = xEventGroupWaitBits(radio_events,
        RADIO_CONNECTED | RADIO_FAILED, pdFALSE, pdFALSE, timeout-elapsed);
    if (!(bits & RADIO_CONNECTED) || (bits & RADIO_FAILED)) goto fail;

    /* The shared discovery helper marks ready only after CCCD subscription. */
    while (!radio_gatt.ready && !radio_disconnected &&
           !(xEventGroupGetBits(radio_events) & RADIO_FAILED) &&
           (xTaskGetTickCount() - start) < timeout) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (!ready() || (xEventGroupGetBits(radio_events) & RADIO_FAILED)) goto fail;
    radio_opened = true;
    return true;

fail:
    ble_radio_close();
    return false;
}

int ble_radio_recv(void *unused, unsigned char *dst, size_t cap)
{
    (void)unused;
    if (!dst || cap == 0) return 0;
    if (!radio_opened || radio_disconnected || radio_conn == BLE_HS_CONN_HANDLE_NONE)
        return -1;
    if (!ready()) return 0;
    if ((xEventGroupGetBits(radio_events)&RADIO_FAILED) || flush_one_frame()<0) return -1;
    xSemaphoreTake(stream_lock, portMAX_DELAY);
    size_t n = simstim_ble_stream_read(&radio_stream, dst, cap);
    xSemaphoreGive(stream_lock);
    return (int)n;
}

int ble_radio_send(void *unused, const unsigned char *src, size_t len)
{
    (void)unused;
    if (!src || len == 0) return 0;
    if (!radio_opened || radio_disconnected || radio_conn == BLE_HS_CONN_HANDLE_NONE)
        return -1;
    if (!ready()) return 0;

    if (flush_one_frame()<0) return -1;
    xSemaphoreTake(stream_lock, portMAX_DELAY);
    size_t accepted = simstim_ble_stream_write(&radio_stream, src, len);
    xSemaphoreGive(stream_lock);
    if (flush_one_frame()<0) return -1;
    return (int)accepted;
}

void ble_radio_close(void)
{
    ack_reset(&uplink);
    radio_opened = false;
    __atomic_store_n(&attempt,0,__ATOMIC_RELEASE);
    if (radio_stack_started && ble_gap_conn_active()) (void)ble_gap_conn_cancel();
    if (radio_conn != BLE_HS_CONN_HANDLE_NONE)
        (void)ble_gap_terminate(radio_conn, BLE_ERR_REM_USER_CONN_TERM);
    if (stream_lock) xSemaphoreTake(stream_lock, portMAX_DELAY);
    if (radio_conn != BLE_HS_CONN_HANDLE_NONE)
        simstim_nimble_disconnect(&radio_gatt, radio_conn, BLE_ERR_REM_USER_CONN_TERM);
    else if (radio_stream.connected)
        simstim_ble_stream_disconnect(&radio_stream, BLE_ERR_REM_USER_CONN_TERM);
    staged_len = 0;
    if (stream_lock) xSemaphoreGive(stream_lock);
}

int ble_radio_diagnostic_line(bool authenticated, char *out, size_t cap)
{
    if (!authenticated || !out || cap == 0) return 0;
    if (!radio_stream_initialized) {
        int n = snprintf(out, cap, "SIMSTIM_BLE_DIAG available=0\n");
        return n > 0 && (size_t)n < cap ? n : 0;
    }
    if (stream_lock) xSemaphoreTake(stream_lock, portMAX_DELAY);
    int n = simstim_ble_diagnostic_read(&radio_stream, true, out, cap);
    if (stream_lock) xSemaphoreGive(stream_lock);
    if (n == 0) {
        n = snprintf(out, cap, "SIMSTIM_BLE_DIAG available=0\n");
        return n > 0 && (size_t)n < cap ? n : 0;
    }
    return n;
}

void ble_radio_record_tls_failure(int low_level_return)
{
    if (!radio_stream_initialized) return;
    if (stream_lock) xSemaphoreTake(stream_lock, portMAX_DELAY);
    simstim_ble_stream_record_failure(&radio_stream, SIMSTIM_BLE_CLOSE_TLS, low_level_return);
    if (stream_lock) xSemaphoreGive(stream_lock);
}
