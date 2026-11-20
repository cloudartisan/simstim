#pragma once
#include <stdint.h>
#include <stddef.h>
typedef struct { uint8_t type; } ble_uuid_t;
typedef struct { ble_uuid_t u; uint16_t value; } ble_uuid16_t;
typedef struct { ble_uuid_t u; uint8_t value[16]; } ble_uuid128_t;
typedef union { ble_uuid_t u; ble_uuid16_t u16; ble_uuid128_t u128; } ble_uuid_any_t;
#define BLE_UUID128_INIT(...) { .u={128}, .value={__VA_ARGS__} }
#define BLE_UUID16_INIT(v) { .u={16}, .value=(v) }
#define BLE_GATT_CHR_F_WRITE 0x08
#define BLE_GATT_CHR_F_WRITE_NO_RSP 0x04
#define BLE_GATT_CHR_F_NOTIFY 0x10
struct ble_gatt_error { uint16_t status, att_handle; };
struct ble_gatt_svc { uint16_t start_handle,end_handle; ble_uuid_any_t uuid; };
struct ble_gatt_chr { uint16_t def_handle,val_handle; uint8_t properties; ble_uuid_any_t uuid; };
struct ble_gatt_dsc { uint16_t handle; ble_uuid_any_t uuid; };
struct ble_gatt_attr { int unused; };
typedef int ble_gatt_attr_fn(uint16_t,const struct ble_gatt_error *,struct ble_gatt_attr *,void *);
typedef int ble_gatt_disc_svc_fn(uint16_t,const struct ble_gatt_error *,const struct ble_gatt_svc *,void *);
typedef int ble_gatt_chr_fn(uint16_t,const struct ble_gatt_error *,const struct ble_gatt_chr *,void *);
typedef int ble_gatt_dsc_fn(uint16_t,const struct ble_gatt_error *,uint16_t,const struct ble_gatt_dsc *,void *);
int ble_uuid_cmp(const ble_uuid_t *,const ble_uuid_t *);
int ble_gattc_disc_svc_by_uuid(uint16_t,const ble_uuid_t *,ble_gatt_disc_svc_fn *,void *);
int ble_gattc_disc_all_chrs(uint16_t,uint16_t,uint16_t,ble_gatt_chr_fn *,void *);
int ble_gattc_disc_all_dscs(uint16_t,uint16_t,uint16_t,ble_gatt_dsc_fn *,void *);
int ble_gattc_write_flat(uint16_t,uint16_t,const void *,uint16_t,ble_gatt_attr_fn *,void *);
int ble_gattc_write_no_rsp_flat(uint16_t,uint16_t,const void *,uint16_t);
