"""Component-aware path mapping and per-session staging plans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from zephyr_remote_openocd.config import PathMapping

from .model import RemotePathCheck, StagedFile

WORKSPACE_TOKEN = "{workspace}"
ADDRESS_TOKEN = "{address}"


class PathPlanningError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedPath:
    local: Path
    remote: str
    kind: str
    mapped: bool


class PathPlanner:
    def __init__(self, mappings: tuple[PathMapping, ...]):
        self.mappings = tuple(sorted(mappings, key=lambda item: len(item.local.parts), reverse=True))
        self.staged_files: list[StagedFile] = []
        self.remote_checks: list[RemotePathCheck] = []
        self._staged_roots: list[tuple[Path, PurePosixPath]] = []
        self._destinations: set[PurePosixPath] = set()

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

    def _existing_staged(self, path: Path) -> str | None:
        candidates = []
        for root, destination in self._staged_roots:
            relative = self._relative(path, root)
            if relative is not None:
                candidates.append((len(root.parts), destination, relative))
        if not candidates:
            return None
        _, destination, relative = max(candidates, key=lambda item: item[0])
        return f"{WORKSPACE_TOKEN}/staged/{destination.joinpath(*relative.parts)}"

    def plan_directory(self, source: Path, namespace: str) -> PlannedPath:
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            raise PathPlanningError(f"required search directory is missing: {source}")
        mapped = self._mapping(source)
        if mapped:
            mapping, relative = mapped
            remote = str(mapping.remote.joinpath(*relative.parts))
            self.remote_checks.append(RemotePathCheck(remote, "directory"))
            return PlannedPath(source, remote, "directory", True)
        existing = self._existing_staged(source)
        if existing:
            return PlannedPath(source, existing, "directory", False)
        destination = PurePosixPath("trees", namespace)
        self._staged_roots.append((source, destination))
        self._walk(source, source, destination, set())
        return PlannedPath(source, f"{WORKSPACE_TOKEN}/staged/{destination}", "directory", False)

    def plan_file(self, source: Path, namespace: str) -> PlannedPath:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise PathPlanningError(f"required file is missing: {source}")
        mapped = self._mapping(source)
        if mapped:
            mapping, relative = mapped
            remote = str(mapping.remote.joinpath(*relative.parts))
            self.remote_checks.append(RemotePathCheck(remote, "file"))
            return PlannedPath(source, remote, "file", True)
        existing = self._existing_staged(source)
        if existing:
            return PlannedPath(source, existing, "file", False)
        destination = PurePosixPath("files", namespace + source.suffix)
        self._add_file(source, destination)
        return PlannedPath(source, f"{WORKSPACE_TOKEN}/staged/{destination}", "file", False)

    def _add_file(self, source: Path, destination: PurePosixPath) -> None:
        if destination in self._destinations:
            raise PathPlanningError(f"duplicate staged destination: {destination}")
        self._destinations.add(destination)
        self.staged_files.append(StagedFile(source, destination))

    def _walk(self, physical: Path, root: Path, destination: PurePosixPath, stack: set[Path]) -> None:
        resolved = physical.resolve()
        if resolved != root and root not in resolved.parents:
            raise PathPlanningError(f"symlink escapes staged root {root}: {physical}")
        if resolved in stack:
            raise PathPlanningError(f"symlink cycle in staged root {root}: {physical}")
        next_stack = stack | {resolved}
        for child in sorted(physical.iterdir(), key=lambda item: item.name):
            child_resolved = child.resolve()
            if child_resolved != root and root not in child_resolved.parents:
                raise PathPlanningError(f"symlink escapes staged root {root}: {child}")
            target = destination / child.name
            if child_resolved.is_dir():
                self._walk(child_resolved, root, target, next_stack)
            elif child_resolved.is_file():
                self._add_file(child_resolved, target)
            else:
                raise PathPlanningError(f"unsupported staged path: {child}")
