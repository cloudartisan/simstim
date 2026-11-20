/* M5Stack StackChan/K151 CoreS3 GC0308 camera survey.
 * Board pinout and reset sequence follow M5Stack's CoreS3/StackChan docs:
 * DVP D0..7=39,40,41,42,15,16,48,47; VSYNC=46, HREF=38, PCLK=45,
 * XCLK=GPIO2 at 20MHz; SCCB shares the already initialized GPIO12/11 I2C bus.
 * AW9523 (0x58) P1_0 drives the active-low GC0308 CAM_RST.
 */
#include <stdbool.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "wire_io.h"
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/poll.h>
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_err.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_video_init.h"
#include "esp_video_device.h"
#include "esp_video_ioctl.h"
#include "linux/videodev2.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define CAM_D0 GPIO_NUM_39
#define CAM_D1 GPIO_NUM_40
#define CAM_D2 GPIO_NUM_41
#define CAM_D3 GPIO_NUM_42
#define CAM_D4 GPIO_NUM_15
#define CAM_D5 GPIO_NUM_16
#define CAM_D6 GPIO_NUM_48
#define CAM_D7 GPIO_NUM_47
#define CAM_VSYNC GPIO_NUM_46
#define CAM_HREF GPIO_NUM_38
#define CAM_PCLK GPIO_NUM_45
#define CAM_XCLK GPIO_NUM_NC /* CoreS3 has an external clock, not an MCU XCLK output. */
#define CAM_RESET GPIO_NUM_NC
#define CAM_PWDN GPIO_NUM_NC
#define CAM_XCLK_HZ 20000000u
#define CAM_WIDTH 320u
#define CAM_HEIGHT 240u
#define CAM_RAW_BYTES (CAM_WIDTH * CAM_HEIGHT * 2u)
#define CAM_DEVICE ESP_VIDEO_DVP_DEVICE_NAME
#define CAM_TIMEOUT_MS 1500
#define SURVEY_MAX_FRAMES 3u
#define SURVEY_MAX_TOTAL_BYTES (SURVEY_MAX_FRAMES * CAM_RAW_BYTES)
#define CAM_TAG "look_camera"
#define BASE64_CHUNK_BYTES 54u

/* One bounded, allowlisted receipt per failed stage. No image data, path, or
 * device/host identity is included; wire_printf follows the active transport. */
static void camera_failure(const char *stage, esp_err_t err, int saved_errno)
{
    const size_t dma_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    const size_t dma_largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    wire_printf("LOOK_DIAG stage=%s esp=%d errno=%d dma_free=%u dma_largest=%u\n",
                stage, (int)err, saved_errno, (unsigned)dma_free, (unsigned)dma_largest);
}

#define CAM_FAIL(stage, err) do { camera_failure(stage, err, errno); goto fail; } while (0)

static const char s_base64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

typedef struct { unsigned index; uint8_t *data; size_t size; } retained_frame_t;
typedef struct {
    int fd;
    void *mapping;
    size_t mapping_len;
    struct v4l2_requestbuffers req;
    struct v4l2_buffer buffer;
    bool initialized;
    bool streaming;
} camera_session_t;

static camera_session_t survey_session;
static retained_frame_t s_frames[SURVEY_MAX_FRAMES];
static unsigned s_frame_count;
static bool s_survey_active;
static bool s_survey_ok;

static esp_err_t xioctl(int fd, unsigned long request, void *arg)
{
    int rc;
    do { errno = 0; rc = ioctl(fd, request, arg); } while (rc < 0 && errno == EINTR);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static bool aw9523_camera_reset(i2c_master_bus_handle_t bus)
{
    i2c_device_config_t cfg = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = 0x58, .scl_speed_hz = 100000 };
    i2c_master_dev_handle_t aw = NULL;
    if (i2c_master_bus_add_device(bus, &cfg, &aw) != ESP_OK) return false;
    uint8_t reg = 0x03, p1 = 0xff;
    bool ok = i2c_master_transmit_receive(aw, &reg, 1, &p1, 1, 100) == ESP_OK;
    /* P1_0 is CAM_RST, active low. Assert 50ms, then release 50ms. */
    const uint8_t assert_reset[] = {0x03, (uint8_t)(p1 & ~1u)};
    ok = ok && i2c_master_transmit(aw, assert_reset, sizeof(assert_reset), 100) == ESP_OK;
    if (ok) vTaskDelay(pdMS_TO_TICKS(50));
    const uint8_t release_reset[] = {0x03, (uint8_t)(p1 | 1u)};
    ok = ok && i2c_master_transmit(aw, release_reset, sizeof(release_reset), 100) == ESP_OK;
    if (ok) vTaskDelay(pdMS_TO_TICKS(50));
    i2c_master_bus_rm_device(aw);
    return ok;
}

