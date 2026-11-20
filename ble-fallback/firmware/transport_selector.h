#pragma once
#include <stdbool.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef enum { SIMSTIM_TRANSPORT_NONE=0, SIMSTIM_TRANSPORT_WIFI=1, SIMSTIM_TRANSPORT_BLE=2 } simstim_transport_t;
typedef struct {
    volatile uint32_t lock;
    simstim_transport_t owner;
    bool enabled;
    bool wifi_ready;
    bool ble_ready;
    uint32_t generation;
} simstim_transport_selector_t;
void simstim_transport_selector_init(simstim_transport_selector_t *s, bool ble_enabled);
void simstim_transport_selector_set_wifi(simstim_transport_selector_t *s, bool ready);
void simstim_transport_selector_set_ble(simstim_transport_selector_t *s, bool ready);
/* Wi-Fi is preferred when unowned; BLE is fallback only while Wi-Fi is
 * unavailable. Active owners are never preempted, preventing duplicate
 * command sessions across radios. */
simstim_transport_t simstim_transport_selector_acquire(simstim_transport_selector_t *s);
void simstim_transport_selector_release(simstim_transport_selector_t *s, simstim_transport_t owner);
simstim_transport_t simstim_transport_selector_owner(simstim_transport_selector_t *s);
#ifdef __cplusplus
}
#endif
