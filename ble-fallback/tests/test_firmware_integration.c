#include "mbedtls/ssl.h"
#include "ble_tls_bio.h"
#include "transport_selector.h"
#include <assert.h>
#include <string.h>
void mbedtls_ssl_set_bio(mbedtls_ssl_context *s, void *ctx,
 int (*send_cb)(void *,const unsigned char *,size_t),
 int (*recv_cb)(void *,unsigned char *,size_t), void *unused) {
 (void)unused; s->ctx=ctx; s->send_cb=send_cb; s->recv_cb=recv_cb;
}
int mbedtls_ssl_handshake(mbedtls_ssl_context *s) { (void)s; return 0; }
int main(void) {
 simstim_ble_stream_t stream; simstim_ble_stream_init(&stream); simstim_ble_stream_begin_generation(&stream);
 mbedtls_ssl_context ssl={0}; assert(simstim_ble_tls_bio_attach(&ssl,&stream)==0);
 unsigned char in[]="tls-in", out[16]={0};
 assert(ssl.send_cb(ssl.ctx,in,6)==6); assert(simstim_ble_stream_egress(&stream,out,sizeof(out))==6); assert(!memcmp(in,out,6));
 assert(ssl.recv_cb(ssl.ctx,out,sizeof(out))==MBEDTLS_ERR_SSL_WANT_READ);
 assert(simstim_ble_stream_ingress(&stream,in,6)==6); assert(ssl.recv_cb(ssl.ctx,out,sizeof(out))==6); assert(!memcmp(in,out,6));
 assert(simstim_ble_tls_bio_handshake(&ssl,&stream)==0); assert(stream.authenticated);
 char diagnostic[256];
 simstim_ble_stream_record_write(&stream,41,0);
 simstim_ble_stream_record_failure(&stream,SIMSTIM_BLE_CLOSE_TLS,-0x1234);
 simstim_ble_stream_disconnect(&stream,0x13); assert(!stream.authenticated);
 assert(simstim_ble_diagnostic_read(&stream,false,diagnostic,sizeof(diagnostic))==0);
 simstim_ble_stream_begin_generation(&stream);
 assert(simstim_ble_diagnostic_read(&stream,false,diagnostic,sizeof(diagnostic))==0);
 simstim_ble_stream_set_tls_authenticated(&stream,true);
 assert(simstim_ble_diagnostic_read(&stream,true,diagnostic,sizeof(diagnostic))>0);
 assert(strstr(diagnostic,"generation=1 cause=tls gap=0 rc=-4660")!=NULL);
 assert(strstr(diagnostic," tx=6 rx=6 fragment_reject=0")!=NULL);
 assert(strstr(diagnostic,"write_acks=1 write_seq=41 write_rc=0")!=NULL);
 simstim_ble_stream_record_failure(&stream,SIMSTIM_BLE_CLOSE_GAP,0x08);
 simstim_ble_stream_disconnect(&stream,0x08);
 assert(simstim_ble_diagnostic_read(&stream,false,diagnostic,sizeof(diagnostic))==0);
 simstim_ble_stream_begin_generation(&stream);
 simstim_ble_stream_set_tls_authenticated(&stream,true);
 assert(simstim_ble_diagnostic_read(&stream,true,diagnostic,sizeof(diagnostic))>0);
 assert(strstr(diagnostic,"generation=2 cause=gap gap=8")!=NULL);
 simstim_transport_selector_t sel; simstim_transport_selector_init(&sel,true);
 simstim_transport_selector_set_ble(&sel,true); assert(simstim_transport_selector_acquire(&sel)==SIMSTIM_TRANSPORT_BLE);
 simstim_transport_selector_set_wifi(&sel,true); assert(simstim_transport_selector_acquire(&sel)==SIMSTIM_TRANSPORT_BLE);
 simstim_transport_selector_release(&sel,SIMSTIM_TRANSPORT_BLE); assert(simstim_transport_selector_acquire(&sel)==SIMSTIM_TRANSPORT_WIFI);
 simstim_transport_selector_init(&sel,false); simstim_transport_selector_set_ble(&sel,true); assert(simstim_transport_selector_acquire(&sel)==SIMSTIM_TRANSPORT_NONE);
 return 0;
}
