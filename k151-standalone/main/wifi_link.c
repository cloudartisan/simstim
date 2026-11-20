#include "wifi_link.h"
#include "ble_channel.h"
#include "simstim_public_config.h"

#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_tls.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mbedtls/ssl.h"
#include "mbedtls/platform_util.h"

#define LINK_HOST           SIMSTIM_LINK_HOST
#define LINK_PORT           SIMSTIM_LINK_PORT
#define LINK_IDENTITY       SIMSTIM_LINK_IDENTITY
#define LINK_STREAM_CAPACITY (16 * 1024)
#define LINK_MAX_WRITE      512
#define LINK_POLL_MS        20
#define LINK_TASK_STACK     8192
#define LINK_TASK_PRIORITY  5
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_DISCONNECTED_BIT BIT1

/* Single producer/consumer byte rings. The generation is changed and both
 * rings are reset atomically, so no queued bytes can cross a TLS reconnect. */
typedef struct {
    uint8_t bytes[LINK_STREAM_CAPACITY];
    size_t head;
    size_t used;
} byte_stream_t;

#include "esp_attr.h"
static EXT_RAM_BSS_ATTR byte_stream_t tx_stream;
static EXT_RAM_BSS_ATTR byte_stream_t rx_stream;
static EventGroupHandle_t wifi_events;
static esp_netif_t *sta_netif;
static TaskHandle_t link_task_handle;
static portMUX_TYPE state_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t current_generation;
static bool session_is_live;
static bool start_requested;
static bool wifi_initialized;
static esp_tls_t *tls;

static size_t stream_free(const byte_stream_t *stream)
{
    return LINK_STREAM_CAPACITY - stream->used;
}

static void stream_reset(byte_stream_t *stream)
{
    stream->head = 0;
    stream->used = 0;
}

static size_t stream_put(byte_stream_t *stream, const uint8_t *src, size_t len)
{
    size_t count = len < stream_free(stream) ? len : stream_free(stream);
    size_t tail = (stream->head + stream->used) % LINK_STREAM_CAPACITY;
    size_t first = count < LINK_STREAM_CAPACITY - tail ? count : LINK_STREAM_CAPACITY - tail;
    memcpy(stream->bytes + tail, src, first);
    memcpy(stream->bytes, src + first, count - first);
    stream->used += count;
    return count;
}

static size_t stream_get(byte_stream_t *stream, uint8_t *dst, size_t len)
{
    size_t count = len < stream->used ? len : stream->used;
    size_t first = count < LINK_STREAM_CAPACITY - stream->head ? count : LINK_STREAM_CAPACITY - stream->head;
    memcpy(dst, stream->bytes + stream->head, first);
    memcpy(dst + first, stream->bytes, count - first);
    stream->head = (stream->head + count) % LINK_STREAM_CAPACITY;
    stream->used -= count;
    return count;
}

static void set_session(uint32_t generation, bool live)
{
    taskENTER_CRITICAL(&state_lock);
    current_generation = generation;
    session_is_live = live;
    taskEXIT_CRITICAL(&state_lock);
}

static bool session_matches(uint32_t generation)
{
    bool matches;
    taskENTER_CRITICAL(&state_lock);
    matches = generation != 0 && session_is_live &&
              current_generation == generation;
    taskEXIT_CRITICAL(&state_lock);
    return matches;
}

static uint32_t next_generation(void)
{
    uint32_t generation;
    taskENTER_CRITICAL(&state_lock);
    generation = current_generation + 1;
    if (generation == 0) generation = 1;
    current_generation = generation;
    session_is_live = false;
    stream_reset(&tx_stream);
    stream_reset(&rx_stream);
    taskEXIT_CRITICAL(&state_lock);
    return generation;
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (wifi_events) {
            xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT);
            xEventGroupSetBits(wifi_events, WIFI_DISCONNECTED_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        if (wifi_events) {
            xEventGroupClearBits(wifi_events, WIFI_DISCONNECTED_BIT);
            xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
        }
    }
}

