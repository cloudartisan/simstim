if(CONFIG_BT_NIMBLE_ENABLED)
    set(ble_dir "${CMAKE_CURRENT_LIST_DIR}/../../ble-fallback/firmware")
    set(ble_sources "ble_radio.c" "ble_channel.c" "${ble_dir}/nimble_gatt_client.c" "${ble_dir}/ble_stream.c")
    set(ble_dep bt)
endif()
include("${CMAKE_CURRENT_LIST_DIR}/../public_config.cmake")
set(SIMSTIM_CUE_MODEL "$ENV{HOME}/.local/share/simstim/voice/kokoro-v1.0.onnx" CACHE FILEPATH "External pinned Kokoro ONNX model")
set(SIMSTIM_CUE_VOICES "$ENV{HOME}/.local/share/simstim/voice/voices-v1.0.bin" CACHE FILEPATH "External pinned Kokoro voice pack")
set(SIMSTIM_CUE_PYTHON "$ENV{HOME}/.local/share/simstim/voice/render/bin/python" CACHE FILEPATH "Python with pinned Kokoro build dependencies")
set(SIMSTIM_CUE_OUTPUT "${CMAKE_BINARY_DIR}/generated-cues")
set(CUE_GENERATOR "${CMAKE_CURRENT_LIST_DIR}/../tools/generate_cues.py")
if(NOT SIMSTIM_FEETECH_SOURCE_DIR)
    set(SIMSTIM_FEETECH_SOURCE_DIR "$ENV{SIMSTIM_FEETECH_SOURCE_DIR}")
endif()
if(NOT EXISTS "${SIMSTIM_FEETECH_SOURCE_DIR}/write_pos.inc")
    message(FATAL_ERROR "Pinned Feetech WritePos source missing. Run build.sh to prepare it; see docs/FIRMWARE.md.")
endif()
if(NOT EXISTS "${SIMSTIM_CUE_MODEL}" OR NOT EXISTS "${SIMSTIM_CUE_VOICES}" OR NOT EXISTS "${SIMSTIM_CUE_PYTHON}")
    message(FATAL_ERROR "Status cue build dependencies missing. See docs/CUES.md and set SIMSTIM_CUE_MODEL, SIMSTIM_CUE_VOICES, SIMSTIM_CUE_PYTHON.")
endif()
execute_process(
    COMMAND "${SIMSTIM_CUE_PYTHON}" "${CUE_GENERATOR}" --model "${SIMSTIM_CUE_MODEL}" --voices "${SIMSTIM_CUE_VOICES}" --out "${SIMSTIM_CUE_OUTPUT}"
    RESULT_VARIABLE CUE_RENDER_RESULT
    COMMAND_ECHO STDOUT
)
if(NOT CUE_RENDER_RESULT EQUAL 0)
    message(FATAL_ERROR "Kokoro cue generation failed (${CUE_RENDER_RESULT})")
endif()
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CUE_GENERATOR}" "${SIMSTIM_CUE_MODEL}" "${SIMSTIM_CUE_VOICES}")
idf_component_register(
    SRCS ${ble_sources} "main.c" "board.c" "speaker.c" "${SIMSTIM_CUE_OUTPUT}/status_cues_data.c" "look_camera.c" "look_motor.c" "servo_position.cpp" "wire_io.c" "wifi_link.c" "wifi_provision.c"
    INCLUDE_DIRS "." ${ble_dir}
    REQUIRES
        bt esp_app_format esp_video esp_cam_sensor esp_sccb_intf usb_host_uvc
        esp_wifi esp_netif esp_event esp_timer mbedtls nvs_flash lwip esp-tls
        esp_common
        esp_driver_gpio
        esp_driver_i2c
        esp_driver_spi
        esp_driver_uart
        esp_driver_i2s
        esp_hw_support
        freertos
        log
)
target_include_directories(${COMPONENT_LIB} PRIVATE "${SIMSTIM_GENERATED_INCLUDE_DIR}" "${SIMSTIM_CUE_OUTPUT}" "${SIMSTIM_FEETECH_SOURCE_DIR}")