static esp_err_t setup_camera(i2c_master_bus_handle_t bus)
{
    static const esp_cam_ctlr_dvp_pin_config_t pins = {
        .data_width = CAM_CTLR_DATA_WIDTH_8,
        .data_io = { CAM_D0,CAM_D1,CAM_D2,CAM_D3,CAM_D4,CAM_D5,CAM_D6,CAM_D7 },
        .vsync_io = CAM_VSYNC, .de_io = CAM_HREF, .pclk_io = CAM_PCLK,
        .xclk_io = CAM_XCLK,
    };
    const esp_video_init_sccb_config_t sccb = { .init_sccb = false,
        .i2c_handle = bus, .freq = 100000 };
    const esp_video_init_dvp_config_t dvp = { .sccb_config = sccb,
        .reset_pin = CAM_RESET, .pwdn_pin = CAM_PWDN, .dvp_pin = pins,
        .xclk_freq = CAM_XCLK_HZ };
    const esp_video_init_config_t config = { .dvp = &dvp };
    esp_err_t err = esp_video_init(&config);
    return err;
}

static void session_close(camera_session_t *s)
{
    if (s->fd >= 0 && s->streaming) {
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        (void)ioctl(s->fd, VIDIOC_STREAMOFF, &type);
        s->streaming = false;
    }
    if (s->mapping && s->mapping != MAP_FAILED) {
        munmap(s->mapping, s->mapping_len); s->mapping = MAP_FAILED;
    }
    if (s->fd >= 0 && s->req.count) {
        s->req.count = 0; (void)ioctl(s->fd, VIDIOC_REQBUFS, &s->req);
    }
    if (s->fd >= 0) { close(s->fd); s->fd = -1; }
    if (s->initialized) { (void)esp_video_deinit(); s->initialized = false; }
}

static bool session_open(camera_session_t *s, i2c_master_bus_handle_t bus)
{
    memset(s, 0, sizeof(*s)); s->fd = -1; s->mapping = MAP_FAILED;
    errno = 0;
    if (!aw9523_camera_reset(bus)) {
        camera_failure("sensor_reset", ESP_FAIL, 0);
        return false;
    }
    esp_err_t init_err = setup_camera(bus);
    if (init_err != ESP_OK) {
        camera_failure("video_init", init_err, 0);
        return false;
    }
    s->initialized = true;
    s->fd = open(CAM_DEVICE, O_RDWR);
    if (s->fd < 0) CAM_FAIL("device_open", ESP_FAIL);
    struct timeval timeout = {.tv_sec=CAM_TIMEOUT_MS/1000,.tv_usec=(CAM_TIMEOUT_MS%1000)*1000};
    if (xioctl(s->fd,VIDIOC_S_DQBUF_TIMEOUT,&timeout)!=ESP_OK) CAM_FAIL("timeout_config", ESP_FAIL);
    struct v4l2_capability cap = {0};
    if (xioctl(s->fd, VIDIOC_QUERYCAP, &cap) != ESP_OK ||
        !(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE) || !(cap.capabilities & V4L2_CAP_STREAMING)) CAM_FAIL("query_caps", ESP_FAIL);
    struct v4l2_format fmt = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE };
    fmt.fmt.pix.width = CAM_WIDTH; fmt.fmt.pix.height = CAM_HEIGHT;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV; fmt.fmt.pix.field = V4L2_FIELD_ANY;
    /* Some esp_video S_FMT paths accept the request without populating sizeimage.
       Query negotiated geometry; validate the actual mapped/allotted length below. */
    if (xioctl(s->fd, VIDIOC_G_FMT, &fmt) != ESP_OK || fmt.fmt.pix.width != CAM_WIDTH ||
        fmt.fmt.pix.height != CAM_HEIGHT || fmt.fmt.pix.pixelformat != V4L2_PIX_FMT_YUYV) {
        CAM_FAIL("format_query", ESP_FAIL);
    }
    s->req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE; s->req.memory = V4L2_MEMORY_MMAP; s->req.count = 1;
    if (xioctl(s->fd, VIDIOC_REQBUFS, &s->req) != ESP_OK || s->req.count != 1) CAM_FAIL("buffer_alloc", ESP_FAIL);
    memset(&s->buffer, 0, sizeof(s->buffer));
    s->buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE; s->buffer.memory = V4L2_MEMORY_MMAP; s->buffer.index = 0;
    if (xioctl(s->fd, VIDIOC_QUERYBUF, &s->buffer) != ESP_OK || s->buffer.length < CAM_RAW_BYTES ||
        s->buffer.length > CAM_RAW_BYTES + 4096) CAM_FAIL("buffer_query", ESP_FAIL);
    s->mapping_len = s->buffer.length;
    errno = 0;
    s->mapping = mmap(NULL, s->mapping_len, PROT_READ | PROT_WRITE, MAP_SHARED, s->fd, s->buffer.m.offset);
    if (s->mapping == MAP_FAILED) CAM_FAIL("buffer_map", ESP_FAIL);
    if (xioctl(s->fd, VIDIOC_QBUF, &s->buffer) != ESP_OK) CAM_FAIL("buffer_queue", ESP_FAIL);
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(s->fd, VIDIOC_STREAMON, &type) != ESP_OK) CAM_FAIL("stream_start", ESP_FAIL);
    s->streaming = true;
    return true;
