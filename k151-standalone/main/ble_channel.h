#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
// Single worker; decrypted byte pipe only after successful TLS-PSK handshake.
bool ble_channel_open(const uint8_t key[32], unsigned timeout_ms);
int ble_channel_read(uint8_t *dst, size_t cap); // 0 would block, -1 lost
int ble_channel_write(const uint8_t *src, size_t len); // same buffer on would block
void ble_channel_close(void);
// Radio stream interface: ciphertext only, bounded calls, same return semantics.
bool ble_radio_open(unsigned timeout_ms);
int ble_radio_recv(void *unused, unsigned char *dst, size_t cap);
int ble_radio_send(void *unused, const unsigned char *src, size_t len);
void ble_radio_close(void);
/* Caller must gate this on the authenticated transport owning the command. */
int ble_radio_diagnostic_line(bool authenticated, char *out, size_t cap);
void ble_radio_record_tls_failure(int low_level_return);
