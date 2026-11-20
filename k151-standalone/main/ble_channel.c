#include "ble_channel.h"
#include "simstim_public_config.h"

#include <limits.h>
#include <string.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/ctr_drbg.h"
#include "mbedtls/entropy.h"
#include "mbedtls/net_sockets.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/ssl.h"
#include "mbedtls/ssl_ciphersuites.h"

#define BLE_TLS_IDENTITY SIMSTIM_LINK_IDENTITY
#define BLE_TLS_PSK_LEN 32U
#define BLE_TLS_HANDSHAKE_DEFAULT_MS 15000U
#define BLE_TLS_CLOSE_MAX_MS 100U

static mbedtls_ssl_context ssl;
static mbedtls_ssl_config conf;
static mbedtls_ctr_drbg_context drbg;
static mbedtls_entropy_context entropy;
static bool contexts_initialized;
static bool channel_open;
static bool radio_open;
static bool channel_failed;

static int64_t deadline_after_ms(unsigned timeout_ms)
{
    return esp_timer_get_time() + (int64_t)timeout_ms * 1000;
}

static bool deadline_expired(int64_t deadline)
{
    return esp_timer_get_time() >= deadline;
}

static void brief_delay(void)
{
    vTaskDelay(1);
}

static int bio_send(void *ctx, const unsigned char *src, size_t len)
{
    (void)ctx;
    if (!radio_open || src == NULL || len == 0) {
        return MBEDTLS_ERR_NET_SEND_FAILED;
    }
    int n = ble_radio_send(NULL, src, len);
    if (n > 0 && (size_t)n <= len) {
        return n;
    }
    if (n == 0) {
        return MBEDTLS_ERR_SSL_WANT_WRITE;
    }
    return MBEDTLS_ERR_NET_SEND_FAILED;
}

static int bio_recv(void *ctx, unsigned char *dst, size_t len)
{
    (void)ctx;
    if (!radio_open || dst == NULL || len == 0) {
        return MBEDTLS_ERR_NET_RECV_FAILED;
    }
    int n = ble_radio_recv(NULL, dst, len);
    if (n > 0 && (size_t)n <= len) {
        return n;
    }
    if (n == 0) {
        return MBEDTLS_ERR_SSL_WANT_READ;
    }
    return MBEDTLS_ERR_NET_RECV_FAILED;
}

static void free_contexts(void)
{
    if (contexts_initialized) {
        mbedtls_ssl_free(&ssl);
        mbedtls_ssl_config_free(&conf);
        mbedtls_ctr_drbg_free(&drbg);
        mbedtls_entropy_free(&entropy);
        contexts_initialized = false;
    }
}

static void stop_radio(void)
{
    if (radio_open) {
        ble_radio_close();
        radio_open = false;
    }
}

static void mark_failed(void)
{
    channel_failed = true;
    channel_open = false;
}

