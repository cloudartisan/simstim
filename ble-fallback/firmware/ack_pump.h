#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <limits.h>
/* Only the network task owns pump state. Each accepted ATT write owns a
 * separate completion cell: a stale callback can never mutate another one. */
typedef struct { unsigned refs; int result; } ack_cell;
typedef struct { ack_cell *active; uint32_t started; bool failed; int reason; } ack_pump;
typedef int (*ack_submit)(void *, const void *, size_t, ack_cell *);
static inline void ack_drop(ack_cell *c) { if(c && __atomic_sub_fetch(&c->refs,1,__ATOMIC_ACQ_REL)==0) free(c); }
static inline void ack_complete(ack_cell *c,int rc) { __atomic_store_n(&c->result,rc,__ATOMIC_RELEASE); ack_drop(c); }
static inline void ack_reset(ack_pump *p) { ack_drop(p->active); *p=(ack_pump){0}; }
/* Caller retains identical staged bytes on 0, releases only on 1, closes
 * generation on -1. Nonzero immediate submit returns own no callback. */
static inline int ack_step(ack_pump *p, uint32_t now, uint32_t max_ticks, const void *bytes, size_t n, ack_submit submit, void *arg, int busy, int nomem, int timeout) {
 if(p->failed) return -1;
 if(!p->active){
  ack_cell *c=malloc(sizeof(*c)); if(!c) return 0;
  c->refs=2; c->result=INT_MIN; p->active=c; p->started=now;
  int rc=submit(arg,bytes,n,c);
  if(rc){p->active=NULL;ack_drop(c);ack_drop(c);if(rc==busy||rc==nomem)return 0;p->failed=true;p->reason=rc;return -1;}
 }
 int rc=__atomic_load_n(&p->active->result,__ATOMIC_ACQUIRE);
 if(rc==INT_MIN){if((uint32_t)(now-p->started)<max_ticks)return 0;rc=timeout;}
 ack_drop(p->active);p->active=NULL;
 if(rc){p->failed=true;p->reason=rc;return -1;}return 1;
}