fail:
    session_close(s); return false;
}

static bool capture_frame(camera_session_t *s, uint8_t **out, size_t *out_size)
{
    struct v4l2_buffer b = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE, .memory = V4L2_MEMORY_MMAP };
    /* Drop the buffered previous pose, then wait for a genuinely new exposure. */
    if (xioctl(s->fd,VIDIOC_DQBUF,&b)!=ESP_OK) { camera_failure("frame_discard", ESP_FAIL, errno); return false; }
    if (xioctl(s->fd,VIDIOC_QBUF,&b)!=ESP_OK) { camera_failure("frame_requeue", ESP_FAIL, errno); return false; }
    if (xioctl(s->fd, VIDIOC_DQBUF, &b) != ESP_OK) { camera_failure("frame_capture", ESP_FAIL, errno); return false; }
    if (b.index != 0 || b.bytesused < CAM_RAW_BYTES || b.bytesused > s->mapping_len) {
        camera_failure("frame_validate", ESP_ERR_INVALID_SIZE, 0);
        (void)xioctl(s->fd, VIDIOC_QBUF, &b);
        return false;
    }
    /* Keep large converted/retained images out of Wi-Fi's scarce internal heap. */
    uint8_t *dst = heap_caps_malloc(CAM_RAW_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!dst) {
        camera_failure("frame_psram", ESP_ERR_NO_MEM, 0);
        (void)xioctl(s->fd, VIDIOC_QBUF, &b);
        return false;
    }
    const uint8_t *src = s->mapping;
    for (size_t i = 0; i < CAM_RAW_BYTES; i += 4) {
        const int y0 = src[i], u = (int)src[i+1] - 128, y1 = src[i+2], v = (int)src[i+3] - 128;
        int r = y0 + ((359 * v) >> 8), g = y0 - ((88 * u + 183 * v) >> 8), bl = y0 + ((454 * u) >> 8);
        uint16_t p0 = (uint16_t)(((r<0?0:r>255?255:r)>>3)<<11 | ((g<0?0:g>255?255:g)>>2)<<5 | (bl<0?0:bl>255?255:bl)>>3);
        r = y1 + ((359 * v) >> 8); g = y1 - ((88 * u + 183 * v) >> 8); bl = y1 + ((454 * u) >> 8);
        uint16_t p1 = (uint16_t)(((r<0?0:r>255?255:r)>>3)<<11 | ((g<0?0:g>255?255:g)>>2)<<5 | (bl<0?0:bl>255?255:bl)>>3);
        dst[i] = p0 & 255; dst[i+1] = p0 >> 8; dst[i+2] = p1 & 255; dst[i+3] = p1 >> 8;
    }
    bool queued = xioctl(s->fd, VIDIOC_QBUF, &b) == ESP_OK;
    if (!queued) { heap_caps_free(dst); camera_failure("frame_requeue", ESP_FAIL, errno); return false; }
    *out = dst; *out_size = CAM_RAW_BYTES; return true;
}

