#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SIMSTIM_BLE_RING_CAPACITY 4096u
#define SIMSTIM_BLE_FRAGMENT_MAX 512u

typedef enum {
    SIMSTIM_BLE_CLOSE_NONE = 0,
    SIMSTIM_BLE_CLOSE_GAP = 1,
    SIMSTIM_BLE_CLOSE_TLS = 2,
    SIMSTIM_BLE_CLOSE_RETURN = 3,
    SIMSTIM_BLE_CLOSE_FRAGMENT = 4,
    SIMSTIM_BLE_CLOSE_TX = 5,
    SIMSTIM_BLE_CLOSE_RX = 6,
    SIMSTIM_BLE_CLOSE_WRITE = 7
} simstim_ble_close_cause_t;

/* Metadata only: never stores packet, TLS, credential, or room bytes. */
typedef struct {
    uint32_t generation;
    simstim_ble_close_cause_t first_cause;
    int32_t gap_reason;
    int32_t low_level_return;
    uint32_t tx_bytes, rx_bytes, fragment_rejections;
} simstim_ble_diagnostic_t;

/* One TLS byte stream over a pre-provisioned BLE peer. No scan API is exposed. */
typedef struct {
    uint8_t rx[SIMSTIM_BLE_RING_CAPACITY];
    uint8_t tx[SIMSTIM_BLE_RING_CAPACITY];
    volatile uint32_t rx_head, rx_tail, tx_head, tx_tail;
    uint16_t rx_next_sequence, tx_next_sequence;
    volatile bool connected;
    volatile bool authenticated; /* Set only by TLS owner after PSK handshake. */
    uint32_t generation;
    simstim_ble_diagnostic_t current_diagnostic;
    simstim_ble_diagnostic_t last_failed_diagnostic;
    bool has_last_failed_diagnostic;
} simstim_ble_stream_t;

void simstim_ble_stream_init(simstim_ble_stream_t *s);
/* Queue bytes for the TLS client; returns bytes accepted (possibly partial). */
size_t simstim_ble_stream_write(simstim_ble_stream_t *s, const void *data, size_t len);
/* Raw TLS records from BLE, not application plaintext. The TLS layer must be
 * allowed to read before authentication; gate all application dispatch on TLS. */
size_t simstim_ble_stream_read(simstim_ble_stream_t *s, void *data, size_t len);
/* Called by NimBLE GATT callbacks with ordered raw stream bytes. */
size_t simstim_ble_stream_ingress(simstim_ble_stream_t *s, const void *data, size_t len);
/* Pull next outgoing raw stream bytes for GATT fragmentation. */
size_t simstim_ble_stream_egress(simstim_ble_stream_t *s, void *data, size_t max_len);
void simstim_ble_stream_set_tls_authenticated(simstim_ble_stream_t *s, bool authenticated);
/* Start a new physical connection generation without erasing prior failure. */
void simstim_ble_stream_begin_generation(simstim_ble_stream_t *s);
/* First cause wins; detail carries GAP reason or low-level TLS/return code. */
void simstim_ble_stream_record_failure(simstim_ble_stream_t *s,
                                       simstim_ble_close_cause_t cause,
                                       int32_t detail);
void simstim_ble_stream_disconnect(simstim_ble_stream_t *s, int32_t gap_reason);
/* Read-only allowlisted line. Caller supplies true only inside an authenticated control session. */
int simstim_ble_diagnostic_read(const simstim_ble_stream_t *s,
                                bool transport_authenticated, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
