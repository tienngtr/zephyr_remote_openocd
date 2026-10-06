# SPDX-License-Identifier: Apache-2.0

get_property(_zro_runners GLOBAL PROPERTY ZEPHYR_RUNNERS)
if(NOT "openocd" IN_LIST _zro_runners)
  return()
endif()

if(CMAKE_GENERATOR STREQUAL "Unix Makefiles" AND NOT CMAKE_SUPPRESS_REGENERATION)
  # Refresh metadata only; never reconfigure an active parallel Make build.
  # ALL covers normal builds. This target can also run without building firmware.
  add_custom_target(zro_runner_state ALL
    COMMAND "${PYTHON_EXECUTABLE}" "${CMAKE_CURRENT_LIST_DIR}/config_default.py"
            --refresh-runner-state "${PROJECT_BINARY_DIR}/runners.yaml"
    VERBATIM)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/configuration_path.cmake")

if(CMAKE_GENERATOR MATCHES "^Ninja")
  # Make environment-only path changes visible to CMake's file-based checks.
  set(_zro_config_state "${CMAKE_CURRENT_BINARY_DIR}/zro-config-path.txt")
  file(WRITE "${_zro_config_state}" "${_zro_config}")
  configure_file("${_zro_config_state}" "${_zro_config_state}.configured" COPYONLY)
  # A generated configure input runs before Ninja's CMake regeneration rule.
  # Restat suppresses regeneration when the selected path remains unchanged.
  add_custom_target(zro_config_path_check
    COMMAND "${CMAKE_COMMAND}" "-DZRO_CONFIG_STATE=${_zro_config_state}"
            -P "${CMAKE_CURRENT_LIST_DIR}/check_configuration_path.cmake"
    BYPRODUCTS "${_zro_config_state}"
    VERBATIM)
endif()

# A normal incremental build asks CMake to reconfigure when this file changes.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_zro_config}")

# Track creation and deletion without treating glob characters in the
# user-selected path as patterns.
set(_zro_config_glob "${_zro_config}")
string(REPLACE "[" "[[]" _zro_config_glob "${_zro_config_glob}")
string(REPLACE "*" "[*]" _zro_config_glob "${_zro_config_glob}")
string(REPLACE "?" "[?]" _zro_config_glob "${_zro_config_glob}")
file(GLOB _zro_config_presence
     CONFIGURE_DEPENDS
     LIST_DIRECTORIES false
     "${_zro_config_glob}")

string(MAKE_C_IDENTIFIER openocd _zro_openocd_id)
string(MAKE_C_IDENTIFIER remote_openocd _zro_remote_id)
get_property(_zro_openocd_args GLOBAL PROPERTY
             "BOARD_RUNNER_ARGS_${_zro_openocd_id}")
set_property(GLOBAL PROPERTY "BOARD_RUNNER_ARGS_${_zro_remote_id}"
             ${_zro_openocd_args})
set_property(GLOBAL APPEND PROPERTY ZEPHYR_RUNNERS remote_openocd)

execute_process(
  COMMAND "${PYTHON_EXECUTABLE}"
          "${ZEPHYR_CURRENT_CMAKE_DIR}/config_default.py"
          "${_zro_config}"
  RESULT_VARIABLE _zro_config_result
  OUTPUT_VARIABLE _zro_default
  ERROR_VARIABLE _zro_config_error
  OUTPUT_STRIP_TRAILING_WHITESPACE
)
if(NOT _zro_config_result EQUAL 0)
  message(FATAL_ERROR "${_zro_config_error}")
endif()

if(_zro_default STREQUAL "remote_openocd")
  set(BOARD_FLASH_RUNNER remote_openocd PARENT_SCOPE)
  set(BOARD_DEBUG_RUNNER remote_openocd PARENT_SCOPE)
elseif(_zro_default STREQUAL "openocd")
  set(BOARD_FLASH_RUNNER openocd PARENT_SCOPE)
  set(BOARD_DEBUG_RUNNER openocd PARENT_SCOPE)
else()
  message(FATAL_ERROR "Unexpected Zephyr Remote OpenOCD default: ${_zro_default}")
endif()
