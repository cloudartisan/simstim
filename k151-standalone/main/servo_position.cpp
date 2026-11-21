// The pinned upstream WritePos function is prepared and hash-verified outside
// the source tree; SIMSTIM's echo-aware transport remains locally implemented.
#include <stdint.h>
extern "C" bool simstim_servo_write(uint8_t,uint8_t,const uint8_t*,uint8_t);
namespace {
constexpr uint16_t HARD_POS_MAX=1023;
constexpr uint8_t REG_GOAL_POSITION_L=42;
class FeetechScs {
public:
 int WritePos(uint8_t,uint16_t,uint16_t,uint16_t);
 int write_reg(uint8_t id,uint8_t reg,const uint8_t *data,uint8_t n){return simstim_servo_write(id,reg,data,n)?0:-1;}
};
#include "write_pos.inc"
}
extern "C" bool simstim_timed_position(uint8_t id,uint16_t raw,uint16_t ms){return FeetechScs{}.WritePos(id,raw,ms,0)==0;}
