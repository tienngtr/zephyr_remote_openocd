# User Documentation

Start with the root [README](../../README.md), the quick start for
installation, module activation, and the first remote flash. These guides hold
the details:

- [Configuration](configuration.md): creating and validating the configuration
  file, defining remotes, SSH arguments and connection sharing setup, fixed
  OpenOCD arguments, copied files and path mappings, forwarded environment
  variables, and defaults and presets.
- [Operations](operations.md): flash, debug, attach, debug server, RTT, and
  semihosting output. Also covers runner options, GDB ports, stopping an
  operation, uninstall, and reference material on forwarded services, exit
  status, connection loss, and SSH connection sharing behavior.
- [Troubleshooting](troubleshooting.md): failures by stage, from setup and
  build through remote startup, a running operation, and session end, plus
  SSH connection sharing problems and remote session files.

> [!NOTE]
> Where the root README or a guide differs from the
> [requirements](../requirements/SRS.md),
> [architecture](../architecture/SAD.md),
> [Protocol v1](../architecture/protocol.md), or
> [configuration schema](../../python/zephyr_remote_openocd/resources/configuration.schema.json),
> those sources take precedence.
