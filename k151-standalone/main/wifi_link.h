#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
/* Only the network task owns TLS. Session handles never cross reconnects.
 * The command task latches one generation for an entire transaction.
 * No API falls back to another transport on failure. */
void wifi_link_start(void);
uint32_t wifi_link_session(void); /* zero = none */
int wifi_link_read(uint32_t session, void *buf, size_t len); /* nonblocking; <0 lost */
bool wifi_link_write(uint32_t session, const void *buf, size_t len); /* bounded */
/* Credentials are loaded once at boot; no writes from network. */
typedef struct { char ssid[33]; char pass[65]; uint8_t key[32]; } simstim_wifi_creds;
bool simstim_wifi_load(simstim_wifi_creds *out);
/* Called only from physical USB command dispatcher; reads binary via USB FIFO. */
void simstim_wifi_provision_usb(void);
