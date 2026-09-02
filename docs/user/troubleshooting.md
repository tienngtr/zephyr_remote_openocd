# Troubleshooting

If setup fails, check that the module copy is readable, the configuration parent
is writable, and the command is running with Python 3.12 or newer. Missing
`pyelftools`, PyYAML, or jsonschema produces a setup warning. Use the Python
environment configured for Zephyr 4.4; the runner has no separate product
dependency-installation step.

For remote operations, verify the configured SSH argv can connect directly and
that the selected remote has an executable `openocd_command`. Use
`ssh_command` for fixed SSH arguments (for example,
`ControlMaster=no`); do not put shell pipelines in the value. Path mappings
must point to existing local inputs and normalized remote POSIX destinations.

The helper deploys under the remote user's private runtime or cache directory.
It removes session workspaces after normal exit, failure, interruption, or loss
of the SSH control connection. A failure message containing a session workspace
or forwarding endpoint is useful diagnostic evidence; do not remove another
user's workspace.

Recording mode (`ZEPHYR_REMOTE_OPENOCD_RECORD=1`) is deliberately no-I/O. It
is useful for checking generated commands and configuration without requiring
SSH, OpenOCD, GDB, or hardware.
