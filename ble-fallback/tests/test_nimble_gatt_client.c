#include "nimble_gatt_client.h"
#include "host/ble_hs.h"
#include "os/os_mbuf.h"
#include <assert.h>
#include <string.h>
static ble_gatt_disc_svc_fn *svc_cb; static ble_gatt_chr_fn *chr_cb; static ble_gatt_dsc_fn *dsc_cb; static ble_gatt_attr_fn *write_cb;
static void *svc_arg,*chr_arg,*dsc_arg,*write_arg; static int fail_disc,fail_write;
static uint16_t dsc_start;
static uint8_t requested_service[16];
int os_mbuf_copydata(const struct os_mbuf *m,int off,uint16_t len,void *out){if(off<0||off+len>m->len)return -1;memcpy(out,m->data+off,len);return 0;}
int ble_uuid_cmp(const ble_uuid_t *a,const ble_uuid_t *b) { if(a->type!=b->type)return 1; return a->type==16 ? memcmp((const char*)a+2,(const char*)b+2,2) : memcmp((const char*)a+1,(const char*)b+1,16); }
int ble_gattc_disc_svc_by_uuid(uint16_t c,const ble_uuid_t *u,ble_gatt_disc_svc_fn *f,void *a){(void)c;memcpy(requested_service,(const char*)u+1,16);svc_cb=f;svc_arg=a;return fail_disc;}
int ble_gattc_disc_all_chrs(uint16_t c,uint16_t s,uint16_t e,ble_gatt_chr_fn *f,void *a){(void)c;assert(s==1&&e==20);chr_cb=f;chr_arg=a;return fail_disc;}
int ble_gattc_disc_all_dscs(uint16_t c,uint16_t s,uint16_t e,ble_gatt_dsc_fn *f,void *a){(void)c;dsc_start=s;assert(e==20);dsc_cb=f;dsc_arg=a;return fail_disc;}
int ble_gattc_write_flat(uint16_t c,uint16_t h,const void *v,uint16_t n,ble_gatt_attr_fn *f,void *a){(void)c;assert(h==6&&n==2&&!memcmp(v,"\1\0",2));if(fail_write)return fail_write;write_cb=f;write_arg=a;return 0;}
int ble_gattc_write_no_rsp_flat(uint16_t c,uint16_t h,const void *v,uint16_t n){(void)c;(void)h;(void)v;(void)n;return 0;}
int ble_gap_connect(uint8_t t,const ble_addr_t *p,uint32_t ms,const struct ble_gap_conn_params *cp,ble_gap_event_fn *f,void *a){(void)t;(void)p;(void)ms;(void)cp;(void)f;(void)a;return 0;}
static void id128(ble_uuid_any_t *u,const uint8_t b[16]){memset(u,0,sizeof(*u));u->u128.u.type=128;memcpy(u->u128.value,b,16);}
static const uint8_t svc_id[16]={1,0,0,0x10,0x15,0x4b,0x10,0x9a,0x3a,0x4f,0xd8,0x82,1,0,0x5a,0x7c};
static const uint8_t up_id[16]={1,0,0,0x10,0x15,0x4b,0x10,0x9a,0x3a,0x4f,0xd8,0x82,2,0,0x5a,0x7c};
static const uint8_t down_id[16]={1,0,0,0x10,0x15,0x4b,0x10,0x9a,0x3a,0x4f,0xd8,0x82,3,0,0x5a,0x7c};
static void start(simstim_nimble_gatt_t *c,uint16_t conn){fail_disc=0;assert(simstim_nimble_discover(conn,c)==0);}
static void service_match(uint16_t conn){struct ble_gatt_svc s={.start_handle=1,.end_handle=20};struct ble_gatt_error ok={0},done={.status=BLE_HS_EDONE};id128(&s.uuid,svc_id);assert(svc_cb(conn,&ok,&s,svc_arg)==0);assert(svc_cb(conn,&done,NULL,svc_arg)==0);}
static void chars(uint16_t conn){struct ble_gatt_error ok={0},done={.status=BLE_HS_EDONE};struct ble_gatt_chr u={.def_handle=2,.val_handle=3,.properties=BLE_GATT_CHR_F_WRITE},d={.def_handle=4,.val_handle=5,.properties=BLE_GATT_CHR_F_NOTIFY};id128(&u.uuid,up_id);id128(&d.uuid,down_id);assert(chr_cb(conn,&ok,&u,chr_arg)==0);assert(chr_cb(conn,&ok,&d,chr_arg)==0);assert(chr_cb(conn,&done,NULL,chr_arg)==0);assert(dsc_start==5);}
int main(void){
 simstim_ble_stream_t stream;simstim_ble_stream_init(&stream);simstim_nimble_gatt_t c;simstim_nimble_gatt_init(&c,&stream);
 start(&c,7);assert(!memcmp(requested_service,svc_id,16));struct ble_gatt_error done={.status=BLE_HS_EDONE};assert(svc_cb(7,&done,NULL,svc_arg)==BLE_HS_ENOENT);assert(!c.ready&&!c.pending);
 start(&c,7);service_match(7);chars(7);struct ble_gatt_error ok={0};struct ble_gatt_dsc d={.handle=6};d.uuid.u16.u.type=16;d.uuid.u16.value=0x2902;assert(dsc_cb(7,&ok,5,&d,dsc_arg)==0);assert(!c.ready&&!write_cb);assert(dsc_cb(7,&done,5,NULL,dsc_arg)==0);assert(!c.ready&&write_cb);assert(write_cb(7,&ok,NULL,write_arg)==0);assert(c.ready&&!c.pending&&c.cccd_handle==6&&c.uplink_handle==3&&c.downlink_handle==5);
 /* Disconnect/reconnect may recycle the connection handle: stale EDONE must not touch new state. */
 start(&c,7);ble_gatt_disc_svc_fn *old_cb=svc_cb;void *old_arg=svc_arg;simstim_nimble_disconnect(&c,7,0x13);start(&c,7);uint32_t generation=c.generation;struct ble_gatt_svc stale={.start_handle=1,.end_handle=20};id128(&stale.uuid,svc_id);assert(old_cb(7,&ok,&stale,old_arg)==0);assert(old_cb(7,&done,NULL,old_arg)==0);assert(c.pending&&!c.ready&&c.generation==generation&&c.service_start==0);
 service_match(7);chars(7);assert(dsc_cb(7,&ok,5,&d,dsc_arg)==0);assert(dsc_cb(7,&done,5,NULL,dsc_arg)==0);assert(write_cb(7,&ok,NULL,write_arg)==0);assert(c.ready);
 return 0;
}
