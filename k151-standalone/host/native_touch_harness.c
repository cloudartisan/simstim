#include <assert.h>
#include <stdarg.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "native_board.h"
#include "../main/speaker.c"

static jmp_buf finished;
static const uint8_t pcm_fixture[320] = {
  0x00,0x10, 0x00,0xf0, 0xff,0x7f, 0x00,0x80,
  0x34,0x12, 0xcc,0xed, 0x01,0x00, 0xff,0xff
};
static const uint8_t touches[] = {0,0,0,1,0,0,0,0,0,0,0};
static size_t touch_at, wire_at;
static uint8_t staged_wire[sizeof(pcm_fixture)+8];
static uint8_t amp_regs[256][2];
static uint8_t io_reg=0;
static unsigned tx_calls, i2s_enables, tap_events, tap_fetches, received_bytes;
static int payload_ok=1, reset_held;
static wire_owner_t active_owner=WIRE_USB;
static uint32_t active_session;

static uint32_t crc32_std(const uint8_t *p,size_t n){uint32_t c=~0u;for(size_t i=0;i<n;i++){c^=p[i];for(int b=0;b<8;b++)c=(c>>1)^(0xedb88320u&-(c&1));}return ~c;}
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t b,const i2c_device_config_t*c,i2c_master_dev_handle_t*out){(void)b;*out=(void*)(uintptr_t)c->device_address;return ESP_OK;}
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t h,const uint8_t*b,size_t n,int timeout){(void)timeout;unsigned addr=(unsigned)(uintptr_t)h;if(addr==0x58&&n==2&&b[0]==2){io_reg=b[1];reset_held=(b[1]&4)==0;}if(addr==0x36&&n==3){amp_regs[b[0]][0]=b[1];amp_regs[b[0]][1]=b[2];}return ESP_OK;}
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t h,const uint8_t*reg,size_t rn,uint8_t*out,size_t n,int timeout){(void)rn;(void)timeout;unsigned addr=(unsigned)(uintptr_t)h;if(addr==0x38){out[0]=touch_at<sizeof(touches)?touches[touch_at++]:0;return ESP_OK;}if(addr==0x58){out[0]=io_reg;return ESP_OK;}if(addr==0x36){memcpy(out,amp_regs[*reg],n);return ESP_OK;}memset(out,0,n);return ESP_OK;}
int wire_printf(const char *fmt,...){char line[512];va_list ap;va_start(ap,fmt);int n=vsnprintf(line,sizeof line,fmt,ap);va_end(ap);fputs(line,stdout);if(!strncmp(line,"TAP_EVENT ",10))tap_events++;if(!strncmp(line,"TAP_FETCH ",10))tap_fetches++;return n;}
int wire_read(void *b,size_t n){if(wire_at>=sizeof(staged_wire))return 0;size_t left=sizeof(staged_wire)-wire_at;if(n>left)n=left;memcpy(b,staged_wire+wire_at,n);wire_at+=n;return (int)n;}
bool wire_read_exact(uint8_t*b,size_t n,uint32_t ms){(void)ms;return wire_read(b,n)==(int)n;}
bool wire_read_line(char*b,size_t n,uint32_t ms){(void)b;(void)n;(void)ms;return false;}
bool wire_transaction_begin(wire_owner_t o,uint32_t s){active_owner=o;active_session=s;return true;}
void wire_transaction_end(void){}
wire_owner_t wire_owner(void){return active_owner;}
uint32_t wire_session(void){return active_session;}
uint32_t wifi_link_session(void){return 0;} int wifi_link_read(uint32_t s,void*b,size_t n){(void)s;(void)b;(void)n;return 0;}
int usb_serial_jtag_ll_read_rxfifo(uint8_t*b,size_t n){(void)b;(void)n;return 0;}
void *heap_caps_malloc(size_t n,int caps){(void)caps;return malloc(n);}
uint32_t esp_rom_crc32_le(uint32_t seed,const uint8_t*p,size_t n){return seed?seed^crc32_std(p,n):crc32_std(p,n);}
uint32_t esp_random(void){return 1234;}
esp_err_t i2s_new_channel(const i2s_chan_config_t*c,i2s_chan_handle_t*txp,i2s_chan_handle_t*rx){(void)c;(void)rx;*txp=(void*)1;return ESP_OK;}
esp_err_t i2s_channel_init_std_mode(i2s_chan_handle_t c,const i2s_std_config_t*s){(void)c;(void)s;return ESP_OK;}
esp_err_t i2s_channel_enable(i2s_chan_handle_t c){(void)c;i2s_enables++;return ESP_OK;}
esp_err_t i2s_channel_disable(i2s_chan_handle_t c){(void)c;return ESP_OK;}
esp_err_t i2s_channel_write(i2s_chan_handle_t c,const void *data,size_t n,size_t*w,uint32_t timeout){(void)c;(void)timeout;const int16_t *samples=data;assert(received_bytes <= 2*sizeof pcm_fixture && n <= 2*sizeof pcm_fixture-received_bytes && n%4==0);size_t count=n/2;for(size_t i=0;i<count;i++){size_t frame=received_bytes/4+i/2;int16_t expect=(int16_t)(pcm_fixture[frame*2]|(pcm_fixture[frame*2+1]<<8))/2;if(samples[i]!=expect)payload_ok=0;}received_bytes+=(unsigned)n;tx_calls++;*w=n;return ESP_OK;}
int xTaskCreate(void(*fn)(void*),const char*n,unsigned stack,void*arg,unsigned pri,void*handle){(void)fn;(void)n;(void)stack;(void)arg;(void)pri;(void)handle;return pdPASS;}
TickType_t xTaskGetTickCount(void){static TickType_t t;return t++;}
static unsigned delay_calls;
void vTaskDelay(TickType_t ticks){if(ticks==pdMS_TO_TICKS(40) && ++delay_calls>=sizeof touches)longjmp(finished,1);}
bool board_proximity_status(uint16_t *count,bool *saturated){*count=0;*saturated=false;return true;}
void simstim_wifi_provision_usb(void){}
bool board_look_cue(bool camera){(void)camera;return true;} bool look_camera_capture(i2c_master_bus_handle_t b){(void)b;return true;} bool motor_look(i2c_master_bus_handle_t b,const char*c){(void)b;(void)c;return true;} bool motor_calibration_ready(void){return true;} void board_look_end(void){}
bool camera_survey_begin(i2c_master_bus_handle_t b){(void)b;return true;} bool camera_survey_finish(void){return true;} void camera_survey_glance(unsigned i,void*c){(void)i;(void)c;} bool motor_survey(i2c_master_bus_handle_t b,void(*g)(unsigned,void*),void*c){(void)b;(void)g;(void)c;return true;}

