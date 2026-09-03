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
profile/capability nodes before running destructive tests. The strict
native-Linux release driver runs these layers serially and fails on missing
mandatory evidence. Supply the inventory and Zephyr prerequisites:

```sh
python3 scripts/release_validate.py \
  --hardware-config /path/to/hardware.toml \
  --zephyr-base /path/to/zephyr \
  --west /path/to/west \
  --board <board>
```

PG-012 and PG-013 remain explicitly reported as deferred until a real WSL2
environment is available. Add the benchmark arguments to the same command for
release evidence; the benchmark remains a manual, non-CI timing gate.

Run static checks for changed Python with `ruff check .` and
`ruff format --check .`; finish changes with `git diff --check`.