static void emit_base64(const uint8_t *src, size_t len)
{
    char out[BASE64_CHUNK_BYTES / 3u * 4u + 1u];
    for (size_t off=0; off<len; off+=BASE64_CHUNK_BYTES) {
        size_t n=len-off; if(n>BASE64_CHUNK_BYTES)n=BASE64_CHUNK_BYTES; size_t w=0;
        for(size_t i=0;i<n;i+=3){size_t left=n-i;uint32_t v=((uint32_t)src[off+i]<<16)|(left>1?(uint32_t)src[off+i+1]<<8:0)|(left>2?src[off+i+2]:0);
            out[w++]=s_base64[(v>>18)&63];out[w++]=s_base64[(v>>12)&63];out[w++]=left>1?s_base64[(v>>6)&63]:'=';out[w++]=left>2?s_base64[v&63]:'=';}
        out[w]='\0'; wire_printf("LOOK_DATA %s\n",out);
    }
}

static camera_session_t survey_session;

bool camera_survey_begin(i2c_master_bus_handle_t bus)
{
    if (!bus || s_survey_active) return false;
    s_frame_count=0;
    s_survey_ok=true;
    memset(s_frames,0,sizeof(s_frames));
    memset(&survey_session,0,sizeof(survey_session));
    survey_session.fd=-1;
    survey_session.mapping=MAP_FAILED;
    if (!session_open(&survey_session,bus)) {
        s_survey_ok=false;
        return false;
    }
    s_survey_active=true;
    return true;
}

/* Called synchronously at each settled pose on the existing control task.
 * DQBUF has a 1500ms timeout; stale-frame discard plus fresh-frame wait is
 * bounded to two timeouts (3s) per glance. No movement overlaps capture, no
 * serial command is consumed, and no media is emitted here. */
void camera_survey_glance(unsigned index, void *ctx)
{
    (void)ctx;
    if (!s_survey_active || !s_survey_ok) return;
    if (index != s_frame_count || s_frame_count >= SURVEY_MAX_FRAMES) {
        camera_failure("survey_view_order", ESP_ERR_INVALID_STATE, 0);
        s_survey_ok=false;
        return;
    }
    uint8_t *bytes=NULL;
    size_t size=0;
    if (!capture_frame(&survey_session,&bytes,&size)) {
        s_survey_ok=false;
        ESP_LOGW(CAM_TAG,"capture timed out/failed at view %u",index);
        return;
    }
    s_frames[s_frame_count++] = (retained_frame_t){index,bytes,size};
}

bool camera_survey_finish(void)
{
    if (!s_survey_active) return false;
    session_close(&survey_session);
    s_survey_active=false;
    bool ok=s_survey_ok && s_frame_count==SURVEY_MAX_FRAMES;
    for(unsigned i=0;i<s_frame_count;i++){
        if (ok) {
            wire_printf("LOOK_GLANCE %u\n",s_frames[i].index);
            wire_printf("LOOK_FRAME_BEGIN %u %u RGB565 %u\n",CAM_WIDTH,CAM_HEIGHT,(unsigned)s_frames[i].size);
            emit_base64(s_frames[i].data,s_frames[i].size);
            wire_printf("LOOK_FRAME_END\n");
        }
        heap_caps_free(s_frames[i].data);
        s_frames[i]=(retained_frame_t){0};
    }
    s_frame_count=0;
    s_survey_ok=false;
    fflush(stdout);
    return ok;
}

bool look_camera_capture(i2c_master_bus_handle_t bus)
{
    camera_session_t session; uint8_t *data=NULL; size_t size=0;
    if(s_survey_active || !session_open(&session,bus))return false;
    bool ok=capture_frame(&session,&data,&size); session_close(&session);
    if(ok){wire_printf("LOOK_FRAME_BEGIN %u %u RGB565 %u\n",CAM_WIDTH,CAM_HEIGHT,(unsigned)size);emit_base64(data,size);wire_printf("LOOK_FRAME_END\n");fflush(stdout);heap_caps_free(data);}
    return ok;
}