int main(int argc,char **argv){uint32_t len=sizeof(pcm_fixture),crc=argc>1?(uint32_t)strtoul(argv[1],NULL,16):crc32_std(pcm_fixture,sizeof(pcm_fixture));memcpy(staged_wire,&len,4);memcpy(staged_wire+4,&crc,4);memcpy(staged_wire+8,pcm_fixture,sizeof(pcm_fixture));
 if(!speaker_init(NULL))return 3;
 active_owner=WIRE_USB;active_session=0;
 dispatch_look(NULL,"UPDATE_STATUS");
 dispatch_look(NULL,"STAGE_UPDATE");
 dispatch_look(NULL,"UPDATE_STATUS");
 if(!setjmp(finished))controls(NULL);
 dispatch_look(NULL,"UPDATE_STATUS");
 printf("NATIVE_ASSERT events=%u fetches=%u wire_bytes=%zu pcm_bytes=%u stereo_bytes=%u payload_ok=%d i2s_enables=%u i2s_writes=%u reset_held=%d amp_volume=%02x%02x\n",tap_events,tap_fetches,wire_at,len,received_bytes,payload_ok,i2s_enables,tx_calls,reset_held,amp_regs[4][0],amp_regs[4][1]);
 return (tap_events==1&&tap_fetches==0&&wire_at==sizeof(staged_wire)&&received_bytes==len*2&&payload_ok&&i2s_enables==1&&tx_calls>0&&reset_held&&amp_regs[4][0]==0x40&&amp_regs[4][1]==0x03)?0:2;
}
