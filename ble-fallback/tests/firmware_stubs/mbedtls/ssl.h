#pragma once
#include <stddef.h>
typedef struct { void *ctx; int (*send_cb)(void *,const unsigned char *,size_t); int (*recv_cb)(void *,unsigned char *,size_t); } mbedtls_ssl_context;
#define MBEDTLS_ERR_NET_CONN_RESET -0x0050
#define MBEDTLS_ERR_SSL_WANT_WRITE -0x6900
#define MBEDTLS_ERR_SSL_WANT_READ -0x6900
#define MBEDTLS_ERR_SSL_BAD_INPUT_DATA -0x7100
void mbedtls_ssl_set_bio(mbedtls_ssl_context *, void *, int (*)(void *,const unsigned char *,size_t), int (*)(void *,unsigned char *,size_t), void *);
int mbedtls_ssl_handshake(mbedtls_ssl_context *);
