# Troubleshooting

This page groups common failures by the stage where they appear: setup and
build, remote startup, a running operation, and the end of a session. Find a
reported message or a symptom in the table below, or browse the sections. The
[Reference](#reference) section describes remote session files and cleanup.

| Message or symptom | Section |
| --- | --- |
| Setup script `Error:` or dependency `Warning:` | [The setup script fails or warns](#the-setup-script-fails-or-warns) |
| `remote_openocd` missing from `west flash --context` | [`remote_openocd` is not listed](#remote_openocd-is-not-listed-by-west-flash---context) |
| CMake configuration fails after a configuration edit | [Apply configuration changes to a build](configuration.md#apply-configuration-changes-to-a-build) |
| `no remote selected for remote_openocd` | [No remote is selected](#no-remote-is-selected) |
| `selected remote 'NAME' does not exist` | [No remote is selected](#no-remote-is-selected) |
| The SSH executable cannot be started | [SSH works manually but the runner fails](#ssh-works-manually-but-the-runner-fails) |
| `cannot resolve remote home for NAME` | [A remote `python3` error](#remote-helper-deployment-or-startup-fails-with-a-python3-error) |
| `remote staging timed out`, `helper deployment timed out`, `remote OpenOCD version query timed out`, `... timed out after N seconds` | [A preparation step times out](#a-preparation-step-times-out) |
| `remote OpenOCD version query failed` | [The OpenOCD version query fails](#the-openocd-version-query-fails) |
| `invalid remote OpenOCD version response` | [The OpenOCD version query fails](#the-openocd-version-query-fails) |
| `cannot parse remote OpenOCD version` | [The OpenOCD version query fails](#the-openocd-version-query-fails) |
| `required file is missing`, `required search directory is missing` | [A required file or directory is missing](#a-required-file-or-directory-is-missing) |
| `required remote file is missing`, `required remote directory is missing` | [A required file or directory is missing](#a-required-file-or-directory-is-missing) |
| `child exited before READY`, `remote OpenOCD exited ...`, `remote OpenOCD failed with exit status N` | [Remote OpenOCD exits during startup](#remote-openocd-exits-during-startup) |
| OpenOCD reports that the probe or channel is in use | [OpenOCD cannot acquire the probe](#openocd-cannot-acquire-the-probe) |
| `Best-effort SERVICE forwarding ... failure` | [A best-effort forwarding warning appears](#a-best-effort-forwarding-warning-appears) |
| A required local port is occupied | [A required local port is occupied](#a-required-local-port-is-occupied) |
| GDB client cannot connect | [A GDB client cannot connect](#a-gdb-client-cannot-connect) |
| `allow-listed environment variable NAME is absent` | [An allow-listed environment variable is absent](#an-allow-listed-environment-variable-is-absent) |
| `helper shutdown did not produce SESSION_ENDED` | [Shutdown results](#shutdown-results) |
| `CONTROLLER_LEASE_LOST` | [`CONTROLLER_LEASE_LOST`](#controller_lease_lost) |
| The SSH connection is lost | [The SSH connection is lost](#the-ssh-connection-is-lost) |
| Other sessions disconnect when one session ends | [Other sessions disconnect](#other-sessions-disconnect-when-one-session-ends) |
| A forward from an earlier session occupies the port | [A retained forward occupies the local port](#a-retained-forward-occupies-the-local-port) |

## Setup and build

### The setup script fails or warns

The setup script stops with an error when it cannot find the module root, read
the shipped configuration template, or create the configuration directory or
file, and when an existing configuration path is not a regular file. Check
that:

- the module copy is complete and readable;
- the parent of the configuration directory is writable;
- the command runs with Python 3.12 or newer.

A dependency warning for `pyelftools`, PyYAML, or jsonschema does not stop the
setup script from creating the configuration. Use the Python environment
configured for Zephyr 4.4.x; the runner has no separate product
dependency-installation step. See
[Run the setup script](../../README.md#2-run-the-setup-script).

### `remote_openocd` is not listed by `west flash --context`

`remote_openocd` is registered only for a build that registers Zephyr's
built-in `openocd` runner. If `openocd` is also missing from the list, the
board build does not provide the `openocd` runner that `remote_openocd`
requires. Otherwise:

1. Make sure that `EXTRA_ZEPHYR_MODULES` includes the module path when west
   runs, however the variable is set; see
   [Activate the module](../../README.md#3-activate-the-module).
2. If the build directory was configured before the module was activated,
   reconfigure it with `west build --cmake-only -d <build>`, where `<build>` is
   the build directory. A pristine build is not required; see
   [Configure the build](../../README.md#6-configure-the-build).

### A changed default runner has no effect

Run `west flash` or `west debug` normally, or run `west build`: the pre-run
incremental build updates the default runner stored in the build. A pristine
build or full firmware compile is not required solely to update it. Only the
default runner chosen by `default_runner` is stored; runner commands read the
rest of the configuration each time they run.

The stored default stays unchanged when:

- the command uses `--no-rebuild`, which intentionally uses the stored default
  as it is;
- `ZEPHYR_REMOTE_OPENOCD_CONFIG` now selects a different file; reconfigure
  CMake with `west build --cmake-only -d <build>`;
- the build directory was configured before the module was activated; see
  [`remote_openocd` is not listed](#remote_openocd-is-not-listed-by-west-flash---context).

See
[Apply configuration changes to a build](configuration.md#apply-configuration-changes-to-a-build).

### No remote is selected

The runner reports
`no remote selected for remote_openocd; use --remote or set default_remote`.
Pass `--remote NAME`, set `ZEPHYR_REMOTE_OPENOCD_REMOTE`, or configure
`default_remote`. The selected name must be defined under `remotes`; otherwise
the runner reports `selected remote 'NAME' does not exist`. See
[Remote selection order](configuration.md#remote-selection-order), and check
the file with the [validator](configuration.md#validate-the-configuration).

## Remote startup

### SSH works manually but the runner fails

The runner uses the complete configured `ssh_command`, including its fixed
options, and the remote environment of non-interactive SSH commands. Check
that:

- the [remote prerequisite checks](../../README.md#5-validate-the-configuration-and-remote-prerequisites)
  succeed with the executable and fixed options from `ssh_command`, so remote
  `python3` and the configured
  OpenOCD executable are available in that environment;
- `ssh_command` is an argv list of fixed options without shell pipelines or
  redirections; see [Custom SSH arguments](configuration.md#custom-ssh-arguments);
- the SSH client supports the
  [stdin half-close](configuration.md#stdin-half-close).

If the configured SSH executable cannot be started, the error names it; check
the executable path in `ssh_command`.

### Remote helper deployment or startup fails with a `python3` error

Install remote `python3` version 3.12 or newer so that it is available to
non-interactive SSH commands, then repeat the
[remote prerequisite checks](../../README.md#5-validate-the-configuration-and-remote-prerequisites)
with the executable and fixed options from `ssh_command`.

The runner deploys the remote helper, a Python program, and starts it with the
remote `python3` through the configured SSH command. The runner does not check
the remote Python version itself: a missing `python3` typically appears as an
SSH or command-not-found diagnostic, and an older one can fail with a Python
error.

When `openocd_command` or a path-mapping destination starts with `~`, the
runner also uses remote `python3` to find the remote home directory, and a
failure is reported as `cannot resolve remote home for NAME`. If the message
ends with `timed out after 30 seconds`, see
[A preparation step times out](#a-preparation-step-times-out).

### A preparation step times out

The error names the step and its timeout:
`cannot resolve remote home for NAME: timed out after 30 seconds`,
`helper deployment timed out after 30 seconds`,
`remote OpenOCD version query timed out after 30 seconds`, or
`remote staging timed out after 60 seconds`. These limits have no
configuration setting. A timeout does not show whether the remote step
completed or was cleaned up.

**Any step.** A timeout usually means SSH could not connect or authenticate
promptly, for example because the host is unreachable or a prompt went
unanswered. Repeat the
[remote prerequisite checks](../../README.md#5-validate-the-configuration-and-remote-prerequisites)
with the executable and fixed options from `ssh_command`, and run the command
with `west -v` to see the full traceback.

**Staging over a slow link.** Map large, stable resources that already exist on
the remote host, such as an OpenOCD scripts tree, through `path_mappings` so
that they are not copied; see
[Files copied to the remote](configuration.md#files-copied-to-the-remote).

### The OpenOCD version query fails

The runner queries the remote OpenOCD version for `debug`, `attach`,
`debugserver`, and `rtt` when the build enables `CONFIG_DEBUG_THREAD_INFO`.

**`remote OpenOCD version query failed`.** Run the configured
`openocd_command` with `--version` through the configured SSH command, as in
the
[remote prerequisite checks](../../README.md#5-validate-the-configuration-and-remote-prerequisites),
and correct the executable path, permissions, fixed arguments, or remote
shared-library environment before retrying.

**`invalid remote OpenOCD version response`.** The remote helper response was
incomplete or incompatible.

**`cannot parse remote OpenOCD version`.** The reported version could not be
interpreted.

### A required file or directory is missing

**Locally.** `required file is missing: PATH` or
`required search directory is missing: PATH` means a local input that the
operation needs does not exist. The runner checks required inputs locally,
even when a path mapping covers the input.

**Remotely.** `required remote file is missing: PATH` or
`required remote directory is missing: PATH` means a mapped input does not
exist at its translated remote path; the session fails before OpenOCD starts.
Remove the mapping so that the runner stages the local input automatically, or
install the resource at the mapped remote path. See
[Path mappings](configuration.md#path-mappings).

**During staging.** Staging of a local search directory fails when the tree
contains a symbolic link that leaves the tree or forms a cycle, or another
special filesystem entry such as a device or socket. Replace or remove that
entry, or map the directory to an existing remote copy.

### Remote OpenOCD exits during startup

If the runner observes that remote OpenOCD has exited before GDB or the RTT
client starts, it does not start the client; an exit not yet observed can
surface after the client starts. Messages include:

- `STARTUP_EXIT: child exited before READY with status N`: before OpenOCD
  became ready;
- `remote OpenOCD failed with exit status N`: OpenOCD exited with a failing
  status after it became ready; the same message reports later failing exits,
  including a failed `flash`;
- `remote OpenOCD exited during startup with status N`: after OpenOCD became
  ready, while the runner set up forwarding;
- `remote OpenOCD exited before GDB startup with status N`, or
  `... before RTT startup ...` for `rtt`: after startup, before the client
  started.

Read the relayed OpenOCD output, and run the command with `west -v` to see the
effective remote OpenOCD argv; see
[Fixed OpenOCD arguments](configuration.md#fixed-openocd-arguments).

### OpenOCD cannot acquire the probe

If OpenOCD cannot acquire the requested probe or channel because another
process owns it, OpenOCD reports the failure and the operation fails rather
than being queued. The runner does not reserve probes. Retry after the other
process releases the probe or channel.

## During an operation

### A best-effort forwarding warning appears

The operation continues while its required forwards are healthy. A warning of
the form `Best-effort SERVICE forwarding PHASE failure on 127.0.0.1:PORT` names
a best-effort forward and its local port, and can be followed by diagnostic
lines. To get that forward, free the named local port, which another program
or a [retained forward](#a-retained-forward-occupies-the-local-port) may be
using, then start the operation again; read the diagnostic for other causes.

An error reporting failed forwarding rollback or resource cleanup remains
fatal even for a best-effort forward, because cleanup could not complete.
Which forwards each command requires is listed in
[Forwarded services](operations.md#forwarded-services).

### A required local port is occupied

If a required local service port is occupied and its forward cannot be
established, the operation fails rather than choosing another port, and the
error names the service and port. Stop the program using the port, or move the
service with the runner port options described in
[Forwarded services](operations.md#forwarded-services) and, for GDB,
[GDB ports](operations.md#gdb-ports). With SSH connection
sharing, the listener can be a
[forward retained from an earlier session](#a-retained-forward-occupies-the-local-port).

### A GDB client cannot connect

Connect to the local endpoint `127.0.0.1:PORT` on the machine that runs west,
not to the remote host, and check that the west process is still running.
`debugserver` prints the GDB endpoint as
`Remote OpenOCD GDB forwarding established at 127.0.0.1:PORT`. With
`--rtt-server`, the runner also prints
`Remote OpenOCD RTT forwarding established at 127.0.0.1:PORT`; that is the RTT
endpoint, not a GDB endpoint.

An established forward confirms only that SSH accepted it, not that OpenOCD is
listening behind it. `remote_openocd` does not support OpenOCD configuration
that overrides its ports or bind address: it does not detect the override and
still forwards its own ports, so the client can reach a forward with no
OpenOCD listener behind it. See
[Forwarded services](operations.md#forwarded-services).

### An allow-listed environment variable is absent

The warning `allow-listed environment variable NAME is absent; omitting it`
means `NAME` appears in `forward_env` but is not set locally. Export it before
starting west, or remove it from `forward_env` if remote OpenOCD does not need
it. Missing values are never forwarded as empty strings. Omitting a missing
local value does not unset a same-named variable already present in the remote
helper environment, so that remote value may remain available to OpenOCD. See
[Forwarded environment](configuration.md#forwarded-environment).

## Session end and connection loss

### Shutdown results

If shutdown reports a missing final result, such as
`helper shutdown did not produce SESSION_ENDED`, or times out, the runner could
not confirm the remote session outcome. This remains a failure even if the
local SSH process exits with status zero; an SSH exit status is neither an
OpenOCD result nor proof of remote cleanup.

1. Keep the diagnostic output and any reported session workspace path, such as
   the `Remote session ID OpenOCD workspace=PATH` line that `debug`, `attach`,
   `debugserver`, and `rtt` print once OpenOCD is ready and its forwards are
   set up.
2. Check that the configured SSH command supports the
   [stdin half-close](configuration.md#stdin-half-close), and inspect its
   connection and stderr diagnostics.
3. Start a new operation. The runner does not restore an interrupted GDB
   session.
4. Before removing retained workspace data, follow
   [Retained session data](#retained-session-data).

### `CONTROLLER_LEASE_LOST`

`CONTROLLER_LEASE_LOST` means the remote helper's input ended before the runner
released the session, typically because the SSH connection or a wrapper ended
remote stdin without the local runner closing it. The operation fails even if
the remote helper's final result arrives after local shutdown begins. Check the
SSH connection and confirm that `ssh_command` supports the
[stdin half-close](configuration.md#stdin-half-close).

### The SSH connection is lost

When the local SSH client reports a lost connection, the runner fails the
operation at its next status check, which can be after GDB exits. The
configured SSH client and the local operating system decide when that report
happens. To detect loss sooner, configure client-side keepalive or timeout
behavior through `ssh_command`, for example OpenSSH's `ServerAliveInterval`
option.

The remote helper detects the loss separately: it begins bounded OpenOCD
cleanup only after the remote SSH service or operating system delivers
control-channel EOF or a termination signal. Local detection does not show
when remote cleanup begins, `remote_openocd` does not bound that interval, and
client-side keepalive settings do not change remote detection timing. See
[Exit status and connection loss](operations.md#exit-status-and-connection-loss).

### Ctrl-C or a signal ends the session

A remote helper terminated by SIGINT or SIGTERM reports session failure even
when cleanup succeeds. This is separate from Ctrl-C inside interactive GDB,
which belongs to GDB; see
[Stop or interrupt an operation](operations.md#stop-or-interrupt-an-operation).

A Ctrl-C meant for interactive GDB does not end the OpenSSH connections that
the runner starts. A custom SSH client or wrapper that installs its own Ctrl-C
handling and exits on it can still end the connection; `remote_openocd` does
not guarantee that every wrapper preserves it.

## SSH connection sharing

`ControlMaster` and other SSH connection-sharing mechanisms are managed by the
SSH client, including forwards that an external sharing master keeps after
the runner exits. Runner cleanup covers the processes and remote session
resources the runner started, so a listener kept by such a master is not, by
itself, a failed runner cleanup.

### Other sessions disconnect when one session ends

This typically happens when an SSH process that the runner launched became the
sharing master. With OpenSSH `ControlMaster auto` and `ControlPersist no`, the
first SSH process that finds no master becomes one. That can be an SSH
subprocess that the runner launched for a session, so the master is no longer
external: other sessions to the same host multiplex through it. When that
session ends, its SSH process waits for the other sessions' channels, the
runner stops it after its shutdown budget, and the other sessions lose their
transport.

To avoid it, set `ControlPersist` to `yes` or a time, or establish the master
outside the runner first; see
[Connection sharing](configuration.md#connection-sharing) for the command.

### A retained forward occupies the local port

A stale forward kept by a sharing master can cause a local-port conflict, or
leave an endpoint that no longer reaches OpenOCD. This happens when the stale
forward occupies the requested local port and either:

- the remote helper cannot reuse the forward's remote address;
- the requested endpoints have changed.

Inspect and remove the stale state through the SSH client's normal controls,
then retry. The runner does not perform this cleanup automatically. For
OpenSSH, run these commands with the executable and fixed options from
`ssh_command` in place of `ssh`,
replacing `HOST` with the remote's `ssh_host`. For example:

- `ssh -O check HOST` reports whether a master is running;
- `ssh -O cancel -L 127.0.0.1:PORT:ADDRESS:PORT HOST` cancels a single
  forward, where `PORT` is the occupied local port and `ADDRESS` is the
  `bindto=ADDRESS` value in the `Remote session` line printed by the session
  that created the forward, or the `bindto` address in that session's
  `west -v` OpenOCD command line if the line was not printed;
- `ssh -O exit HOST` closes the master and every session that uses it, so use
  it only when no other active session uses that master.

### Preferred-address cache

The runner keeps a best-effort local preferred-address cache under
`~/.cache/zephyr_remote_openocd/preferred-addresses/`, keyed by the configured
SSH command prefix and host, so that later sessions can reuse retained
forwards. It saves an address only after required forwarding succeeds. On a
later session, the remote helper first tries that address, checking as usual
that no other session holds it and that its remote ports are available, and
allocates another address if it cannot be reused. Concurrent sessions never
share an address while both are active.
Deleting or losing the cache discards remembered addresses without weakening
the remote helper's checks or removing retained forwards. A later session may
then receive another address and leave a retained forward stale; see
[A retained forward occupies the local port](#a-retained-forward-occupies-the-local-port).

## Reference

### Remote session file locations

The remote helper is installed under the remote user's
`~/.local/libexec/zephyr_remote_openocd/protocol_v1/`. Session workspaces are
created under `$XDG_RUNTIME_DIR/zephyr_remote_openocd-sessions-v1/`, or under
`~/.cache/zephyr_remote_openocd/sessions-v1/` when `XDG_RUNTIME_DIR` in the
remote SSH environment does not identify an existing directory.

### Session cleanup

At the end of each session, after confirmed process and staging cleanup, the
remote helper attempts to remove the session workspace and its session
metadata. This applies on normal exit, failure, interruption, or loss of the
SSH control connection once the remote helper observes that loss. Failure to
remove any of these is reported as a cleanup failure while the connection can
still deliver the report; after SSH loss, the report may not arrive.

Orphaned session metadata older than 24 hours whose workspace is absent can be
removed during a later session's setup. This does not guarantee a maximum
retention time.

### Retained session data

If the remote helper cannot confirm that OpenOCD and staging have stopped, it
keeps the workspace inputs and reports a cleanup failure. Before removing
retained data, independently confirm that the corresponding OpenOCD processes
and staging operations have stopped. The age of the data and the exit of the
remote helper do not by themselves show that removal is safe.
