# Troubleshooting

If setup fails, check that the module copy is readable, the configuration parent
is writable, and the command is running with Python 3.12 or newer. Missing
`pyelftools`, PyYAML, or jsonschema produces a setup warning. Use the Python
environment configured for Zephyr 4.4.x; the runner has no separate product
dependency-installation step.

For remote operations, verify the configured SSH argv can connect directly and
that the selected remote has an executable `openocd_command`. Use
`ssh_command` for fixed SSH arguments (for example, `-F /path/to/ssh_config`);
do not put shell pipelines in the value. Path mappings
must point to existing local inputs and normalized remote POSIX destinations.

The helper deploys under the remote user's private runtime or cache directory.
It removes session workspaces after normal exit, failure, interruption, or loss
of the SSH control connection. A failure message containing a session workspace
or forwarding endpoint is useful diagnostic evidence; do not remove another
user's workspace.

Successful session cleanup removes the workspace and its lease/closure metadata.
Failure to remove any of these artifacts is reported as a cleanup failure.
Orphaned coordination metadata older than 24 hours is reclaimed opportunistically
during later session allocation when its workspace is absent; this does not
guarantee a maximum retention time.

If child disposal cannot be confirmed, the helper retains workspace inputs and
reports cleanup failure. Confirm that the corresponding OpenOCD processes and
staging operations have stopped before removing retained data. Current helpers
also leave legacy workspace roots from older revisions alone; these locations
are listed in [operations.md](operations.md#stop-and-uninstall).

A best-effort forwarding warning identifies an unavailable Tcl or telnet
forward. Check the named local port and SSH diagnostic; the operation can
continue when its required forwards are healthy. With `debug --rtt-server`,
`attach --rtt-server`, or `debugserver --rtt-server`, both GDB and RTT SSH
forwarding are required. RTT
forwarding startup or observed runtime failure fails the command; the runner
does not probe the remote RTT service. The `rtt` command requires GDB
only for setup, then requires the RTT forward. Runtime warnings appear when
the session next checks forwarding status, potentially after interactive GDB
returns. An error reporting failed forwarding rollback or resource cleanup
remains fatal even for a best-effort forward, because cleanup could not
complete.

Local SSH-loss detection is controlled by the configured SSH client and local
operating system. Remote helper detection is separate: the helper begins
bounded OpenOCD process cleanup only after the remote SSH service/operating
system delivers control-channel EOF or a termination signal. Local detection
does not guarantee when remote cleanup begins, and the project does not bound
that interval. Configure client-side keepalive or timeout behavior through
`ssh_command` if different local detection behavior is required; it does not
control remote detection timing.

## SSH connection sharing

`ControlMaster` and other SSH connection-sharing mechanisms are managed by your
SSH client. The runner stops the subprocesses it launches but does not own or
cancel forwarding state retained by an external sharing master. A retained
listener after runner shutdown is not, by itself, failed runner cleanup.

The runner keeps its best-effort local preferred address cache under
`~/.cache/zephyr_remote_openocd/preferred-addresses/`, keyed by the configured SSH
command prefix and host. It saves an address only after required forwarding
succeeds. On a later session, the helper first tries that address with its
normal lease and remote-port checks, then falls back to random allocation if
the preferred address cannot be reused. Concurrent sessions still receive
separate leases.
Deleting or losing the cache cannot weaken these checks and does not cancel
retained forwards.

If a stale retained forward occupies the requested local port, and the helper
cannot reuse its remote address or the requested endpoints have changed, the
operation may report a local-port conflict or the retained endpoint may no
longer reach OpenOCD. Inspect and remove the stale state through the configured
SSH client's normal controls, then retry. For OpenSSH, consult its connection-
sharing controls for cancelling forwards or closing an unused master. Avoid
closing a master still used by another active session. The runner does not
perform this user cleanup automatically.

Contributors can set `ZRO_RECORD=1` to inspect the runner's generated JSON plan
without starting SSH, OpenOCD, GDB, forwarding, or hardware access. This checks
plan construction, not deployment or real command behavior. See
[Runner recording mode](../development/testing.md#runner-recording-mode) for
usage, limitations, and the optional injected OpenOCD version.

## Common symptoms

**`remote_openocd` is not listed by `west flash --context`.** Ensure the module
is in `EXTRA_ZEPHYR_MODULES`. If the build directory predates activation of the
module, explicitly reconfigure it with `west build --cmake-only -d <build>`.
Otherwise, a normal `west build`, `west flash`, or `west debug` reloads its
runners; a pristine build is not required.

**No remote is selected.** Pass `--remote NAME`, set
`ZEPHYR_REMOTE_OPENOCD_REMOTE`, or configure `default_remote`. The selected
name must exist in the YAML file.

**SSH works manually but the runner fails.** Check the complete configured
`ssh_command`, including fixed options, and verify that remote `python3` and
the configured OpenOCD executable are available to that SSH environment.

**The remote reports that `python3` is missing or too old.** The helper is a
Python program deployed and started through the configured SSH command. Install
a remote `python3` version 3.12 or newer that is available to non-interactive
SSH commands, then repeat the two remote prerequisite checks from the README
with the same SSH options.

**The runner reports `remote OpenOCD version query failed`.** Run the configured
`openocd_command` with `--version` through the configured SSH command. Correct
the executable path, permissions, fixed arguments, or remote shared-library
environment before retrying. An `invalid remote OpenOCD version response`
instead indicates that the helper response was incomplete or incompatible.

**Mapped input is missing remotely.** Remove the mapping to stage the local
input automatically, or install the resource at the mapped remote path and
ensure its path is normalized.

**The runner says an allow-listed environment variable is absent.** A warning
of the form `allow-listed environment variable NAME is absent; omitting it`
means `NAME` appears in `forward_env` but is not set locally. Export it before
starting west, or remove it from `forward_env` if remote OpenOCD does not need
it. Missing values are never forwarded as empty strings. Omitting a missing
local value also does not unset a same-named variable already present in the
remote helper environment; that remote value may remain available to OpenOCD.

**A GDB client cannot connect.** Use the local endpoint printed by
`debugserver` or an RTT-server operation. Check that the client uses the local
forwarded port, not the remote OpenOCD port, and that the west process is still
running.

**Editing the configuration has no effect.** Once the module is configured into
the build, run `west flash` or `west debug` normally after changing the default
runner; their pre-run incremental build updates generated runner metadata. You
can also run `west build` explicitly. A `--no-rebuild` command intentionally
uses existing metadata, and a pristine build or full firmware compile is not
required solely to regenerate it.
