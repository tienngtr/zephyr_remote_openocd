# Test layers

The maintained tests use pytest. A plain invocation is deliberately
self-contained:

```sh
pytest
```

This runs unit and local-process coverage only. Select external layers
explicitly:

```sh
pytest tests/zephyr_integration -m zephyr
pytest tests/ssh_integration -m ssh --hardware-config /path/to/hardware.toml
pytest tests/hardware -m hardware --hardware-config /path/to/hardware.toml
```

The TOML inventory is test-only and must stay outside the Git tree. Copy the
neutral template at `tests/fixtures/hardware.example.toml`, replace its
placeholders, and pass the resulting path with `--hardware-config` (or set
`ZRO_HARDWARE_CONFIG`). See
[`docs/development/hardware-fixtures.md`](../docs/development/hardware-fixtures.md)
for the schema, capability selection, build recipes, serial framing, and
cleanup expectations. Use `--collect-only` before destructive runs to inspect
the generated profile nodes.

Zephyr integration builds the applications declared by the inventory and
includes the Git-free clean-install acceptance. SSH and hardware tests use the
same host/product settings generated from that inventory; unavailable
infrastructure is skipped in normal runs and is an error for strict release
validation.

The startup-overhead benchmark is a manual release-validation command, not
pytest discovery. Its procedure and conservative baseline interpretation are
documented in [`docs/validation/v1.md`](../docs/validation/v1.md).