static bool wifi_setup(const simstim_wifi_creds *creds)
{
    /* Never allow IDF's Wi-Fi diagnostics to print credentials/SSID data. */
    esp_log_level_set("wifi", ESP_LOG_NONE);
    esp_log_level_set("wpa", ESP_LOG_NONE);
    esp_log_level_set("wpa_supplicant", ESP_LOG_NONE);

    esp_err_t err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return false;
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return false;

    sta_netif = esp_netif_create_default_wifi_sta();
    if (!sta_netif) return false;
    if (esp_netif_set_hostname(sta_netif, SIMSTIM_DEVICE_HOSTNAME) != ESP_OK) return false;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    if (esp_wifi_init(&init) != ESP_OK) return false;
    wifi_initialized = true;

    if (esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                   wifi_event_handler, NULL) != ESP_OK ||
        esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                   wifi_event_handler, NULL) != ESP_OK) {
        return false;
    }

    wifi_config_t config = {0};
    size_t ssid_len = strnlen(creds->ssid, sizeof(creds->ssid));
    size_t pass_len = strnlen(creds->pass, sizeof(creds->pass));
    if (ssid_len == 0 || ssid_len > sizeof(config.sta.ssid) ||
        pass_len > sizeof(config.sta.password)) return false;
    memcpy(config.sta.ssid, creds->ssid, ssid_len);
    memcpy(config.sta.password, creds->pass, pass_len);
    config.sta.threshold.authmode = pass_len ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    config.sta.pmf_cfg.capable = true;
    config.sta.pmf_cfg.required = false;

    if (esp_wifi_set_storage(WIFI_STORAGE_RAM) != ESP_OK ||
        esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK ||
        esp_wifi_set_config(WIFI_IF_STA, &config) != ESP_OK ||
        esp_wifi_start() != ESP_OK) return false;
    return true;
}

static int connect_tls(const uint8_t key[32])
{
#if defined(CONFIG_ESP_TLS_PSK_VERIFICATION)
    static const int suites[] = { MBEDTLS_TLS_PSK_WITH_AES_128_GCM_SHA256, 0 };
    psk_hint_key_t psk = {
        .key = key,
        .key_size = 32,
        .hint = LINK_IDENTITY,
    };
    esp_tls_cfg_t config = {
        .psk_hint_key = &psk,
        .timeout_ms = 5000,
        .tls_version = ESP_TLS_VER_TLS_1_2,
        .ciphersuites_list = suites,
    };
    tls = esp_tls_init();
    if (!tls) return -1;
    int result = esp_tls_conn_new_sync(LINK_HOST, (int)strlen(LINK_HOST),
                                       LINK_PORT, &config, tls);
    if (result != 1) {
        esp_tls_conn_destroy(tls);
        tls = NULL;
        return -1;
    }
    return 0;
#else
    (void)key;
    return -1;
#endif
}

static bool tls_set_nonblocking(int *sockfd)
{
    if (!tls || esp_tls_get_conn_sockfd(tls, sockfd) != ESP_OK || *sockfd < 0) return false;
    int flags = fcntl(*sockfd, F_GETFL, 0);
    return flags >= 0 && fcntl(*sockfd, F_SETFL, flags | O_NONBLOCK) == 0;
}

static void close_tls(void)
{
    if (tls) {
        esp_tls_conn_destroy(tls);
        tls = NULL;
    }
}

static bool socket_alive(void)
{
    return wifi_events &&
           (xEventGroupGetBits(wifi_events) & WIFI_CONNECTED_BIT) != 0;
}

/* Same application rings, exactly one active TLS owner. No USB fallback for
 * a transaction that started on either radio; reset generation on every loss. */
static void try_ble(const uint8_t key[32])
{
#if CONFIG_BT_NIMBLE_ENABLED
    if (!ble_channel_open(key, 15000)) return;
    uint32_t gen=next_generation(); set_session(gen,true);
    uint8_t in[512], out[512]; size_t have=0, off=0;
    TickType_t last_tx=xTaskGetTickCount();
    while (session_matches(gen)) {
        bool progress=false;
        taskENTER_CRITICAL(&state_lock);
        size_t space=stream_free(&rx_stream);
        if(off==have){ have=stream_get(&tx_stream,out,sizeof(out)); off=0; last_tx=xTaskGetTickCount(); }
        taskEXIT_CRITICAL(&state_lock);
        if (space) {
            int n=ble_channel_read(in,space<sizeof(in)?space:sizeof(in));
            if(n<0 || (size_t)n>space || (size_t)n>sizeof(in))break;
            if(n){ taskENTER_CRITICAL(&state_lock);stream_put(&rx_stream,in,n);taskEXIT_CRITICAL(&state_lock);progress=true; }
        }
        if (off<have) {
            int n=ble_channel_write(out+off,have-off);
            if(n<0 || (size_t)n>have-off)break;
            if(n){off+=n;last_tx=xTaskGetTickCount();progress=true;}
            else if(xTaskGetTickCount()-last_tx>pdMS_TO_TICKS(20000))break;
        }
        if(!progress)vTaskDelay(1);
    }
    set_session(gen,false); ble_channel_close();
#else
    (void)key;
#endif
}

