#include "ble_stream.h"
#include <stdio.h>
#include <string.h>

static size_t used(uint32_t head, uint32_t tail) { return (size_t)(head - tail); }
static size_t put(uint8_t *ring, volatile uint32_t *head, volatile uint32_t *tail,
                  const void *src, size_t len) {
    size_t n = SIMSTIM_BLE_RING_CAPACITY - used(*head, *tail);
    if (n > len) n = len;
    const uint8_t *p = (const uint8_t *)src;
    for (size_t i = 0; i < n; ++i) ring[(*head + (uint32_t)i) % SIMSTIM_BLE_RING_CAPACITY] = p[i];
    *head += (uint32_t)n;
    return n;
}
static size_t get(uint8_t *ring, volatile uint32_t *head, volatile uint32_t *tail,
                  void *dst, size_t len) {
    size_t n = used(*head, *tail);
    if (n > len) n = len;
    uint8_t *p = (uint8_t *)dst;
    for (size_t i = 0; i < n; ++i) p[i] = ring[(*tail + (uint32_t)i) % SIMSTIM_BLE_RING_CAPACITY];
    *tail += (uint32_t)n;
    return n;
}
void simstim_ble_stream_init(simstim_ble_stream_t *s) {
    if (s) { memset(s, 0, sizeof(*s)); }
}
void simstim_ble_stream_begin_generation(simstim_ble_stream_t *s) {
    if (!s) return;
    ++s->generation;
    memset(&s->current_diagnostic, 0, sizeof(s->current_diagnostic));
    s->current_diagnostic.generation = s->generation;
    s->connected = true;
}
void simstim_ble_stream_record_failure(simstim_ble_stream_t *s,
                                       simstim_ble_close_cause_t cause,
                                       int32_t detail) {
    if (!s || cause == SIMSTIM_BLE_CLOSE_NONE || s->current_diagnostic.first_cause != SIMSTIM_BLE_CLOSE_NONE) return;
    s->current_diagnostic.first_cause = cause;
    if (cause == SIMSTIM_BLE_CLOSE_GAP) s->current_diagnostic.gap_reason = detail;
    else s->current_diagnostic.low_level_return = detail;
}
size_t simstim_ble_stream_write(simstim_ble_stream_t *s, const void *data, size_t len) {
    if (!s || !data || !s->connected || !len) return 0;
    size_t n = put(s->tx, &s->tx_head, &s->tx_tail, data, len);
    /* Partial ring admission is normal nonblocking BIO behavior. It is not a
     * transport failure: the TLS caller must resume with the remaining bytes. */
    return n;
}
size_t simstim_ble_stream_read(simstim_ble_stream_t *s, void *data, size_t len) {
    if (!s || !data || !s->connected || !len) return 0;
    size_t n = get(s->rx, &s->rx_head, &s->rx_tail, data, len);
    return n;
}
size_t simstim_ble_stream_ingress(simstim_ble_stream_t *s, const void *data, size_t len) {
    if (!s || !data || !s->connected || !len || len > SIMSTIM_BLE_FRAGMENT_MAX) {
        if (s) { ++s->current_diagnostic.fragment_rejections; simstim_ble_stream_record_failure(s, SIMSTIM_BLE_CLOSE_FRAGMENT, -1); }
        return 0;
    }
    size_t n = put(s->rx, &s->rx_head, &s->rx_tail, data, len);
    if (n != len) { ++s->current_diagnostic.fragment_rejections; simstim_ble_stream_record_failure(s, SIMSTIM_BLE_CLOSE_FRAGMENT, -2); }
    else s->current_diagnostic.rx_bytes += (uint32_t)n;
    return n;
}
size_t simstim_ble_stream_egress(simstim_ble_stream_t *s, void *data, size_t max_len) {
    if (!s || !data || !s->connected || !max_len) return 0;
    if (max_len > SIMSTIM_BLE_FRAGMENT_MAX) max_len = SIMSTIM_BLE_FRAGMENT_MAX;
    size_t n = get(s->tx, &s->tx_head, &s->tx_tail, data, max_len);
    s->current_diagnostic.tx_bytes += (uint32_t)n;
    return n;
}
void simstim_ble_stream_set_tls_authenticated(simstim_ble_stream_t *s, bool authenticated) {
    if (s) s->authenticated = authenticated && s->connected;
}
void simstim_ble_stream_disconnect(simstim_ble_stream_t *s, int32_t gap_reason) {
    if (!s) return;
    if (s->current_diagnostic.first_cause == SIMSTIM_BLE_CLOSE_NONE)
        simstim_ble_stream_record_failure(s, SIMSTIM_BLE_CLOSE_GAP, gap_reason);
    s->last_failed_diagnostic = s->current_diagnostic;
    s->has_last_failed_diagnostic = true;
    s->authenticated = false; s->connected = false;
    s->rx_head = s->rx_tail = s->tx_head = s->tx_tail = 0;
    s->rx_next_sequence = s->tx_next_sequence = 0;
}
int simstim_ble_diagnostic_read(const simstim_ble_stream_t *s, bool transport_authenticated,
                                char *out, size_t cap) {
    if (!s || !out || !cap || !transport_authenticated || !s->has_last_failed_diagnostic) return 0;
    const simstim_ble_diagnostic_t *d = &s->last_failed_diagnostic;
    const char *cause = "unknown";
    switch (d->first_cause) {
    case SIMSTIM_BLE_CLOSE_GAP: cause = "gap"; break;
    case SIMSTIM_BLE_CLOSE_TLS: cause = "tls"; break;
    case SIMSTIM_BLE_CLOSE_RETURN: cause = "return"; break;
    case SIMSTIM_BLE_CLOSE_FRAGMENT: cause = "fragment"; break;
    case SIMSTIM_BLE_CLOSE_TX: cause = "tx"; break;
    case SIMSTIM_BLE_CLOSE_RX: cause = "rx"; break;
    case SIMSTIM_BLE_CLOSE_WRITE: cause = "write"; break;
    default: break;
    }
    int n = snprintf(out, cap, "SIMSTIM_BLE_DIAG generation=%lu cause=%s gap=%ld rc=%ld tx=%lu rx=%lu fragment_reject=%lu\n",
        (unsigned long)d->generation, cause, (long)d->gap_reason, (long)d->low_level_return,
        (unsigned long)d->tx_bytes, (unsigned long)d->rx_bytes,
        (unsigned long)d->fragment_rejections);
    return n > 0 && (size_t)n < cap ? n : 0;
}
