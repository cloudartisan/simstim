/* Fixed, offline, output-only speech. Board routing/registers from M5Unified _speaker_enabled_cb_cores3. */
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "wire_io.h"
#include "wifi_link.h"
#include "ble_channel.h"
#include <unistd.h>
#include <fcntl.h>
#include <stdlib.h>
#include "esp_heap_caps.h"
#include "esp_rom_crc.h"
#include "esp_random.h"
#include "hal/usb_serial_jtag_ll.h"
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "status_cues_data.h"
static i2c_master_dev_handle_t amp,touch,io;
static i2s_chan_handle_t tx;
static bool aw16(uint8_t r,uint16_t v){uint8_t b[]={r,v>>8,v};return i2c_master_transmit(amp,b,3,100)==ESP_OK;}
static bool rd(i2c_master_dev_handle_t h,uint8_t r,uint8_t*b,size_t n){return i2c_master_transmit_receive(h,&r,1,b,n,100)==ESP_OK;}
static bool reset_release(bool release){uint8_t b[]={0x02,0};if(!rd(io,2,b+1,1))return false;b[1]=(b[1]&~4)|(release?4:0);return i2c_master_transmit(io,b,2,100)==ESP_OK;}
static bool attach(i2c_master_bus_handle_t bus,int addr,i2c_master_dev_handle_t*out){i2c_device_config_t c={.device_address=addr,.scl_speed_hz=100000,.dev_addr_length=I2C_ADDR_BIT_LEN_7};return i2c_master_bus_add_device(bus,&c,out)==ESP_OK;}
static bool play_pcm(const uint8_t *pcm,size_t bytes){
 size_t sent=0; bool clocks=false,ok=false;uint8_t st[2]={0},v[2]={0};
 if(!reset_release(true)){goto done;}
 vTaskDelay(pdMS_TO_TICKS(50));
 if(i2s_channel_enable(tx)!=ESP_OK){goto done;}
 clocks=true;
 if(!aw16(0x05,0x0018)||!aw16(0x61,0x0673)||!aw16(0x06,0x14c3)||!aw16(0x0c,0x1864)||!aw16(0x04,0x4040)||!aw16(0x05,0x0008))goto done;
 if(!rd(amp,0x04,v,2)||((v[0]<<8)|v[1])!=0x4040||!rd(amp,0x05,v,2)||(v[1]&0x10))goto done;
 for(size_t i=0;i<bytes/2;){int16_t out[256];size_t n=0,w=0;while(n<128 && i<bytes/2){int16_t a=(int16_t)(pcm[2*i]|pcm[2*i+1]<<8);out[2*n]=out[2*n+1]=a/2;n++;i++;}if(i2s_channel_write(tx,out,n*4,&w,300)!=ESP_OK||w!=n*4)goto done;sent+=w;}
 vTaskDelay(pdMS_TO_TICKS(120));ok=rd(amp,1,st,2);
 done:;bool mute=aw16(0x05,0x0018);bool off=aw16(0x04,0x4003);bool held=reset_release(false);if(clocks)i2s_channel_disable(tx);
 bool played=ok&&mute&&off&&held;wire_printf("SIMSTIM_SPEAKER result=%s stereo_bytes=%u status=%02x%02x reset_off=%d\n",played?"played":"failed",(unsigned)sent,st[0],st[1],held);fflush(stdout);return played;
}
static bool play_status_cue(unsigned cue){
 const int16_t *samples=NULL;size_t count=0;
 if(cue==0){samples=cue_online;count=CUE_ONLINE_SAMPLES;}
 else if(cue==1){samples=cue_no_update;count=CUE_NO_UPDATE_SAMPLES;}
 else if(cue==2){samples=cue_check_failed;count=CUE_CHECK_FAILED_SAMPLES;}
 return samples && play_pcm((const uint8_t*)samples,count*sizeof(*samples));
}
extern bool board_look_cue(bool camera), look_camera_capture(i2c_master_bus_handle_t), motor_look(i2c_master_bus_handle_t,const char*), motor_calibration_ready(void);
extern void board_look_end(void);
extern bool camera_survey_begin(i2c_master_bus_handle_t bus),camera_survey_finish(void);
extern void camera_survey_glance(unsigned index,void *ctx);
extern bool motor_survey(i2c_master_bus_handle_t bus,void (*glance)(unsigned,void*),void*);
static uint8_t *update_pcm; static size_t update_bytes; static uint32_t update_crc; static TickType_t update_time; static wire_owner_t update_owner; static uint32_t update_session;
static void clear_update(void){free(update_pcm);update_pcm=NULL;update_bytes=0;update_crc=0;}
static void expire_update(void){if(update_pcm && xTaskGetTickCount()-update_time>=pdMS_TO_TICKS(1800000))clear_update();}
static uint32_t tap_boot_nonce; static uint32_t tap_counter; static bool tap_nonce_ready;
#define TAP_WAIT_MS 15000
static bool usb_read_exact(uint8_t *dst,size_t len,TickType_t deadline){TickType_t now=xTaskGetTickCount();if((int32_t)(deadline-now)<=0)return false;uint32_t ms=(uint32_t)(deadline-now)*portTICK_PERIOD_MS;return wire_read_exact(dst,len,ms);}
static bool usb_read_line(char *line,size_t cap,TickType_t deadline){TickType_t now=xTaskGetTickCount();if((int32_t)(deadline-now)<=0)return false;uint32_t ms=(uint32_t)(deadline-now)*portTICK_PERIOD_MS;return wire_read_line(line,cap,ms);}
static void tap_done(const char *id,const char *result){if(!strcmp(result,"failed"))play_status_cue(2);wire_printf("TAP_DONE %s %s\n",id,result);fflush(stdout);}
/* One synchronous request bounds pending work and makes late replies harmless:
   after timeout the next request has a different boot-seeded sequence ID. */