static void network_task(void *arg)
{
    (void)arg;
    simstim_wifi_creds creds = {0};
    uint8_t psk[32] = {0};

    wifi_events = xEventGroupCreate();
    if (!wifi_events || !simstim_wifi_load(&creds)) {
        goto task_exit;
    }
    memcpy(psk, creds.key, sizeof(psk));
    if (!wifi_setup(&creds)) goto task_exit;
    mbedtls_platform_zeroize(&creds, sizeof(creds));

    uint32_t backoff_ms = 1000;
    for (;;) {
        if (!socket_alive()) {
            if (esp_wifi_connect() != ESP_OK) {
                try_ble(psk);
                vTaskDelay(pdMS_TO_TICKS(backoff_ms));
                if (backoff_ms < 30000) backoff_ms *= 2;
                continue;
            }
            EventBits_t bits = xEventGroupWaitBits(wifi_events,
                WIFI_CONNECTED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
            if (!(bits & WIFI_CONNECTED_BIT)) {
                try_ble(psk);
                vTaskDelay(pdMS_TO_TICKS(backoff_ms));
                if (backoff_ms < 30000) backoff_ms *= 2;
                continue;
            }
        }

        uint32_t generation = next_generation();
        if (connect_tls(psk) != 0) {
            set_session(generation, false);
            try_ble(psk);
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            if (backoff_ms < 30000) backoff_ms *= 2;
            continue;
        }
        int sockfd = -1;
        if (!tls_set_nonblocking(&sockfd)) {
            set_session(generation, false);
            close_tls();
            continue;
        }
        set_session(generation, true);
        backoff_ms = 1000;

        bool connected = true;
        uint8_t tx_chunk[LINK_MAX_WRITE];
        size_t tx_len = 0, tx_off = 0;
        bool write_wants_read = false;
        bool read_wants_write = false;
        while (connected && socket_alive() && session_matches(generation)) {
            size_t rx_space;
            taskENTER_CRITICAL(&state_lock);
            rx_space = stream_free(&rx_stream);
            taskEXIT_CRITICAL(&state_lock);

            if (tx_off == tx_len) {
                taskENTER_CRITICAL(&state_lock);
                tx_len = stream_get(&tx_stream, tx_chunk, sizeof(tx_chunk));
                taskEXIT_CRITICAL(&state_lock);
                tx_off = 0;
            }

            ssize_t buffered = esp_tls_get_bytes_avail(tls);
            if (buffered > 0 && rx_space > 0 && !read_wants_write) {
                uint8_t incoming[512];
                size_t want = rx_space < sizeof(incoming) ? rx_space : sizeof(incoming);
                ssize_t got = esp_tls_conn_read(tls, incoming, want);
                if (got > 0) {
                    taskENTER_CRITICAL(&state_lock);
                    if (session_is_live && current_generation == generation)
                        (void)stream_put(&rx_stream, incoming, (size_t)got);
                    taskEXIT_CRITICAL(&state_lock);
                    read_wants_write = false;
                    continue;
                }
                if (got == 0) { connected = false; break; }
                if (got == ESP_TLS_ERR_SSL_WANT_WRITE) read_wants_write = true;
                else if (got != ESP_TLS_ERR_SSL_WANT_READ) {
                    connected = false; break;
                }
            }

            fd_set read_set, write_set;
            FD_ZERO(&read_set); FD_ZERO(&write_set);
            if ((rx_space > 0 && !read_wants_write) || write_wants_read)
                FD_SET(sockfd, &read_set);
            if ((tx_len > tx_off && !write_wants_read) || read_wants_write)
                FD_SET(sockfd, &write_set);
            struct timeval timeout = { .tv_sec = 0, .tv_usec = LINK_POLL_MS * 1000 };
            int ready = select(sockfd + 1, &read_set, &write_set, NULL, &timeout);
            if (ready < 0) {
                if (errno == EINTR) continue;
                connected = false; break;
            }
            bool readable = ready > 0 && FD_ISSET(sockfd, &read_set);
            bool writable = ready > 0 && FD_ISSET(sockfd, &write_set);

            if (((readable && !read_wants_write) || (read_wants_write && writable)) && rx_space > 0) {
                uint8_t incoming[512];
                size_t want = rx_space < sizeof(incoming) ? rx_space : sizeof(incoming);
                ssize_t got = esp_tls_conn_read(tls, incoming, want);
                if (got > 0) {
                    taskENTER_CRITICAL(&state_lock);
                    if (session_is_live && current_generation == generation)
                        (void)stream_put(&rx_stream, incoming, (size_t)got);
                    taskEXIT_CRITICAL(&state_lock);
                    read_wants_write = false;
                } else if (got == 0) { connected = false; break; }
                else if (got == ESP_TLS_ERR_SSL_WANT_WRITE) read_wants_write = true;
                else if (got != ESP_TLS_ERR_SSL_WANT_READ) {
                    connected = false; break;
                }
            }

            if (tx_off < tx_len && (write_wants_read ? readable : writable)) {
                ssize_t sent = esp_tls_conn_write(tls, tx_chunk + tx_off, tx_len - tx_off);
                if (sent > 0) {
                    tx_off += (size_t)sent;
                    write_wants_read = false;
                } else if (sent == ESP_TLS_ERR_SSL_WANT_READ) {
                    write_wants_read = true;
                } else if (sent == ESP_TLS_ERR_SSL_WANT_WRITE) {
                    write_wants_read = false;
                } else { connected = false; break; }
            }
        }

        set_session(generation, false);
        close_tls();
        taskENTER_CRITICAL(&state_lock);
        stream_reset(&tx_stream);
        stream_reset(&rx_stream);
        taskEXIT_CRITICAL(&state_lock);
        if (backoff_ms < 30000) {
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            backoff_ms *= 2;
            if (backoff_ms > 30000) backoff_ms = 30000;
        }
    }

task_exit:
    mbedtls_platform_zeroize(&creds, sizeof(creds));
    mbedtls_platform_zeroize(psk, sizeof(psk));
    if (wifi_initialized) {
        esp_wifi_stop();
        esp_wifi_deinit();
        wifi_initialized = false;
    }
    link_task_handle = NULL;
    vTaskDelete(NULL);
}

void wifi_link_start(void)
{
    taskENTER_CRITICAL(&state_lock);
    if (start_requested) {
        taskEXIT_CRITICAL(&state_lock);
        return;
    }
    start_requested = true;
    taskEXIT_CRITICAL(&state_lock);

    if (xTaskCreate(network_task, "wifi_link", LINK_TASK_STACK, NULL,
                    LINK_TASK_PRIORITY, &link_task_handle) != pdPASS) {
        taskENTER_CRITICAL(&state_lock);
        start_requested = false;
        taskEXIT_CRITICAL(&state_lock);
    }
}

uint32_t wifi_link_session(void)
{
    uint32_t generation;
    taskENTER_CRITICAL(&state_lock);
    generation = session_is_live ? current_generation : 0;
    taskEXIT_CRITICAL(&state_lock);
    return generation;
}

int wifi_link_read(uint32_t session, void *buf, size_t len)
{
    if (!buf || len == 0) return -1;
    if (len > LINK_MAX_WRITE) len = LINK_MAX_WRITE;
    taskENTER_CRITICAL(&state_lock);
    if (session == 0 || !session_is_live || current_generation != session) {
        taskEXIT_CRITICAL(&state_lock);
        return -1;
    }
    size_t count = stream_get(&rx_stream, buf, len);
    taskEXIT_CRITICAL(&state_lock);
    return (int)count;
}

bool wifi_link_write(uint32_t session, const void *buf, size_t len)
{
    if (!buf || len == 0 || len > LINK_MAX_WRITE) return false;
    const uint8_t *bytes = buf;
    for (;;) {
        taskENTER_CRITICAL(&state_lock);
        bool live = session != 0 && session_is_live && current_generation == session;
        if (live && stream_free(&tx_stream) >= len) {
            size_t accepted = stream_put(&tx_stream, bytes, len);
            taskEXIT_CRITICAL(&state_lock);
            return accepted == len;
        }
        taskEXIT_CRITICAL(&state_lock);
        if (!live) return false;
        /* Bounded memory, lossless backpressure while the same session lives. */
        vTaskDelay(1);
    }
}
