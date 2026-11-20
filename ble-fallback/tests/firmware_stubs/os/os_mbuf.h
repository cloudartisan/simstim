#pragma once
#include <stdint.h>
struct os_mbuf { uint16_t len; uint8_t *data; };
#define OS_MBUF_PKTLEN(m) ((m)->len)
int os_mbuf_copydata(const struct os_mbuf *,int,uint16_t,void *);
