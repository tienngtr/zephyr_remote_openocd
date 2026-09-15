# SPDX-License-Identifier: Apache-2.0

"""Write a GitHub-linked Markdown summary from Coverage.py data."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


def coverage_markdown(root: Path) -> str:
    result = subprocess.run(
        (sys.executable, "-m", "coverage", "report", "--format=markdown"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _source_path(markdown_name: str) -> str:
    return re.sub(r"\\(.)", r"\1", markdown_name)


def link_file_rows(report: str, revision: str) -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not server or not repository:
        return report

    lines = []
    for line in report.splitlines():
        columns = line.split("|")
        if len(columns) < 4:
            lines.append(line)
            continue
        name = columns[1].strip()
        if name in {"Name", "TOTAL"} or name.startswith("-"):
            lines.append(line)
            continue
        path = _source_path(name)
        url = f"{server}/{repository}/blob/{revision}/{quote(path, safe='/')}"
        columns[1] = f" [{name}]({url}) "
        lines.append("|".join(columns))
    return "\n".join(lines) + "\n"


def repository_root() -> Path:
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--revision",
        default="HEAD",
        help="Git revision linked by the report (default: HEAD)",
    )
    parser.add_argument("--output", type=Path, help="append Markdown to this file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = link_file_rows(coverage_markdown(repository_root()), args.revision)
    summary = "## Coverage\n\n" + report
    if args.output:
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(summary)
    else:
        sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
