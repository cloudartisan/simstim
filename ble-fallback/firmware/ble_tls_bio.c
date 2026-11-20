#include "ble_tls_bio.h"
#include <limits.h>
#include <stddef.h>

static int bio_send(void *ctx, const unsigned char *buf, size_t len) {
    simstim_ble_stream_t *s = (simstim_ble_stream_t *)ctx;
    if (!s || !buf || !s->connected) { if (s) simstim_ble_stream_record_failure(s,SIMSTIM_BLE_CLOSE_TX,MBEDTLS_ERR_NET_CONN_RESET); return MBEDTLS_ERR_NET_CONN_RESET; }
    if (!len) return 0;
    size_t n = simstim_ble_stream_write(s, buf, len);
    if (n) return n > INT_MAX ? INT_MAX : (int)n;
    return MBEDTLS_ERR_SSL_WANT_WRITE;
}

static int bio_recv(void *ctx, unsigned char *buf, size_t len) {
    simstim_ble_stream_t *s = (simstim_ble_stream_t *)ctx;
    if (!s || !buf || !s->connected) { if (s) simstim_ble_stream_record_failure(s,SIMSTIM_BLE_CLOSE_RX,MBEDTLS_ERR_NET_CONN_RESET); return MBEDTLS_ERR_NET_CONN_RESET; }
    if (!len) return 0;
    size_t n = simstim_ble_stream_read(s, buf, len);
    if (n) return n > INT_MAX ? INT_MAX : (int)n;
    return MBEDTLS_ERR_SSL_WANT_READ;
}

int simstim_ble_tls_bio_attach(mbedtls_ssl_context *ssl, simstim_ble_stream_t *stream) {
    if (!ssl || !stream || !stream->connected) return MBEDTLS_ERR_SSL_BAD_INPUT_DATA;
    mbedtls_ssl_set_bio(ssl, stream, bio_send, bio_recv, NULL);
    return 0;
}

int simstim_ble_tls_bio_handshake(mbedtls_ssl_context *ssl, simstim_ble_stream_t *stream) {
    if (!ssl || !stream || !stream->connected) return MBEDTLS_ERR_SSL_BAD_INPUT_DATA;
    int rc = simstim_ble_tls_bio_attach(ssl, stream);
    if (rc != 0) { simstim_ble_stream_record_failure(stream,SIMSTIM_BLE_CLOSE_RETURN,rc); return rc; }
    rc = mbedtls_ssl_handshake(ssl);
    if (rc == 0) simstim_ble_stream_set_tls_authenticated(stream, true);
    else { simstim_ble_stream_set_tls_authenticated(stream, false); simstim_ble_stream_record_failure(stream,SIMSTIM_BLE_CLOSE_TLS,rc); }
    return rc;
}

void simstim_ble_tls_bio_detach(mbedtls_ssl_context *ssl) {
    if (ssl) mbedtls_ssl_set_bio(ssl, NULL, NULL, NULL, NULL);
}
