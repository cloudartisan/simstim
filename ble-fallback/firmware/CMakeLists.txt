idf_component_register(SRCS "ble_stream.c" "nimble_gatt_client.c" "ble_tls_bio.c" "transport_selector.c" INCLUDE_DIRS "." REQUIRES bt mbedtls)
