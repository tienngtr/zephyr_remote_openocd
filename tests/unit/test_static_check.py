# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "static_check", ROOT / "scripts/contributor/static_check.py"
)
assert SPEC is not None and SPEC.loader is not None
static_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = static_check
SPEC.loader.exec_module(static_check)


def check(tmp_path: Path, source: str, relative: str = "module.py") -> bool:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return static_check.check_path_semantics(tmp_path, (relative,))


@pytest.mark.parametrize(
    "source",
    (
        "path.resolve()\n",
        "path.resolve(strict=False)\n",
        "import os\nos.path.realpath(path)\n",
        "import os\nos.path.normpath(path)\n",
        "import os as system\nsystem.path.abspath(path)\n",
        "import os.path as osp\nosp.abspath(path)\n",
        "from os import path as osp\nosp.normpath(name)\n",
        "import posixpath\nposixpath.normpath(path)\n",
        "from os.path import normpath\nnormpath(path)\n",
        "from os.path import realpath as real\nreal(path)\n",
    ),
)
def test_lexical_path_resolution_is_rejected(tmp_path: Path, source: str) -> None:
    assert not check(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    (
        "path.resolve(strict=True)\n",
        "path.resolve(True)\n",
        "import os\nos.path.realpath(path, strict=True)\n",
        "path.absolute()\n",
        "path.resolve()  # nonstrict-path: justified\n",
        "# nonstrict-path: justified\n# continued\npath.resolve()\n",
    ),
)
def test_strict_or_justified_resolution_is_accepted(tmp_path: Path, source: str) -> None:
    assert check(tmp_path, source)


def test_justification_does_not_cover_a_later_statement(tmp_path: Path) -> None:
    assert not check(tmp_path, "# nonstrict-path: justified\nfirst = 1\npath.resolve()\n")


def test_tests_are_exempt(tmp_path: Path) -> None:
    assert check(tmp_path, "path.resolve()\n", "tests/test_module.py")
