#pragma once
#include "ble_stream.h"
#include <mbedtls/ssl.h>
#include <mbedtls/net_sockets.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Bind an already configured TLS client to the BLE byte stream. TLS policy
 * (PSK identity/key, TLS 1.2 version and cipher suite) is owned by caller and
 * must match the Wi-Fi client policy; this function does not set credentials. */
int simstim_ble_tls_bio_attach(mbedtls_ssl_context *ssl, simstim_ble_stream_t *stream);
int simstim_ble_tls_bio_handshake(mbedtls_ssl_context *ssl, simstim_ble_stream_t *stream);
void simstim_ble_tls_bio_detach(mbedtls_ssl_context *ssl);
#ifdef __cplusplus
}
#endif
