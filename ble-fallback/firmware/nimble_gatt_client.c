#include "nimble_gatt_client.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "os/os_mbuf.h"
#include <stdlib.h>
#include <string.h>

/* BLE_UUID128_INIT uses on-air little-endian byte order. */
static const ble_uuid128_t service_uuid = BLE_UUID128_INIT(0x01,0x00,0x00,0x10,0x15,0x4b,0x10,0x9a,0x3a,0x4f,0xd8,0x82,0x01,0x00,0x5a,0x7c);
static const ble_uuid128_t uplink_uuid = BLE_UUID128_INIT(0x01,0x00,0x00,0x10,0x15,0x4b,0x10,0x9a,0x3a,0x4f,0xd8,0x82,0x02,0x00,0x5a,0x7c);
static const ble_uuid128_t downlink_uuid = BLE_UUID128_INIT(0x01,0x00,0x00,0x10,0x15,0x4b,0x10,0x9a,0x3a,0x4f,0xd8,0x82,0x03,0x00,0x5a,0x7c);
static const ble_uuid16_t cccd_uuid = BLE_UUID16_INIT(0x2902);

typedef struct { simstim_nimble_gatt_t *client; uint32_t generation; } callback_ctx_t;
static callback_ctx_t *ctx_new(simstim_nimble_gatt_t *c) {
    callback_ctx_t *x=malloc(sizeof(*x));
    if (x) { x->client=c; x->generation=c->generation; }
    return x;
}
static bool ctx_current(uint16_t conn, callback_ctx_t *x) {
    return x->client->pending && x->client->conn_handle==conn && x->client->generation==x->generation;
}
static void ctx_free(callback_ctx_t *x) { free(x); }
/* A stale discovery can still deliver multiple item callbacks followed by
 * a terminal callback. Keep its private context until that terminal event. */