bool ble_channel_open(const uint8_t key[32], unsigned timeout_ms)
{
    static const unsigned char personal[] = "simstim-k151";
    static const int ciphersuites[] = {
        MBEDTLS_TLS_PSK_WITH_AES_128_GCM_SHA256,
        0
    };
    static const unsigned char identity[] = BLE_TLS_IDENTITY;
    int ret;
    bool ok = false;
    int64_t deadline;

    if (key == NULL) {
        return false;
    }
    ble_channel_close();
    if (timeout_ms == 0) {
        timeout_ms = BLE_TLS_HANDSHAKE_DEFAULT_MS;
    }
    deadline = deadline_after_ms(timeout_ms);

    mbedtls_ssl_init(&ssl);
    mbedtls_ssl_config_init(&conf);
    mbedtls_ctr_drbg_init(&drbg);
    mbedtls_entropy_init(&entropy);
    contexts_initialized = true;

    ret = mbedtls_ctr_drbg_seed(&drbg, mbedtls_entropy_func, &entropy,
                                personal, sizeof(personal) - 1U);
    if (ret != 0) {
        goto cleanup;
    }
    ret = mbedtls_ssl_config_defaults(&conf, MBEDTLS_SSL_IS_CLIENT,
                                      MBEDTLS_SSL_TRANSPORT_STREAM,
                                      MBEDTLS_SSL_PRESET_DEFAULT);
    if (ret != 0) {
        goto cleanup;
    }
    mbedtls_ssl_conf_min_tls_version(&conf, MBEDTLS_SSL_VERSION_TLS1_2);
    mbedtls_ssl_conf_max_tls_version(&conf, MBEDTLS_SSL_VERSION_TLS1_2);
    mbedtls_ssl_conf_ciphersuites(&conf, ciphersuites);
    mbedtls_ssl_conf_rng(&conf, mbedtls_ctr_drbg_random, &drbg);
    ret = mbedtls_ssl_conf_psk(&conf, key, BLE_TLS_PSK_LEN,
                               identity, sizeof(identity) - 1U);
    if (ret != 0) {
        goto cleanup;
    }
    ret = mbedtls_ssl_setup(&ssl, &conf);
    if (ret != 0) {
        goto cleanup;
    }

    if (deadline_expired(deadline)) {
        goto cleanup;
    }
    unsigned remaining_ms = (unsigned)((deadline - esp_timer_get_time() + 999) / 1000);
    if (!ble_radio_open(remaining_ms)) {
        goto cleanup;
    }
    radio_open = true;
    mbedtls_ssl_set_bio(&ssl, NULL, bio_send, bio_recv, NULL);

    while (!deadline_expired(deadline)) {
        ret = mbedtls_ssl_handshake(&ssl);
        if (ret == 0) {
            channel_open = true;
            channel_failed = false;
            ok = true;
            goto cleanup;
        }
        if (ret != MBEDTLS_ERR_SSL_WANT_READ &&
            ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
            break;
        }
        brief_delay();
    }

cleanup:
    if (!ok) {
        if (ret != 0) ble_radio_record_tls_failure(ret);
        stop_radio();
        free_contexts();
        channel_open = false;
        channel_failed = false;
    }
    return ok;
}

int ble_channel_read(uint8_t *dst, size_t cap)
{
    if (!channel_open || channel_failed || dst == NULL || cap == 0) {
        return channel_failed ? -1 : 0;
    }
    if (cap > INT_MAX) {
        cap = INT_MAX;
    }
    int ret = mbedtls_ssl_read(&ssl, dst, cap);
    if (ret > 0 && (size_t)ret <= cap) {
        return ret;
    }
    if (ret == MBEDTLS_ERR_SSL_WANT_READ || ret == MBEDTLS_ERR_SSL_WANT_WRITE) {
        return 0;
    }
    ble_radio_record_tls_failure(ret);
    mark_failed();
    return -1;
}

int ble_channel_write(const uint8_t *src, size_t len)
{
    if (!channel_open || channel_failed || src == NULL || len == 0) {
        return channel_failed ? -1 : 0;
    }
    if (len > INT_MAX) {
        len = INT_MAX;
    }
    int ret = mbedtls_ssl_write(&ssl, src, len);
    if (ret > 0 && (size_t)ret <= len) {
        return ret;
    }
    if (ret == MBEDTLS_ERR_SSL_WANT_READ || ret == MBEDTLS_ERR_SSL_WANT_WRITE) {
        return 0;
    }
    ble_radio_record_tls_failure(ret);
    mark_failed();
    return -1;
}

void ble_channel_close(void)
{
    if (contexts_initialized && radio_open && channel_open && !channel_failed) {
        int64_t deadline = deadline_after_ms(BLE_TLS_CLOSE_MAX_MS);
        while (!deadline_expired(deadline)) {
            int ret = mbedtls_ssl_close_notify(&ssl);
            if (ret == 0) {
                break;
            }
            if (ret != MBEDTLS_ERR_SSL_WANT_READ &&
                ret != MBEDTLS_ERR_SSL_WANT_WRITE) {
                break;
            }
            brief_delay();
        }
    }
    channel_open = false;
    channel_failed = false;
    stop_radio();
    free_contexts();
}
