# Testing layers

The maintained suite uses pytest. Plain `pytest` runs only hardware-free unit
and local-process tests. External layers are selected explicitly so a normal
contributor run never needs SSH, a Zephyr checkout, or lab hardware:

```sh
pytest                         # unit + local integration
pytest tests/zephyr_integration -m zephyr
pytest tests/ssh_integration -m ssh --hardware-config /path/to/hardware.toml
pytest tests/hardware -m hardware --hardware-config /path/to/hardware.toml
```

The ignored TOML inventory and its selection rules are documented in
[`hardware-fixtures.md`](hardware-fixtures.md). Use `--collect-only` to inspect
profile/capability nodes before running destructive tests. The strict release
driver will be added after the external pytest migration; it must report
missing prerequisites rather than silently treating them as evidence.

Run static checks for changed Python with `ruff check .` and
`ruff format --check .`; finish changes with `git diff --check`.