static void tap_refresh(void){expire_update();if(update_pcm){if(update_owner==wire_owner() && update_session==wire_session())play_pcm(update_pcm,update_bytes);clear_update();return;}
 if(!tap_nonce_ready){tap_boot_nonce=esp_random();tap_counter=0;tap_nonce_ready=true;}uint32_t serial=++tap_counter;uint32_t request_id=tap_boot_nonce+serial;char id[9],expected[40],line[64];snprintf(id,sizeof(id),"%08lx",(unsigned long)request_id);
 wire_printf("TAP_FETCH %s\n",id);fflush(stdout);TickType_t deadline=xTaskGetTickCount()+pdMS_TO_TICKS(TAP_WAIT_MS);
 if(!usb_read_line(line,sizeof(line),deadline)){tap_done(id,"failed");return;}
 snprintf(expected,sizeof(expected),"TAP_NONE %s",id);if(!strcmp(line,expected)){bool played=play_status_cue(1);tap_done(id,played?"none":"failed");return;}
 snprintf(expected,sizeof(expected),"TAP_FAIL %s",id);if(!strcmp(line,expected)){tap_done(id,"failed");return;}
 snprintf(expected,sizeof(expected),"TAP_AUDIO %s",id);if(strcmp(line,expected)){tap_done(id,"failed");return;}
 wire_printf("TAP_HEADER %s\n",id);fflush(stdout);uint8_t header[8];if(!usb_read_exact(header,sizeof(header),deadline)){tap_done(id,"failed");return;}
 uint32_t len,crc;memcpy(&len,header,4);memcpy(&crc,header+4,4);if(len<320||len>320000||len%2){tap_done(id,"failed");return;}
 uint8_t *pcm=heap_caps_malloc(len,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);if(!pcm){tap_done(id,"failed");return;}wire_printf("TAP_READY %s\n",id);fflush(stdout);
 if(!usb_read_exact(pcm,len,deadline)||esp_rom_crc32_le(0,pcm,len)!=crc){free(pcm);tap_done(id,"failed");return;}bool played=play_pcm(pcm,len);free(pcm);tap_done(id,played?"played":"failed");}
