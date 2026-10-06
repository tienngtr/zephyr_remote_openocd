# SPDX-License-Identifier: Apache-2.0

# Shared by configure-time selection and the build-time environment check.
set(_zro_config "$ENV{HOME}/.config/zephyr_remote_openocd/config.yaml")
if(NOT "$ENV{ZEPHYR_REMOTE_OPENOCD_CONFIG}" STREQUAL "")
  set(_zro_config "$ENV{ZEPHYR_REMOTE_OPENOCD_CONFIG}")
  if(_zro_config STREQUAL "~")
    set(_zro_config "$ENV{HOME}")
  elseif(_zro_config MATCHES "^~/")
    string(SUBSTRING "${_zro_config}" 1 -1 _zro_config_suffix)
    set(_zro_config "$ENV{HOME}${_zro_config_suffix}")
  endif()
endif()
