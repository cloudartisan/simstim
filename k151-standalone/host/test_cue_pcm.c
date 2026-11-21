#include "status_cues_data.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

static void check(const int16_t *samples, size_t count, const char *path) {
    assert(samples && count >= 3200);
    FILE *pcm = fopen(path, "rb");
    assert(pcm);
    size_t active = 0;
    uint64_t energy = 0;
    for (size_t i = 0; i < count; ++i) {
        uint8_t bytes[2];
        assert(fread(bytes, 1, sizeof(bytes), pcm) == sizeof(bytes));
        int16_t expected = (int16_t)((uint16_t)bytes[0] | ((uint16_t)bytes[1] << 8));
        assert(samples[i] == expected);
        int32_t x = samples[i];
        if (x < 0) x = -x;
        active += x != 0;
        energy += (uint64_t)x;
    }
    assert(fgetc(pcm) == EOF);
    fclose(pcm);
    assert(active > count / 2);
    assert(energy / count > 100);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    char path[1024];
    snprintf(path, sizeof(path), "%s/online.pcm", argv[1]);
    check(cue_online, CUE_ONLINE_SAMPLES, path);
    snprintf(path, sizeof(path), "%s/no_update.pcm", argv[1]);
    check(cue_no_update, CUE_NO_UPDATE_SAMPLES, path);
    snprintf(path, sizeof(path), "%s/check_failed.pcm", argv[1]);
    check(cue_check_failed, CUE_CHECK_FAILED_SAMPLES, path);
    assert(CUE_ONLINE_SAMPLES != CUE_NO_UPDATE_SAMPLES);
    assert(CUE_ONLINE_SAMPLES != CUE_CHECK_FAILED_SAMPLES);
    assert(CUE_NO_UPDATE_SAMPLES != CUE_CHECK_FAILED_SAMPLES);
    puts("firmware cue C arrays byte-match PCM: 3 non-silent, distinct clips passed");
    return 0;
}