static void tap_update(void){tap_refresh();}
static void dispatch_look(i2c_master_bus_handle_t bus,const char *cmd){
 static TickType_t survey_completed; static bool can_speak; static wire_owner_t speak_owner; static uint32_t speak_session;
 /* Reject motor work before cue playback, camera setup, or motor power. Keep
    LOOK_FRAME camera-only; calibration gates every command that can move or
    probe the actuator, including survey's camera callbacks. */
 bool motor_command=!strcmp(cmd,"LOOK_SURVEY") || !strcmp(cmd,"LOOK_PROBE") ||
    !strcmp(cmd,"LOOK_LEFT") || !strcmp(cmd,"LOOK_RIGHT") ||
    !strcmp(cmd,"LOOK_UP") || !strcmp(cmd,"LOOK_DOWN") || !strcmp(cmd,"LOOK_HOME");
 if(motor_command && !motor_calibration_ready()){
    wire_printf("LOOK_ERROR device_calibration_missing_or_invalid\n");fflush(stdout);return;
 }
 bool staging=!strcmp(cmd,"STAGE_UPDATE");
 if(!strcmp(cmd,"TAP_CHECK")){tap_refresh();return;}
 if(!strcmp(cmd,"TOUCH_STATUS")){uint8_t status=0;bool valid=rd(touch,2,&status,1);wire_printf("TOUCH %s %u\n",valid?(status?"PRESSED":"RELEASED"):"ERROR",valid?(unsigned)status:0);fflush(stdout);return;}
 if(!strcmp(cmd,"PROX_STATUS")){extern bool board_proximity_status(uint16_t*,bool*);uint16_t count=0;bool saturated=false;bool valid=board_proximity_status(&count,&saturated);wire_printf("PROX %s %u %u\n",valid?"OK":"ERROR",(unsigned)count,(unsigned)saturated);fflush(stdout);return;}
 if(!strcmp(cmd,"BLE_DIAGNOSTICS")){
    char line[192];
#if CONFIG_BT_NIMBLE_ENABLED
    int n=ble_radio_diagnostic_line(wire_owner()==WIRE_WIFI,line,sizeof(line));
#else
    int n=0;
#endif
    if(n<=0){wire_printf("LOOK_ERROR diagnostics_unavailable\n");return;}
    wire_printf("%s",line);fflush(stdout);return;
 }
 if(!strcmp(cmd,"UPDATE_STATUS")){expire_update();wire_printf("UPDATE_STATUS bytes=%u crc=%08lx\n",(unsigned)update_bytes,(unsigned long)update_crc);fflush(stdout);return;}
 if(staging || !strcmp(cmd,"SAY_OBSERVATION")){
    if(!staging && (!can_speak || speak_owner!=wire_owner() || speak_session!=wire_session() || xTaskGetTickCount()-survey_completed>pdMS_TO_TICKS(90000))){wire_printf("SAY_DENIED\n");return;}
    if(!staging){can_speak=false;} wire_printf("SAY_HEADER\n");fflush(stdout);
    /* Host waits for affirmative allocation before sending PCM. Byte count and CRC
       bound a single short observation to this completed survey; never replay. */
    uint8_t header[8]; size_t got=0; TickType_t start=xTaskGetTickCount();
    while(got<8 && xTaskGetTickCount()-start<pdMS_TO_TICKS(1500)){
        int n=wire_read(header+got,8-got);if(n<0)break;if(n>0)got+=(size_t)n;else vTaskDelay(1);
    }
    if(got!=8){wire_printf("SAY_ERROR header\n");return;}
    uint32_t len,crc;memcpy(&len,header,4);memcpy(&crc,header+4,4);
    if(len<320 || len>320000 || len%2){wire_printf("SAY_ERROR length\n");return;}
    uint8_t *pcm=heap_caps_malloc(len,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!pcm){wire_printf("SAY_ERROR memory\n");return;}
    wire_printf("SAY_READY\n");fflush(stdout);got=0;start=xTaskGetTickCount();
    while(got<len && xTaskGetTickCount()-start<pdMS_TO_TICKS(6000)){
       int n=wire_read(pcm+got,len-got);if(n<0)break;if(n>0)got+=(size_t)n;
       if(!n)taskYIELD();
       if(!n && (xTaskGetTickCount()-start)%10==0)vTaskDelay(1);
    }
    if(got==len && esp_rom_crc32_le(0,pcm,len)==crc){if(staging){clear_update();update_pcm=pcm;pcm=NULL;update_bytes=len;update_crc=crc;update_time=xTaskGetTickCount();update_owner=wire_owner();update_session=wire_session();wire_printf("UPDATE_STAGED\n");}else play_pcm(pcm,len);}else{wire_printf("SAY_ERROR transfer\n");}
    free(pcm);fflush(stdout);return;
 }
 if(!strcmp(cmd,"LOOK_SURVEY")){
    can_speak=false;
    if(!board_look_cue(true)){wire_printf("LOOK_ERROR cue\n");return;}
    wire_printf("LOOK_SURVEY_BEGIN\n");fflush(stdout);
    bool ok=false;
    if(camera_survey_begin(bus)){
        bool moved=motor_survey(bus,camera_survey_glance,NULL);
        bool captured=camera_survey_finish();
        ok=moved && captured;
    }
    board_look_end();can_speak=ok;speak_owner=wire_owner();speak_session=wire_session();survey_completed=xTaskGetTickCount();
    wire_printf("LOOK_SURVEY_END ok=%u\n",(unsigned)ok);fflush(stdout);return;
 }
 bool camera=!strcmp(cmd,"LOOK_FRAME");
 if(!camera && strcmp(cmd,"LOOK_PROBE") && strcmp(cmd,"LOOK_LEFT") && strcmp(cmd,"LOOK_RIGHT") && strcmp(cmd,"LOOK_UP") && strcmp(cmd,"LOOK_DOWN") && strcmp(cmd,"LOOK_HOME"))return;
 if(!board_look_cue(camera)){wire_printf("LOOK_ERROR cue\n");return;}
 bool ok=camera?look_camera_capture(bus):motor_look(bus,cmd+5);
 board_look_end();
 if(!ok)wire_printf("LOOK_ERROR operation\n");
 fflush(stdout);
}
static void controls(void*bus){bool armed=false,down=false,overflow=false;int idle=0;char cmd[32];size_t used=0;bool active=false;wire_owner_t owner=WIRE_USB;uint32_t session=0;fcntl(0,F_SETFL,O_NONBLOCK);for(;;){bool trigger=false;uint8_t touches=0;if(rd(touch,2,&touches,1)&&touches<=1){if(!touches){if(down&&armed)trigger=true;down=false;if(++idle>=3)armed=true;}else{down=true;idle=0;}}else{armed=false;idle=0;down=false;}
 uint8_t c;for(int j=0;j<32;++j){int n=0;if(active)n=wire_read(&c,1);else{n=usb_serial_jtag_ll_read_rxfifo(&c,1);if(n==1){owner=WIRE_USB;session=0;}else{uint32_t candidate=wifi_link_session();if(candidate){n=wifi_link_read(candidate,&c,1);if(n>0){owner=WIRE_WIFI;session=candidate;}}}if(n>0){if(!wire_transaction_begin(owner,session)){n=0;}else active=true;}}if(n<0){wire_transaction_end();active=false;used=0;overflow=false;break;}if(n!=1)break;if(c=='\n'||c=='\r'){cmd[used]=0;if(!overflow){if(owner==WIRE_USB&&!strcmp(cmd,"WIFI_PROVISION"))simstim_wifi_provision_usb();else if(!strcmp(cmd,"SPEAK_ONLINE"))play_status_cue(0);else dispatch_look(bus,cmd);}used=0;overflow=false;wire_transaction_end();active=false;}else if(c<32||c>126||used>=sizeof(cmd)-1)overflow=true;else cmd[used++]=(char)c;}
 expire_update();if(trigger&&!active){armed=false;down=false;idle=0;uint32_t tap_session=wifi_link_session();wire_owner_t tap_owner=tap_session?WIRE_WIFI:WIRE_USB;if(wire_transaction_begin(tap_owner,tap_session)){static uint32_t event_count;uint32_t uptime_ms=(uint32_t)(xTaskGetTickCount()*portTICK_PERIOD_MS);wire_printf("TAP_EVENT %lu %lu\n",(unsigned long)++event_count,(unsigned long)uptime_ms);tap_update();wire_transaction_end();}}vTaskDelay(pdMS_TO_TICKS(40));}}
bool speaker_init(i2c_master_bus_handle_t bus){if(!attach(bus,0x36,&amp)||!attach(bus,0x38,&touch)||!attach(bus,0x58,&io)||!reset_release(false))return false;
 i2s_chan_config_t ch=I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0,I2S_ROLE_MASTER);ch.auto_clear=true;if(i2s_new_channel(&ch,&tx,NULL)!=ESP_OK)return false;
 i2s_std_config_t cfg={.clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(16000),.slot_cfg=I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_STEREO),.gpio_cfg={.mclk=I2S_GPIO_UNUSED,.bclk=34,.ws=33,.dout=13,.din=I2S_GPIO_UNUSED}};
 return i2s_channel_init_std_mode(tx,&cfg)==ESP_OK&&xTaskCreate(controls,"spk_ctl",12288,bus,3,NULL)==pdPASS;}
