# Built-in OpenOCD Runner Compatibility Record

This record applies the classification rules in
[SAD §16.1](SAD.md#161-classifying-built-in-runner-behavior) to the flash
options of Zephyr's built-in `openocd` runner and keeps the upstream evidence
for each classification. [REQ-FUNC-OPT-008](../requirements/SRS.md#req-func-opt-008)
remains the normative list of deliberate differences; this record explains
them. Review it whenever support for another Zephyr release is added.

Last reviewed on 2026-11-01 against Zephyr v4.4.0, v4.4.2, and v4.5.0.
Upstream code links point to release tags; history links point to commits and
pull requests in `zephyrproject-rtos/zephyr`.

## Built-in flash model

The built-in runner's `do_flash()`
([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L307-L394))
builds one fixed sequence and lets options enable phases or modify them:

```text
connect -> pre-load -> LOAD [reset-halt, erase?, load] -> VERIFY [reset-halt?, verify]
        -> post-verify -> start (reset run, or resume at the ELF entry point)
```

- `--verify-only` removes LOAD and enables VERIFY; start still runs.
- `--verify` enables VERIFY.
- `--erase` modifies LOAD only. Zephyr defines it as "mass erase flash before
  loading"
  ([`core.py`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/core.py#L648-L651)).
- The image type selects the LOAD mechanism. HEX and BIN use the board's
  `--cmd-load` command. ELF uses OpenOCD `load_image`, a generic target-memory
  write
  ([OpenOCD v0.12.0](https://github.com/openocd-org/openocd/blob/v0.12.0/src/target/target.c#L3659-L3716)),
  and has no erase step
  ([ELF branch](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L341-L355)).
- Zephyr reports `FileType.OTHER` when `--file-type` is absent and `--file` is
  either absent or has a suffix other than `.hex`, `.bin`, or `.elf`
  ([`run_common.py`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/run_common.py#L680-L704)).
  The built-in runner then uses the type of a deprecated `--use-*` option, if
  any
  ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L222-L234)).
  Otherwise it keeps `FileType.OTHER` and flashes through the HEX branch: the
  build HEX file or the given file is passed unchanged to `--cmd-load` and
  `--cmd-verify`, so that OpenOCD command determines the file format
  ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L307-L330)).
  This runner plans it the same way. In this record, HEX includes
  `FileType.OTHER`.

This model describes the built-in behavior accurately, but upstream history
shows that it arose from the order in which code was added rather than from a
decision about option combinations. No upstream issue or pull request
discussing `--erase` with `--verify-only` or with an ELF image was found.

## Classification

| Built-in behavior | Classification | Upstream evidence |
| --- | --- | --- |
| HEX/BIN `--erase` without `--cmd-erase` logs an error and exits successfully without programming | Rejected (REQ-FUNC-OPT-008) | The error is followed by a bare `return` ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L364-L367)), added with `--erase` support in [#83516](https://github.com/zephyrproject-rtos/zephyr/pull/83516); the error shows that failure was intended. |
| BIN `--verify` or `--verify-only` without `--cmd-verify` skips verification | Rejected (REQ-FUNC-OPT-008) | BIN verification is conditional on a verify command ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L378-L382)), added in [#101888](https://github.com/zephyrproject-rtos/zephyr/pull/101888). |
| `--erase` with `--verify-only` ignores `--erase` | Rejected (REQ-FUNC-OPT-008) | [#83516](https://github.com/zephyrproject-rtos/zephyr/pull/83516) placed the erase inside the existing load branch that `--verify-only` skips ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L362-L370)); the combination is not discussed. Erasing and then verifying an image that was not reprogrammed cannot succeed. |
| ELF with `--erase` ignores `--erase` | Rejected (REQ-FUNC-OPT-008) | [#83516](https://github.com/zephyrproject-rtos/zephyr/pull/83516) changed only the HEX method; the ELF path was then a separate method, described in [#35284](https://github.com/zephyrproject-rtos/zephyr/pull/35284) as "added later and is kind of a hack". [#101888](https://github.com/zephyrproject-rtos/zephyr/pull/101888) merged the methods and kept each image type's behavior. |
| `debugserver --serial` is ignored | Performed (REQ-FUNC-OPT-008) | `do_debugserver()` omits the serial command ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L499-L512), [v4.5.0-rc3](https://github.com/zephyrproject-rtos/zephyr/blob/v4.5.0-rc3/scripts/west_commands/runners/openocd.py#L543-L558)) that every other command applies. |
| `--verify` with `--verify-only` verifies once | Reproduced | The redundant request is performed. |
| `--verify-only` still resets and starts the target | Reproduced | [#35284](https://github.com/zephyrproject-rtos/zephyr/pull/35284) defines it as running `verify_image` "without load_image" ([`b652fe2`](https://github.com/zephyrproject-rtos/zephyr/commit/b652fe2a7b41fa0eeea1a2b4d0c5a396b3a259e8)), not as leaving the target untouched. |
| ELF runs `--cmd-pre-load` only when loading and `--cmd-post-verify` only when verifying; HEX/BIN always run both | Reproduced | Layering: [#46569](https://github.com/zephyrproject-rtos/zephyr/pull/46569) added the ELF hooks inside the load and verify branches; [#55734](https://github.com/zephyrproject-rtos/zephyr/pull/55734) later made HEX load and verify conditional but left its hooks unconditional. |
| HEX requires `--cmd-verify` even without verification | Reproduced | Before [#55734](https://github.com/zephyrproject-rtos/zephyr/pull/55734), HEX flashing always verified ([`fa7170b`](https://github.com/zephyrproject-rtos/zephyr/commit/fa7170b846d6c9ce518c181bb8e5c78963bc3953)). Both runners reject the request. |
| `--erase` drops a trailing ` erase` from the HEX load command, not the BIN one | Reproduced | Introduced in [#83516](https://github.com/zephyrproject-rtos/zephyr/pull/83516) and kept by [#101888](https://github.com/zephyrproject-rtos/zephyr/pull/101888) ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py#L369-L370)). |

## Upstream conventions

Upstream precedent supports performing or rejecting requests rather than
ignoring them:

- **Ignored requests are fixed.** HEX `--verify-only` was ignored until
  [#55734](https://github.com/zephyrproject-rtos/zephyr/pull/55734); bossac
  `--erase` was ignored until
  [#81894](https://github.com/zephyrproject-rtos/zephyr/pull/81894). Both were
  fixed by performing the request.
- **Conflicting erase requests are rejected.** The nRF runners reject
  `--erase` with `--erase-mode` or `--ext-erase-mode`
  ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/nrf_common.py#L489-L495)).
  They only warn when a modifier has nothing to act on because of the image
  contents
  ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/nrf_common.py#L391-L393)).
- **Board defaults are overridable.** `--erase` accepts `--no-erase`
  ([`core.py`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/core.py#L648-L651)),
  and the image-type options were kept "last flag wins" in
  [#101888](https://github.com/zephyrproject-rtos/zephyr/pull/101888) so that
  a user can override a `board.cmake` default.

## ELF image path

The ELF path was added to load and run an image "not from flash but from RAM"
([`83b346e`](https://github.com/zephyrproject-rtos/zephyr/commit/83b346edefc37615d241de7f5b807b9bf96201d1),
[#22602](https://github.com/zephyrproject-rtos/zephyr/pull/22602)). It issues
`load_image`, not OpenOCD's flash-programming command `flash write_image`, so
whether an ELF load reaches flash depends on the target and the OpenOCD build.

- **Selection.** Since
  [#102590](https://github.com/zephyrproject-rtos/zephyr/pull/102590) (Zephyr
  4.4), `--file-type=elf` replaces the deprecated `--use-elf`, and a `--file`
  path ending in `.elf` selects ELF without either option
  ([`run_common.py`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/run_common.py#L680-L704)).
  In other runners the image type is only a file format: JLink, for example,
  erases before programming whichever image it selects, including the build
  ELF
  ([v4.4.0](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/jlink.py#L482-L539)).
- **In-tree boards (v4.4.0).** 32 boards select ELF for `openocd`, 30 of them
  with `--file-type=elf`, including flash-based microcontrollers such as
  [`wch/ch32v003evt`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/boards/wch/ch32v003evt/board.cmake).
  None defaults to `--erase`.
  [`openhwgroup/cv32a6_genesys_2`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/boards/openhwgroup/cv32a6_genesys_2/board.cmake)
  and `cv64a6_genesys_2` default to `--verify`.
- **Later boards.**
  [`ti/lp_am13e230`](https://github.com/zephyrproject-rtos/zephyr/blob/v4.5.0-rc3/boards/ti/lp_am13e230/board.cmake)
  selects ELF only when `CONFIG_XIP` is disabled, which matches the original
  RAM-load purpose.

## Zephyr release review

| Release | Flash erase and verify logic | Other changes affecting compatibility |
| --- | --- | --- |
| v4.4.0 | Baseline for this record. | None. |
| v4.4.1, v4.4.2 | `openocd.py` identical to v4.4.0. | None. |
| v4.5.0 | Unchanged. | `--cmd-pre-init-flash` replaces `--cmd-pre-init` for flash ([#101910](https://github.com/zephyrproject-rtos/zephyr/pull/101910), [code](https://github.com/zephyrproject-rtos/zephyr/blob/v4.5.0/scripts/west_commands/runners/openocd.py#L381)); this is board recipe and must be reproduced. `--serial` becomes a deprecated alias for `-i/--dev-id` ([#111906](https://github.com/zephyrproject-rtos/zephyr/pull/111906), [code](https://github.com/zephyrproject-rtos/zephyr/blob/v4.5.0/scripts/west_commands/runners/openocd.py#L160-L162)); `debugserver` still omits it. |

The final v4.5.0 source preserves these flash behaviors. Its additional debug
command phases, RTOS eligibility, and interactive-client API are adapted as
described in [SAD §16](SAD.md#16-zephyr-runner-reuse-strategy). Its `--log-file`
option is explicitly rejected under REQ-FUNC-OPT-008 pending separate logging
support. Release candidates and development versions remain unsupported.
