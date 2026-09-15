# SPDX-License-Identifier: Apache-2.0

"""Write actionable code-complexity metrics for an exact Git revision."""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import quote

from radon.complexity import cc_rank, cc_visit
from radon.metrics import h_visit, mi_visit

PRODUCTION_PREFIXES = ("python/", "runners/", "scripts/", "tools/")
TEST_PREFIXES = ("tests/",)
DETAIL_LIMIT = 20
WORKING_TREE = "working tree"


@dataclass(frozen=True)
class BlockMetrics:
    path: str
    kind: str
    name: str
    line: int
    complexity: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.path, self.kind, self.name

    @property
    def rank(self) -> str:
        return cc_rank(self.complexity)


@dataclass(frozen=True)
class FileMetrics:
    path: str
    maintainability: float
    effort: float


@dataclass(frozen=True)
class ScopeMetrics:
    blocks: tuple[BlockMetrics, ...]
    files: tuple[FileMetrics, ...]

    @property
    def average_complexity(self) -> float:
        return mean(item.complexity for item in self.blocks)

    @property
    def maximum_complexity(self) -> int:
        return max(item.complexity for item in self.blocks)

    @property
    def concerning_blocks(self) -> int:
        return sum(item.complexity >= 11 for item in self.blocks)

    @property
    def average_maintainability(self) -> float:
        return mean(item.maintainability for item in self.files)

    @property
    def total_effort(self) -> float:
        return sum(item.effort for item in self.files)


def repository_root() -> Path:
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def revision_sources(root: Path, revision: str) -> dict[str, str]:
    """Read Python sources from a revision without modifying the worktree."""
    result = subprocess.run(
        (
            "git",
            "archive",
            "--format=tar",
            revision,
            "--",
            "python",
            "runners",
            "scripts",
            "tools",
            "tests",
        ),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"cannot read Git revision {revision!r}: {detail}")
    sources = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            sources[member.name] = stream.read().decode("utf-8")
    return sources


def working_tree_sources(root: Path) -> dict[str, str]:
    """Read tracked and untracked Python sources from the working tree."""
    result = subprocess.run(
        (
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "python",
            "runners",
            "scripts",
            "tools",
            "tests",
        ),
        cwd=root,
        check=True,
        capture_output=True,
    )
    sources = {}
    for encoded_path in result.stdout.split(b"\0"):
        path = os.fsdecode(encoded_path)
        source_path = root / path
        if path.endswith(".py") and source_path.is_file():
            sources[path] = source_path.read_text(encoding="utf-8")
    return sources


def analyze_sources(sources: dict[str, str], prefixes: tuple[str, ...]) -> ScopeMetrics:
    is_test_scope = prefixes == TEST_PREFIXES
    blocks = []
    files = []
    for path, source in sorted(sources.items()):
        if not path.startswith(prefixes):
            continue
        for block in cc_visit(source, no_assert=is_test_scope):
            blocks.append(
                BlockMetrics(
                    path,
                    block.letter,
                    block.fullname,
                    block.lineno,
                    block.complexity,
                )
            )
        files.append(
            FileMetrics(
                path,
                mi_visit(source, multi=True),
                h_visit(source).total.effort,
            )
        )
    if not blocks or not files:
        raise RuntimeError(f"no Python sources found for {prefixes!r}")
    return ScopeMetrics(tuple(blocks), tuple(files))


def analyze_revision(root: Path, revision: str) -> dict[str, ScopeMetrics]:
    sources = revision_sources(root, revision)
    return {
        "Production": analyze_sources(sources, PRODUCTION_PREFIXES),
        "Tests": analyze_sources(sources, TEST_PREFIXES),
    }


def analyze_working_tree(root: Path) -> dict[str, ScopeMetrics]:
    sources = working_tree_sources(root)
    return {
        "Production": analyze_sources(sources, PRODUCTION_PREFIXES),
        "Tests": analyze_sources(sources, TEST_PREFIXES),
    }


