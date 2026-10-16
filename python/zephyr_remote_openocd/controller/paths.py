# SPDX-License-Identifier: Apache-2.0

"""Component-aware path mapping and per-session staging plans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.controller.staging_spec import StagedDirectory, StagedEntry, StagedFile
from zephyr_remote_openocd.protocol.arguments import ArgumentTemplate, SessionValue
from zephyr_remote_openocd.protocol.process import RequiredPathSpec

WORKSPACE_PLACEHOLDER = "{workspace}"
REMOTE_ADDRESS_PLACEHOLDER = "{address}"


class PathPlanningError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedPath:
    remote: str
    template: ArgumentTemplate | None = None


class PathPlanner:
    def __init__(self, mappings: tuple[PathMapping, ...]):
        self.mappings = tuple(
            sorted(mappings, key=lambda item: len(item.local.parts), reverse=True)
        )
        self.staged_files: list[StagedEntry] = []
        self.remote_checks: list[RequiredPathSpec] = []
        self._staged_roots: list[tuple[Path, PurePosixPath]] = []

    @staticmethod
    def _relative(path: Path, root: Path) -> Path | None:
        try:
            return path.relative_to(root)
        except ValueError:
            return None

    def _mapping(self, path: Path) -> tuple[PathMapping, Path] | None:
        for mapping in self.mappings:
            relative = self._relative(path, mapping.local)
            if relative is not None:
                return mapping, relative
        return None

    @staticmethod
    def _staged_path(destination: PurePosixPath) -> tuple[str, ArgumentTemplate]:
        template = ArgumentTemplate((SessionValue.WORKSPACE, f"/staged/{destination}"))
        return template.preview(), template

    def _existing_staged(self, path: Path) -> tuple[str, ArgumentTemplate] | None:
        candidates = []
        for root, destination in self._staged_roots:
            relative = self._relative(path, root)
            if relative is not None:
                candidates.append((len(root.parts), destination, relative))
        if not candidates:
            return None
        _, destination, relative = max(candidates, key=lambda item: item[0])
        return self._staged_path(destination.joinpath(*relative.parts))

    def plan_directory(self, source: Path, namespace: str) -> PlannedPath:
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            raise PathPlanningError(f"required search directory is missing: {source}")
        mapped = self._mapping(source)
        if mapped:
            mapping, relative = mapped
            remote = str(mapping.remote.joinpath(*relative.parts))
            self.remote_checks.append(RequiredPathSpec(remote, "directory"))
            return PlannedPath(remote)
        existing = self._existing_staged(source)
        if existing:
            return PlannedPath(*existing)
        destination = PurePosixPath("trees", namespace)
        self._staged_roots.append((source, destination))
        self._add_directory(source, destination)
        self._walk(source, source, destination, set())
        remote, template = self._staged_path(destination)
        return PlannedPath(remote, template)

    def plan_file(self, source: Path, namespace: str) -> PlannedPath:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise PathPlanningError(f"required file is missing: {source}")
        mapped = self._mapping(source)
        if mapped:
            mapping, relative = mapped
            remote = str(mapping.remote.joinpath(*relative.parts))
            self.remote_checks.append(RequiredPathSpec(remote, "file"))
            return PlannedPath(remote)
        existing = self._existing_staged(source)
        if existing:
            return PlannedPath(*existing)
        destination = PurePosixPath("files", namespace + source.suffix)
        self._add_file(source, destination)
        remote, template = self._staged_path(destination)
        return PlannedPath(remote, template)

    # Generated namespaces keep staged destinations disjoint; build_archive owns
    # manifest conflict rejection before any source is read.
    def _add_file(self, source: Path, destination: PurePosixPath) -> None:
        self.staged_files.append(StagedFile(source, destination))

    def _add_directory(self, source: Path, destination: PurePosixPath) -> None:
        self.staged_files.append(StagedDirectory(source, destination))

    def _walk(
        self, resolved: Path, root: Path, destination: PurePosixPath, stack: set[Path]
    ) -> None:
        # Callers pass the resolved root or a resolved child already checked for containment.
        if resolved in stack:
            raise PathPlanningError(f"symlink cycle in staged root {root}: {resolved}")
        next_stack = stack | {resolved}
        for child in sorted(resolved.iterdir(), key=lambda item: item.name):
            child_resolved = child.resolve()
            if child_resolved != root and root not in child_resolved.parents:
                raise PathPlanningError(f"symlink escapes staged root {root}: {child}")
            target = destination / child.name
            if child_resolved.is_dir():
                self._add_directory(child_resolved, target)
                self._walk(child_resolved, root, target, next_stack)
            elif child_resolved.is_file():
                self._add_file(child_resolved, target)
            else:
                raise PathPlanningError(f"unsupported staged path: {child}")
