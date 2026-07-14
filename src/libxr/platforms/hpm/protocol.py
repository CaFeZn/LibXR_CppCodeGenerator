"""Versioned JSON envelopes for the headless HPM CLI."""

import json
from importlib import metadata as importlib_metadata
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .diagnostics import Diagnostic, diagnostics_to_dicts

PROTOCOL_VERSION = 1
GENERATOR_DISTRIBUTION = "libxr"
UNKNOWN_GENERATOR_VERSION = "unknown"


def generator_version() -> str:
    """Return the installed generator version without requiring package metadata."""

    try:
        value = importlib_metadata.version(GENERATOR_DISTRIBUTION)
    except Exception:
        return UNKNOWN_GENERATOR_VERSION
    return (
        value if isinstance(value, str) and value.strip() else UNKNOWN_GENERATOR_VERSION
    )


def base_envelope() -> Dict[str, Any]:
    """Create the common prefix shared by every HPM JSON response."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "generator_version": generator_version(),
    }


def inspect_envelope(
    project: Mapping[str, Any],
    pinmux_functions: Sequence[str],
    peripherals: Iterable[Mapping[str, Any]],
    *,
    clock_functions: Optional[Sequence[str]] = None,
    clock_sources: Optional[Iterable[Mapping[str, Any]]] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    errors: Iterable[Diagnostic] = (),
    warnings: Iterable[Diagnostic] = (),
) -> Dict[str, Any]:
    """Build an ``inspect`` response with a stable JSON-compatible shape."""

    envelope = base_envelope()
    envelope.update(
        {
            "project": dict(project),
            "pinmux_functions": list(pinmux_functions),
            "peripherals": [dict(peripheral) for peripheral in peripherals],
            "clock_functions": [] if clock_functions is None else list(clock_functions),
            "clock_sources": (
                []
                if clock_sources is None
                else [dict(source) for source in clock_sources]
            ),
            "capabilities": {} if capabilities is None else dict(capabilities),
            "errors": diagnostics_to_dicts(errors),
            "warnings": diagnostics_to_dicts(warnings),
        }
    )
    return envelope


def validate_envelope(
    valid: bool,
    normalized_config: Mapping[str, Any],
    *,
    errors: Iterable[Diagnostic] = (),
    warnings: Iterable[Diagnostic] = (),
) -> Dict[str, Any]:
    """Build a ``validate`` response with structured diagnostics."""

    envelope = base_envelope()
    envelope.update(
        {
            "valid": bool(valid),
            "normalized_config": dict(normalized_config),
            "errors": diagnostics_to_dicts(errors),
            "warnings": diagnostics_to_dicts(warnings),
        }
    )
    return envelope


def generate_envelope(
    success: bool,
    generated_files: Sequence[str],
    *,
    errors: Iterable[Diagnostic] = (),
    warnings: Iterable[Diagnostic] = (),
) -> Dict[str, Any]:
    """Build a ``generate`` response with generated workspace paths."""

    envelope = base_envelope()
    envelope.update(
        {
            "success": bool(success),
            "generated_files": list(generated_files),
            "errors": diagnostics_to_dicts(errors),
            "warnings": diagnostics_to_dicts(warnings),
        }
    )
    return envelope


def dumps_json(value: Mapping[str, Any], *, pretty: bool = False) -> str:
    """Serialize a protocol value without writing to stdout or stderr."""

    if pretty:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
