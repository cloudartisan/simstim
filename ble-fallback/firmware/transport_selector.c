#include "transport_selector.h"
#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
#define LOCK() portENTER_CRITICAL(&mux)
#define UNLOCK() portEXIT_CRITICAL(&mux)
#else
#include <stdatomic.h>
static atomic_flag mux = ATOMIC_FLAG_INIT;
#define LOCK() while (atomic_flag_test_and_set_explicit(&mux, memory_order_acquire)) {}
#define UNLOCK() atomic_flag_clear_explicit(&mux, memory_order_release)
#endif

void simstim_transport_selector_init(simstim_transport_selector_t *s, bool enabled) {
    if (!s) return;
    s->lock = 0;
    s->owner = SIMSTIM_TRANSPORT_NONE;
    s->enabled = enabled;
    s->wifi_ready = false;
    s->ble_ready = false;
    s->generation = 0;
}

void simstim_transport_selector_set_wifi(simstim_transport_selector_t *s, bool ready) {
    if (!s) return;
    LOCK();
    s->wifi_ready = ready;
    ++s->generation;
    UNLOCK();
}

void simstim_transport_selector_set_ble(simstim_transport_selector_t *s, bool ready) {
    if (!s) return;
    LOCK();
    s->ble_ready = ready;
    ++s->generation;
    UNLOCK();
}

simstim_transport_t simstim_transport_selector_acquire(simstim_transport_selector_t *s) {
    if (!s) return SIMSTIM_TRANSPORT_NONE;
    LOCK();
    if (s->owner == SIMSTIM_TRANSPORT_NONE) {
        if (s->wifi_ready) s->owner = SIMSTIM_TRANSPORT_WIFI;
        else if (s->enabled && s->ble_ready) s->owner = SIMSTIM_TRANSPORT_BLE;
    }
    simstim_transport_t result = s->owner;
    UNLOCK();
    return result;
}

void simstim_transport_selector_release(simstim_transport_selector_t *s, simstim_transport_t owner) {
    if (!s) return;
    LOCK();
    if (s->owner == owner && owner != SIMSTIM_TRANSPORT_NONE)
        s->owner = SIMSTIM_TRANSPORT_NONE;
    UNLOCK();
}

simstim_transport_t simstim_transport_selector_owner(simstim_transport_selector_t *s) {
    if (!s) return SIMSTIM_TRANSPORT_NONE;
    LOCK();
    simstim_transport_t result = s->owner;
    UNLOCK();
    return result;
}
