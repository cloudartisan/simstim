#pragma once
#include <stdint.h>
struct ble_gap_event;
typedef struct { uint8_t type,val[6]; } ble_addr_t;
typedef int ble_gap_event_fn(struct ble_gap_event *,void *);
struct ble_gap_event;
struct ble_gap_conn_params { uint16_t scan_itvl,scan_window,itvl_min,itvl_max,latency,supervision_timeout,min_ce_len,max_ce_len; };
int ble_gap_connect(uint8_t,const ble_addr_t *,uint32_t,const struct ble_gap_conn_params *,ble_gap_event_fn *,void *);