def _number(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def _delta(value: float, digits: int = 2) -> str:
    return f"{value:+,.{digits}f}"


def _location(path: str, line: int | None, revision: str) -> str:
    label = f"{path}:{line}" if line is not None else path
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not server or not repository or revision == WORKING_TREE:
        return f"`{label}`"
    url = f"{server}/{repository}/blob/{revision}/{quote(path, safe='/')}"
    if line is not None:
        url += f"#L{line}"
    return f"[{label}]({url})"


def _aggregate_rows(metrics: dict[str, ScopeMetrics]) -> list[str]:
    rows = []
    for scope, item in metrics.items():
        rows.append(
            f"| {scope} | {_number(item.average_complexity)} | "
            f"{item.maximum_complexity} | {item.concerning_blocks} | "
            f"{_number(item.average_maintainability)} | {_number(item.total_effort, 0)} |"
        )
    return rows


def _comparison_rows(base: dict[str, ScopeMetrics], current: dict[str, ScopeMetrics]) -> list[str]:
    rows = []
    attributes = (
        ("Average CC", "average_complexity", 2),
        ("Maximum CC", "maximum_complexity", 0),
        ("C-F blocks", "concerning_blocks", 0),
        ("Average MI", "average_maintainability", 2),
        ("Halstead effort", "total_effort", 0),
    )
    for scope in current:
        for label, attribute, digits in attributes:
            before = float(getattr(base[scope], attribute))
            after = float(getattr(current[scope], attribute))
            rows.append(
                f"| {scope} | {label} | {_number(before, digits)} | "
                f"{_number(after, digits)} | {_delta(after - before, digits)} |"
            )
    return rows


def _block_map(metrics: ScopeMetrics) -> dict[tuple[str, str, str], BlockMetrics]:
    return {item.identity: item for item in metrics.blocks}


def _changed_blocks(
    base: dict[str, ScopeMetrics], current: dict[str, ScopeMetrics]
) -> list[tuple[str, BlockMetrics | None, BlockMetrics | None]]:
    changes = []
    for scope in current:
        before = _block_map(base[scope])
        after = _block_map(current[scope])
        for identity in before.keys() | after.keys():
            old = before.get(identity)
            new = after.get(identity)
            if old is None or new is None or old.complexity != new.complexity:
                changes.append((scope, old, new))

    def sort_key(item):
        old, new = item[1:]
        point = new or old
        assert point is not None
        delta = (new.complexity if new else 0) - (old.complexity if old else 0)
        return -delta, -(new.complexity if new else 0), point.path, point.name

    return sorted(changes, key=sort_key)


def _changed_files(
    base: dict[str, ScopeMetrics], current: dict[str, ScopeMetrics]
) -> list[tuple[str, FileMetrics | None, FileMetrics | None]]:
    changes = []
    for scope in current:
        before = {item.path: item for item in base[scope].files}
        after = {item.path: item for item in current[scope].files}
        for path in before.keys() | after.keys():
            old = before.get(path)
            new = after.get(path)
            if (
                old is None
                or new is None
                or (old.maintainability != new.maintainability or old.effort != new.effort)
            ):
                changes.append((scope, old, new))

    def sort_key(item):
        old, new = item[1:]
        point = new or old
        assert point is not None
        mi_delta = (new.maintainability if new else 0) - (old.maintainability if old else 0)
        effort_delta = (new.effort if new else 0) - (old.effort if old else 0)
        return mi_delta, -effort_delta, point.path

    return sorted(changes, key=sort_key)


def _limited(items: list[Any]) -> tuple[list[Any], int]:
    return items[:DETAIL_LIMIT], max(0, len(items) - DETAIL_LIMIT)


def _current_hotspots(current: dict[str, ScopeMetrics], revision: str) -> list[str]:
    blocks = sorted(
        (
            (scope, block)
            for scope, metrics in current.items()
            for block in metrics.blocks
            if block.complexity >= 11
        ),
        key=lambda item: (-item[1].complexity, item[1].path, item[1].name),
    )
    selected, omitted = _limited(blocks)
    lines = [
        "### Current C-F callable hotspots",
        "",
        "Includes callables with CC 11 or higher; ordered by higher CC, then path and name.",
        "",
        "| Scope | Point | Callable | CC |",
        "|---|---|---|---:|",
    ]
    for scope, block in selected:
        lines.append(
            f"| {scope} | {_location(block.path, block.line, revision)} | "
            f"`{block.name}` | {block.rank} ({block.complexity}) |"
        )
    if not selected:
        lines.append("| - | - | No C-F callables | - |")
    if omitted:
        lines.extend(("", f"_{omitted} additional callable hotspots omitted._"))
    return lines


def _file_hotspots(current: dict[str, ScopeMetrics], revision: str) -> list[str]:
    selected: set[tuple[str, str]] = set()
    for scope, metrics in current.items():
        selected.update(
            (scope, item.path)
            for item in sorted(metrics.files, key=lambda item: item.maintainability)[:5]
        )
        selected.update(
            (scope, item.path) for item in sorted(metrics.files, key=lambda item: -item.effort)[:5]
        )
    rows = []
    for scope, path in selected:
        item = next(entry for entry in current[scope].files if entry.path == path)
        rows.append((scope, item))
    rows.sort(key=lambda entry: (entry[1].maintainability, -entry[1].effort, entry[1].path))
    lines = [
        "### Current file hotspots",
        "",
        "Includes the five lowest-MI and five highest-effort files in each scope; "
        "ordered by lower MI, then higher Halstead effort, then path.",
        "",
        "| Scope | File | MI | Halstead effort |",
        "|---|---|---:|---:|",
    ]
    for scope, item in rows:
        lines.append(
            f"| {scope} | {_location(item.path, None, revision)} | "
            f"{_number(item.maintainability)} | {_number(item.effort, 0)} |"
        )
    return lines


def render_push(current: dict[str, ScopeMetrics], revision: str) -> str:
    lines = [
        "## Code complexity metrics",
        "",
        f"Tip revision: `{revision}`",
        "",
        "| Scope | Average CC | Maximum CC | C-F blocks | Average MI | Halstead effort |",
        "|---|---:|---:|---:|---:|---:|",
        *_aggregate_rows(current),
        "",
        *_current_hotspots(current, revision),
        "",
        *_file_hotspots(current, revision),
    ]
    return "\n".join(lines) + "\n"


def _render_block_changes(
    base: dict[str, ScopeMetrics],
    current: dict[str, ScopeMetrics],
    base_revision: str,
    revision: str,
) -> list[str]:
    lines = [
        "### Callable complexity changes",
        "",
        "| Scope | Point | Callable | Base | Current | Δ |",
        "|---|---|---|---:|---:|---:|",
    ]
    changes, omitted = _limited(_changed_blocks(base, current))
    for scope, old, new in changes:
        point = new or old
        assert point is not None
        point_revision = revision if new else base_revision
        old_value = f"{old.rank} ({old.complexity})" if old else "-"
        new_value = f"{new.rank} ({new.complexity})" if new else "-"
        delta = (new.complexity if new else 0) - (old.complexity if old else 0)
        lines.append(
            f"| {scope} | {_location(point.path, point.line, point_revision)} | "
            f"`{point.name}` | {old_value} | {new_value} | {delta:+d} |"
        )
    if not changes:
        lines.append("| - | - | No callable CC changes | - | - | - |")
    if omitted:
        lines.extend(("", f"_{omitted} additional callable changes omitted._"))
    return lines


def _render_file_changes(
    base: dict[str, ScopeMetrics],
    current: dict[str, ScopeMetrics],
    base_revision: str,
    revision: str,
) -> list[str]:
    lines = [
        "### File metric changes",
        "",
        "| Scope | File | Base MI | Current MI | Δ MI | Base effort | Current effort | Δ effort |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    changes, omitted = _limited(_changed_files(base, current))
    for scope, old, new in changes:
        point = new or old
        assert point is not None
        point_revision = revision if new else base_revision
        old_mi = old.maintainability if old else 0.0
        new_mi = new.maintainability if new else 0.0
        old_effort = old.effort if old else 0.0
        new_effort = new.effort if new else 0.0
        lines.append(
            f"| {scope} | {_location(point.path, None, point_revision)} | "
            f"{_number(old_mi)} | {_number(new_mi)} | {_delta(new_mi - old_mi)} | "
            f"{_number(old_effort, 0)} | {_number(new_effort, 0)} | "
            f"{_delta(new_effort - old_effort, 0)} |"
        )
    if not changes:
        lines.append("| - | No file metric changes | - | - | - | - | - | - |")
    if omitted:
        lines.extend(("", f"_{omitted} additional file changes omitted._"))
    return lines


def render_comparison(
    base: dict[str, ScopeMetrics],
    current: dict[str, ScopeMetrics],
    base_revision: str,
    revision: str,
) -> str:
    lines = [
        "## Code complexity changes",
        "",
        f"Base `{base_revision}` → tested revision `{revision}`",
        "",
        "| Scope | Metric | Base | Current | Δ |",
        "|---|---|---:|---:|---:|",
        *_comparison_rows(base, current),
        "",
        *_render_block_changes(base, current, base_revision, revision),
        "",
        *_render_file_changes(base, current, base_revision, revision),
        "",
        *_current_hotspots(current, revision),
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--revision", default="HEAD", help="Git revision to report (default: HEAD)")
    source.add_argument(
        "--working-tree",
        action="store_true",
        help="report tracked and untracked working-tree sources",
    )
    parser.add_argument("--base-revision", help="Git revision used as the comparison base")
    parser.add_argument("--output", type=Path, help="append Markdown to this file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = repository_root()
    revision = WORKING_TREE if args.working_tree else args.revision
    current = analyze_working_tree(root) if args.working_tree else analyze_revision(root, revision)
    if args.base_revision:
        base = analyze_revision(root, args.base_revision)
        report = render_comparison(base, current, args.base_revision, revision)
    else:
        report = render_push(current, revision)
    if args.output:
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(report)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
