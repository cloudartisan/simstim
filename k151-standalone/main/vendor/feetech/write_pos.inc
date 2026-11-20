int FeetechScs::WritePos(uint8_t id, uint16_t position, uint16_t time_ms, uint16_t speed)
{
    // Defensive clamp — protects the mech if any caller forgets to clamp.
    if (position > HARD_POS_MAX) position = HARD_POS_MAX;

    // SCSCL series stores 16-bit registers BIG-endian over the wire: the
    // byte at the lower register address (`*_L`) is actually the *high*
    // byte of the word. (The naming in the datasheet is "L = low address",
    // not "low byte" — easy to misread.) Sending little-endian made the
    // servo see a huge value and clamp to the mechanical maximum.
    uint8_t params[6] = {
        static_cast<uint8_t>((position >> 8) & 0xFF),
        static_cast<uint8_t>(position & 0xFF),
        static_cast<uint8_t>((time_ms >> 8) & 0xFF),
        static_cast<uint8_t>(time_ms & 0xFF),
        static_cast<uint8_t>((speed >> 8) & 0xFF),
        static_cast<uint8_t>(speed & 0xFF),
    };
    return write_reg(id, REG_GOAL_POSITION_L, params, 6);
}
