#pragma once
#include "ble_stream.h"
#include "ack_pump.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
struct os_mbuf;
#ifdef __cplusplus
extern "C" {
#endif

/* Provisioned peer address only; no scan/discovery operation is exposed. */
int simstim_nimble_connect_peer(const ble_addr_t *peer, ble_gap_event_fn *events,
                                void *arg, uint32_t timeout_ms);

/* Discovery runs on an already-connected handle. ready is set only after the
 * matching service/chars/CCCD are found and the CCCD write succeeds. The caller
 * must call disconnect from its GAP disconnect event before reusing the state. */
typedef struct {
    uint16_t conn_handle;
    uint16_t service_start, service_end;
    uint16_t uplink_handle, downlink_handle, cccd_handle;
    bool pending;
    bool ready;
    /* Monotonic session token; callback contexts snapshot it at submission. */
    uint32_t generation;
    simstim_ble_stream_t *stream;
} simstim_nimble_gatt_t;
void simstim_nimble_gatt_init(simstim_nimble_gatt_t *client,
                              simstim_ble_stream_t *stream);
int simstim_nimble_discover(uint16_t conn_handle, simstim_nimble_gatt_t *client);
void simstim_nimble_disconnect(simstim_nimble_gatt_t *client,
                               uint16_t conn_handle, int32_t gap_reason);
int simstim_nimble_subscribe(uint16_t conn_handle, uint16_t cccd_handle,
                             ble_gatt_attr_fn *done, void *arg);
int simstim_nimble_write_fragment(uint16_t conn_handle, uint16_t uplink_handle,
                                  const void *frame, size_t frame_len, ack_cell *cell);
int simstim_nimble_notification(simstim_ble_stream_t *stream, struct os_mbuf *om);
#ifdef __cplusplus
}
#endif
