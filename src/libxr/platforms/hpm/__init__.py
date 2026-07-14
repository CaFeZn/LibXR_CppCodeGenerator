"""Headless HPM project discovery and Pinmux Tool parsing."""

from .hpmpc import HpmpcData, HpmpcParseError, PinmuxPeripheral, parse_hpmpc
from .project import (
    HpmProject,
    ProjectDiscoveryError,
    discover_project,
    find_hpmpc_files,
    inspect_project,
)

__all__ = [
    "HpmProject",
    "HpmpcData",
    "HpmpcParseError",
    "PinmuxPeripheral",
    "ProjectDiscoveryError",
    "discover_project",
    "find_hpmpc_files",
    "inspect_project",
    "parse_hpmpc",
]