static int stale_item(callback_ctx_t *x, const struct ble_gatt_error *e) {
    if (e->status != 0) ctx_free(x);
    return 0;
}
void simstim_nimble_gatt_init(simstim_nimble_gatt_t *c, simstim_ble_stream_t *stream) {
    if (c) { memset(c,0,sizeof(*c)); c->conn_handle=BLE_HS_CONN_HANDLE_NONE; c->stream=stream; }
}
static int on_cccd_write(uint16_t conn,const struct ble_gatt_error *e,struct ble_gatt_attr *a,void *arg) {
    callback_ctx_t *x=arg; simstim_nimble_gatt_t *c=x->client; (void)a;
    if (!ctx_current(conn,x)) { ctx_free(x); return 0; }
    c->pending=false; c->ready=e->status==0; int rc=e->status; ctx_free(x); return rc;
}
static int on_dsc(uint16_t conn,const struct ble_gatt_error *e,uint16_t chr,const struct ble_gatt_dsc *d,void *arg) {
    callback_ctx_t *x=arg; simstim_nimble_gatt_t *c=x->client;
    if (!ctx_current(conn,x)) return stale_item(x,e);
    if (e->status==0 && chr!=c->downlink_handle) { c->pending=false; ctx_free(x); return BLE_HS_EBADDATA; }
    if (e->status==0 && d) {
        if (ble_uuid_cmp(&d->uuid.u,&cccd_uuid.u)==0) {
            if (!d->handle || d->handle<=c->downlink_handle || d->handle>c->service_end || c->cccd_handle) { c->pending=false; ctx_free(x); return BLE_HS_EBADDATA; }
            c->cccd_handle=d->handle;
        }
        return 0;
    }
    if (e->status==BLE_HS_EDONE) {
        if (!c->cccd_handle) { c->pending=false; c->ready=false; ctx_free(x); return BLE_HS_ENOENT; }
        callback_ctx_t *w=ctx_new(c);
        if (!w) { c->pending=false; ctx_free(x); return BLE_HS_ENOMEM; }
        int rc=ble_gattc_write_flat(conn,c->cccd_handle,(const void *)"\1\0",2,on_cccd_write,w);
        if (rc) { c->pending=false; ctx_free(w); }
        ctx_free(x); return rc;
    }
    c->pending=false; int rc=e->status; ctx_free(x); return rc;
}
static int on_chr(uint16_t conn,const struct ble_gatt_error *e,const struct ble_gatt_chr *ch,void *arg) {
    callback_ctx_t *x=arg; simstim_nimble_gatt_t *c=x->client;
    if (!ctx_current(conn,x)) return stale_item(x,e);
    if (e->status==0 && ch) {
        if (!ch->def_handle || !ch->val_handle || ch->def_handle>=ch->val_handle || ch->def_handle<c->service_start || ch->val_handle>c->service_end) { c->pending=false; ctx_free(x); return BLE_HS_EBADDATA; }
        if (ble_uuid_cmp(&ch->uuid.u,&uplink_uuid.u)==0) {
            if (c->uplink_handle || !(ch->properties&BLE_GATT_CHR_F_WRITE)) { c->pending=false; ctx_free(x); return BLE_HS_EBADDATA; }
            c->uplink_handle=ch->val_handle;
        } else if (ble_uuid_cmp(&ch->uuid.u,&downlink_uuid.u)==0) {
            if (c->downlink_handle || !(ch->properties&BLE_GATT_CHR_F_NOTIFY)) { c->pending=false; ctx_free(x); return BLE_HS_EBADDATA; }
            c->downlink_handle=ch->val_handle;
        }
        return 0;
    }
    if (e->status==BLE_HS_EDONE) {
        if (!c->uplink_handle || !c->downlink_handle) { c->pending=false; ctx_free(x); return BLE_HS_ENOENT; }
        /* NimBLE 1.x expects the characteristic value handle as start. */
        callback_ctx_t *d=ctx_new(c); if (!d) { c->pending=false; ctx_free(x); return BLE_HS_ENOMEM; }
        int rc=ble_gattc_disc_all_dscs(conn,c->downlink_handle,c->service_end,on_dsc,d);
        if (rc) { c->pending=false; ctx_free(d); }
        ctx_free(x); return rc;
    }
    c->pending=false; int rc=e->status; ctx_free(x); return rc;
}
static int on_service(uint16_t conn,const struct ble_gatt_error *e,const struct ble_gatt_svc *s,void *arg) {
    callback_ctx_t *x=arg; simstim_nimble_gatt_t *c=x->client;
    if (!ctx_current(conn,x)) return stale_item(x,e);
    if (e->status==0 && s) {
        if (c->service_start || !s->start_handle || s->start_handle>s->end_handle || ble_uuid_cmp(&s->uuid.u,&service_uuid.u)!=0) { c->pending=false; ctx_free(x); return BLE_HS_EBADDATA; }
        c->service_start=s->start_handle; c->service_end=s->end_handle; return 0;
    }
    if (e->status==BLE_HS_EDONE) {
        if (!c->service_start) { c->pending=false; ctx_free(x); return BLE_HS_ENOENT; }
        callback_ctx_t *ch=ctx_new(c); if (!ch) { c->pending=false; ctx_free(x); return BLE_HS_ENOMEM; }
        int rc=ble_gattc_disc_all_chrs(conn,c->service_start,c->service_end,on_chr,ch);
        if (rc) { c->pending=false; ctx_free(ch); }
        ctx_free(x); return rc;
    }
    c->pending=false; int rc=e->status; ctx_free(x); return rc;
}
int simstim_nimble_discover(uint16_t conn,simstim_nimble_gatt_t *c) {
    if (conn==BLE_HS_CONN_HANDLE_NONE || !c || c->pending) return BLE_HS_EINVAL;
    if (c->stream && !c->stream->connected) simstim_ble_stream_begin_generation(c->stream);
    ++c->generation; c->conn_handle=conn; c->pending=true; c->ready=false;
    c->service_start=c->service_end=c->uplink_handle=c->downlink_handle=c->cccd_handle=0;
    callback_ctx_t *x=ctx_new(c); if (!x) { c->pending=false; return BLE_HS_ENOMEM; }
    int rc=ble_gattc_disc_svc_by_uuid(conn,&service_uuid.u,on_service,x);
    if (rc) { c->pending=false; ctx_free(x); if (c->stream) simstim_ble_stream_record_failure(c->stream,SIMSTIM_BLE_CLOSE_RETURN,rc); }
    return rc;
}
void simstim_nimble_disconnect(simstim_nimble_gatt_t *c,uint16_t conn,int32_t gap_reason) {
    if (!c || c->conn_handle!=conn) return;
    c->pending=false; c->ready=false; ++c->generation; c->conn_handle=BLE_HS_CONN_HANDLE_NONE;
    c->service_start=c->service_end=c->uplink_handle=c->downlink_handle=c->cccd_handle=0;
    if (c->stream) simstim_ble_stream_disconnect(c->stream,gap_reason);
}
int simstim_nimble_connect_peer(const ble_addr_t *peer,ble_gap_event_fn *events,void *arg,uint32_t timeout_ms) {
    if (!peer || !events || !timeout_ms) return BLE_HS_EINVAL;
    struct ble_gap_conn_params p={0}; p.scan_itvl=0x10; p.scan_window=0x10; p.itvl_min=12; p.itvl_max=24; p.latency=0; p.supervision_timeout=400;
    return ble_gap_connect(BLE_OWN_ADDR_PUBLIC,peer,timeout_ms,&p,events,arg);
}
int simstim_nimble_subscribe(uint16_t conn,uint16_t cccd,ble_gatt_attr_fn *done,void *arg) {
    const uint8_t value[2]={1,0};
    if (conn==BLE_HS_CONN_HANDLE_NONE || !cccd || !done) return BLE_HS_EINVAL;
    return ble_gattc_write_flat(conn,cccd,value,sizeof(value),done,arg);
}
static int ack_done(uint16_t conn,const struct ble_gatt_error *e,struct ble_gatt_attr *a,void *arg) {
    (void)conn; (void)a; ack_complete(arg,e ? e->status : BLE_HS_EBADDATA); return 0;
}
int simstim_nimble_write_fragment(uint16_t conn,uint16_t handle,const void *frame,size_t len,ack_cell *cell) {
    if (!cell || conn==BLE_HS_CONN_HANDLE_NONE || !handle || !frame || len<5 || len>SIMSTIM_BLE_FRAGMENT_MAX+4) return BLE_HS_EINVAL;
    return ble_gattc_write_flat(conn,handle,frame,(uint16_t)len,ack_done,cell);
}
int simstim_nimble_notification(simstim_ble_stream_t *s,struct os_mbuf *om) {
    uint8_t frame[SIMSTIM_BLE_FRAGMENT_MAX+4];
    if (!s || !om) return BLE_HS_EINVAL;
    uint16_t n=OS_MBUF_PKTLEN(om);
    if (n<5 || n>sizeof(frame)) { ++s->current_diagnostic.fragment_rejections; simstim_ble_stream_record_failure(s,SIMSTIM_BLE_CLOSE_FRAGMENT,BLE_HS_EMSGSIZE); return BLE_HS_EMSGSIZE; }
    if (os_mbuf_copydata(om,0,n,frame)!=0) { ++s->current_diagnostic.fragment_rejections; simstim_ble_stream_record_failure(s,SIMSTIM_BLE_CLOSE_FRAGMENT,BLE_HS_EBADDATA); return BLE_HS_EBADDATA; }
    uint16_t seq=((uint16_t)frame[0]<<8)|frame[1], payload=((uint16_t)frame[2]<<8)|frame[3];
    if (!payload || payload>SIMSTIM_BLE_FRAGMENT_MAX || payload+4!=n || seq!=s->rx_next_sequence) { ++s->current_diagnostic.fragment_rejections; simstim_ble_stream_record_failure(s,SIMSTIM_BLE_CLOSE_FRAGMENT,BLE_HS_EBADDATA); return BLE_HS_EBADDATA; }
    if (simstim_ble_stream_ingress(s,frame+4,payload)!=payload) return BLE_HS_ENOMEM;
    s->rx_next_sequence++; return 0;
}
