# SPDX-License-Identifier: Apache-2.0

include("${CMAKE_CURRENT_LIST_DIR}/configuration_path.cmake")
if(EXISTS "${ZRO_CONFIG_STATE}")
  file(READ "${ZRO_CONFIG_STATE}" _zro_previous_config)
  if(_zro_config STREQUAL _zro_previous_config)
    return()
  endif()
endif()

file(WRITE "${ZRO_CONFIG_STATE}" "${_zro_config}")
