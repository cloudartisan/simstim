if(CONFIG_BT_NIMBLE_ENABLED)
    set(ble_dir "${CMAKE_CURRENT_LIST_DIR}/../../ble-fallback/firmware")
    set(ble_sources "ble_radio.c" "ble_channel.c" "${ble_dir}/nimble_gatt_client.c" "${ble_dir}/ble_stream.c")
    set(ble_dep bt)
endif()
include("${CMAKE_CURRENT_LIST_DIR}/../public_config.cmake")
idf_component_register(
    SRCS ${ble_sources} "main.c" "board.c" "speaker.c" "look_camera.c" "look_motor.c" "servo_position.cpp" "wire_io.c" "wifi_link.c" "wifi_provision.c"
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
target_include_directories(${COMPONENT_LIB} PRIVATE "${SIMSTIM_GENERATED_INCLUDE_DIR}")
