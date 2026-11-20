#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>

typedef enum { WIRE_USB = 0, WIRE_WIFI = 1 } wire_owner_t;

void wire_io_init(void);
/* Latch the selected transport for one complete command transaction. */
bool wire_transaction_begin(wire_owner_t owner, uint32_t session);
void wire_transaction_end(void);
wire_owner_t wire_owner(void);
uint32_t wire_session(void);
int wire_read(void *buffer, size_t length); /* nonblocking; -1 means link lost */
bool wire_read_exact(uint8_t *dst, size_t len, uint32_t timeout_ms);
bool wire_read_line(char *line, size_t cap, uint32_t timeout_ms);
int wire_vprintf(const char *format, va_list args);
int wire_printf(const char *format, ...);
/* Atomic idle-only copy to USB and the current live WiFi generation. */
int wire_broadcast_printf(const char *format, ...);
/* Output is routed explicitly through wire_printf at call sites. */
