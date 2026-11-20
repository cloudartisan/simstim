cmake_minimum_required(VERSION 3.16)
# Source exports need a version independent of Git metadata.
set(PROJECT_VER "0.1.0")

set(EXTRA_COMPONENT_DIRS)
if(DEFINED ENV{SIMSTIM_COMPONENTS_DIR} AND NOT "$ENV{SIMSTIM_COMPONENTS_DIR}" STREQUAL "")
    # The offline IDF mirror keeps registry namespace prefixes in directory
    # names (espressif__esp_video); IDF's component resolver expects the
    # component name itself. Create build-local aliases without touching it.
    set(_simstim_components "$ENV{SIMSTIM_COMPONENTS_DIR}")
    set(_simstim_aliases "${CMAKE_BINARY_DIR}/simstim-managed-components")
    file(MAKE_DIRECTORY "${_simstim_aliases}")
    foreach(_component IN ITEMS cmake_utilities esp_cam_sensor esp_sccb_intf esp_video usb_host_uvc)
        if(EXISTS "${_simstim_components}/espressif__${_component}")
            if(NOT EXISTS "${_simstim_aliases}/${_component}")
                file(CREATE_LINK "${_simstim_components}/espressif__${_component}"
                     "${_simstim_aliases}/${_component}" SYMBOLIC)
            endif()
        elseif(NOT EXISTS "${_simstim_aliases}/${_component}")
            message(FATAL_ERROR "Missing offline component espressif__${_component} in SIMSTIM_COMPONENTS_DIR")
        endif()
        list(APPEND EXTRA_COMPONENT_DIRS "${_simstim_aliases}/${_component}")
    endforeach()
    list(APPEND CMAKE_MODULE_PATH "${_simstim_aliases}/cmake_utilities")
endif()
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# Do not pull optional IDF components (Wi-Fi/Bluetooth/media/etc.) into this app.
idf_build_set_property(MINIMAL_BUILD ON)
project(k151_standalone)
