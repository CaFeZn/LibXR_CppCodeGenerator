"""Discover the files that make up an HPM SDK board project."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

from .hpmpc import HpmpcData, PinmuxPeripheral, parse_hpmpc
from .pinmux_c import reconcile_pinmux_data

_SKIP_DIRECTORIES = {
    ".git",
    ".vscode",
    "build",
    "libxr",
    "node_modules",
    "out",
}
_PINMUX_DECLARATION_RE = re.compile(
    r"^\s*void\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*void\s*\)\s*;",
    re.MULTILINE,
)
_PINMUX_DEFINITION_RE = re.compile(
    r"^\s*void\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*void\s*\)\s*\{",
    re.MULTILINE,
)


class ProjectDiscoveryError(ValueError):
    """Raised when the requested HPM project files cannot be located."""


@dataclass
class HpmProject:
    """Absolute project paths plus the parsed HPM project metadata."""

    root: str
    hpmpc_path: str
    board_dir: str
    board_name: str
    board_c: str
    board_h: str
    pinmux_c: str
    pinmux_h: str
    soc_name: str
    package_name: str
    sdk_name: str
    project_name: str
    pinmux_functions: List[str]
    peripherals: List[PinmuxPeripheral]
    clock_functions: List[str]

    def to_inspect_dict(self) -> Dict[str, Any]:
        return {
            "project": {
                "board": self.board_name,
                "soc": self.soc_name,
                "package": self.package_name,
                "sdk": self.sdk_name,
                "hpmpc": _relative_posix(self.hpmpc_path, self.root),
            },
            "pinmux_functions": list(self.pinmux_functions),
            "peripherals": [peripheral.to_dict() for peripheral in self.peripherals],
            "clock_functions": list(self.clock_functions),
        }


def _should_skip_directory(name: str) -> bool:
    lower = name.lower()
    return (
        lower in _SKIP_DIRECTORIES
        or lower.startswith("build")
        or lower.startswith("hpm_sdk")
    )


def _walk_files(root: str, predicate: Callable[[str], bool]) -> List[str]:
    matches: List[str] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if not _should_skip_directory(entry.name):
                    pending.append(entry.path)
            elif entry.is_file(follow_symlinks=False) and predicate(entry.path):
                matches.append(os.path.abspath(entry.path))
    return sorted(matches, key=lambda item: item.replace("\\", "/").casefold())


def _relative_posix(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def _is_boards_path(path: str, root: str) -> bool:
    relative = Path(os.path.relpath(path, root))
    return any(part.lower() == "boards" for part in relative.parts[:-1])


def find_hpmpc_files(root: str) -> List[str]:
    """Return credential-bearing source paths without reading their contents."""

    return _walk_files(
        os.path.abspath(root), lambda path: path.lower().endswith(".hpmpc")
    )


def _find_hpmpc(root: str, configured_path: str) -> str:
    if configured_path:
        candidate = configured_path
        if not os.path.isabs(candidate):
            candidate = os.path.join(root, candidate)
        candidate = os.path.abspath(candidate)
        if os.path.isfile(candidate):
            return candidate
        raise ProjectDiscoveryError(
            "Configured HPM Pinmux Tool file does not exist: {}".format(candidate)
        )

    matches = find_hpmpc_files(root)
    for match in matches:
        if _is_boards_path(match, root):
            return match
    if matches:
        return matches[0]
    raise ProjectDiscoveryError("No .hpmpc file found under: {}".format(root))


def _find_board_directory(root: str, hpmpc_path: str) -> str:
    adjacent = os.path.dirname(hpmpc_path)
    if all(
        os.path.isfile(os.path.join(adjacent, name)) for name in ("board.c", "board.h")
    ):
        return adjacent

    board_headers = _walk_files(
        root, lambda path: os.path.basename(path).lower() == "board.h"
    )
    for board_header in board_headers:
        directory = os.path.dirname(board_header)
        if os.path.isfile(os.path.join(directory, "board.c")):
            return directory
    raise ProjectDiscoveryError(
        "No matching board.c and board.h found under: {}".format(root)
    )


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as source:
            return source.read()
    except OSError:
        return ""


def _unique_names(*groups: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for group in groups:
        for name in group:
            if name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _declared_pinmux_functions(board_dir: str) -> List[str]:
    declarations = _PINMUX_DECLARATION_RE.findall(
        _read_text(os.path.join(board_dir, "pinmux.h"))
    )
    definitions = _PINMUX_DEFINITION_RE.findall(
        _read_text(os.path.join(board_dir, "pinmux.c"))
    )
    return _unique_names(declarations, definitions)


def _project_from_data(
    root: str, hpmpc_path: str, board_dir: str, data: HpmpcData
) -> HpmProject:
    return HpmProject(
        root=root,
        hpmpc_path=hpmpc_path,
        board_dir=board_dir,
        board_name=os.path.basename(os.path.normpath(board_dir)),
        board_c=os.path.join(board_dir, "board.c"),
        board_h=os.path.join(board_dir, "board.h"),
        pinmux_c=os.path.join(board_dir, "pinmux.c"),
        pinmux_h=os.path.join(board_dir, "pinmux.h"),
        soc_name=data.soc_name,
        package_name=data.package_name,
        sdk_name=data.sdk_name,
        project_name=data.project_name,
        pinmux_functions=_unique_names(
            data.pinmux_functions, _declared_pinmux_functions(board_dir)
        ),
        peripherals=list(data.peripherals),
        clock_functions=list(data.clock_functions),
    )


def discover_project(root: str, configured_hpmpc_path: str = "") -> HpmProject:
    """Discover an HPM project using the same selection rules as the VS Code extension."""

    project_root = os.path.abspath(root)
    if not os.path.isdir(project_root):
        raise ProjectDiscoveryError(
            "HPM project directory does not exist: {}".format(project_root)
        )
    hpmpc_path = _find_hpmpc(project_root, configured_hpmpc_path)
    board_dir = _find_board_directory(project_root, hpmpc_path)
    data = reconcile_pinmux_data(
        parse_hpmpc(hpmpc_path),
        _read_text(os.path.join(board_dir, "pinmux.c")),
        _read_text(os.path.join(board_dir, "board.h")),
    )
    return _project_from_data(project_root, hpmpc_path, board_dir, data)


def inspect_project(root: str, configured_hpmpc_path: str = "") -> Dict[str, Any]:
    """Return the JSON-ready payload consumed by the future inspect command."""

    return discover_project(root, configured_hpmpc_path).to_inspect_dict()
