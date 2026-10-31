# SPDX-License-Identifier: Apache-2.0

"""Fault the source-authority boundary around real upstream runner objects."""

from __future__ import annotations

import argparse
import io
import os
import runpy
import sys
from collections.abc import Sequence
from contextlib import redirect_stderr
from importlib import import_module
from importlib.abc import MetaPathFinder
from pathlib import Path
from types import ModuleType


class FailedImport(MetaPathFinder):
    """Make the selected upstream import boundary unavailable."""

    def __init__(self, prefix: str, error: ImportError | AttributeError) -> None:
        self.prefix = prefix
        self.error = error

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> None:
        if fullname.startswith(self.prefix):
            raise self.error


def main() -> None:
    source, module, fault = sys.argv[1:]
    os.environ["ZEPHYR_BASE"] = source
    sys.path.insert(0, str(Path(source) / "scripts/west_commands"))
    core = import_module("runners.core")
    runners = import_module("runners")
    stock = runners.get_runner_cls("openocd")
    entry = str(Path(module) / "runners/remote_openocd.py")

    if fault in {"adapter-import-error", "adapter-attribute-error"}:
        error = (
            ImportError("supported adapter import failed")
            if fault == "adapter-import-error"
            else AttributeError("supported adapter attribute failed")
        )
        sys.meta_path.insert(0, FailedImport("zephyr_remote_openocd.zephyr", error))
        try:
            runpy.run_path(entry)
        except (ImportError, AttributeError) as caught:
            assert caught is error
        else:
            raise AssertionError("supported adapter failure was hidden")
        return

    # West loads stock runners before external modules. Remove only ZRO's
    # source-authority dependency afterward; stock objects remain real and keep
    # their already-imported upstream state, as in the external-import sequence.
    if fault == "missing-source-module":
        sys.modules.pop("zephyr_ext_common")
        sys.meta_path.insert(
            0,
            FailedImport(
                "zephyr_ext_common",
                ModuleNotFoundError(
                    "No module named 'zephyr_ext_common'", name="zephyr_ext_common"
                ),
            ),
        )
    else:
        assert fault == "missing-source-export"
        delattr(sys.modules["zephyr_ext_common"], "ZEPHYR_BASE")

    registration = runpy.run_path(entry)
    selected = registration["RemoteOpenOcdBinaryRunner"]
    assert runners.get_runner_cls("remote_openocd") is selected
    assert not any(name.startswith("zephyr_remote_openocd.zephyr") for name in sys.modules)
    assert runners.get_runner_cls("openocd") is stock
    parser = argparse.ArgumentParser()
    stock.add_parser(parser)
    cfg = core.RunnerConfig(
        build_dir=module,
        board_dir=module,
        elf_file=None,
        exe_file=None,
        hex_file=None,
        bin_file=None,
        uf2_file=None,
        mot_file=None,
        file=None,
    )
    assert isinstance(stock.create(cfg, parser.parse_args([])), stock)

    diagnostic = io.StringIO()
    with redirect_stderr(diagnostic):
        try:
            selected.add_parser(argparse.ArgumentParser())
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("unavailable remote runner accepted selection")
    assert "cannot determine active Zephyr source" in diagnostic.getvalue()
    assert "zephyr_ext_common.ZEPHYR_BASE" in diagnostic.getvalue()


if __name__ == "__main__":
    main()
