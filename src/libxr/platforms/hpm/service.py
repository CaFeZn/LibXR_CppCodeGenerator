"""Application services for headless HPM project inspection and generation."""

import os
import shutil
import stat
import tempfile
from typing import Any, Dict, List

import yaml

from libxr.GeneratorCodeHPM import generate_code
from libxr.PeripheralAnalyzerHPM import parse_hpmpc_file

from .board_generator import (
    BoardFileError,
    BoardGenerationError,
    MalformedCFunctionError,
    MissingBoardHelperError,
    MissingClockFunctionError,
    UnmanagedCanHelperError,
    UnsupportedClockSourceError,
    render_board_files,
)
from .clock import clock_sources_for_soc
from .config import (
    ConfigFileError,
    capabilities_for_project,
    read_config,
    selected_peripheral_instances,
    serialize_config,
    validate_config,
    write_config,
)
from .diagnostics import Diagnostic, ERROR
from .libxr_config import (
    LibxrConfigError,
    merge_libxr_config,
    read_libxr_config,
    serialize_libxr_config,
)
from .project import discover_project
from .protocol import generate_envelope, inspect_envelope, validate_envelope
from .markers import MarkerConflictError, detect_newline

_APP_MAIN_HEADER = """#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void app_main(void);

#ifdef __cplusplus
}
#endif
"""


class GenerationPathError(ValueError):
    """Raised before rendering when a generated path is unsafe or ambiguous."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GenerationCommitError(OSError):
    """Raised when a multi-file generation transaction cannot be committed."""


def inspect(directory: str, hpmpc_path: str = "") -> Dict[str, Any]:
    """Inspect an HPM project without modifying it."""
    project = discover_project(directory, hpmpc_path)
    result = project.to_inspect_dict()
    return inspect_envelope(
        result["project"],
        result["pinmux_functions"],
        result["peripherals"],
        clock_functions=result["clock_functions"],
        clock_sources=clock_sources_for_soc(project.soc_name, project.board_name),
        capabilities=capabilities_for_project(project),
    )


def validate(
    directory: str,
    hpmpc_path: str = "",
    peripheral_config_path: str = "hpm_peripherals.yaml",
    candidate: Any = None,
    write: bool = False,
) -> Dict[str, Any]:
    """Normalize and validate an HPM peripheral configuration."""
    project = discover_project(directory, hpmpc_path)
    config_path = peripheral_config_path
    if not os.path.isabs(config_path):
        config_path = os.path.join(project.root, config_path)
    source = candidate
    if source is None and os.path.isfile(config_path):
        source = read_config(config_path)
    result = validate_config(project, source)
    if write:
        write_config(config_path, result.normalized_config)
    return validate_envelope(
        result.valid,
        result.normalized_config,
        errors=result.errors,
        warnings=result.warnings,
    )


def _resolve_path(root: str, value: str, default: str) -> str:
    selected = value or default
    return os.path.abspath(
        selected if os.path.isabs(selected) else os.path.join(root, selected)
    )


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _require_inside_project(path: str, root: str) -> None:
    try:
        inside = os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(
            root
        )
    except ValueError:
        inside = False
    if not inside:
        raise GenerationPathError(
            "HPM_OUTPUT_PATH_OUTSIDE_PROJECT",
            "Generation path must be inside the HPM project: {}".format(path),
        )


def _validate_generation_paths(
    project: Any,
    peripheral_path: str,
    libxr_path: str,
    config_path: str,
    app_path: str,
    app_header_path: str,
    include_peripheral_output: bool = False,
) -> List[str]:
    sources = {
        _path_key(project.hpmpc_path): ".hpmpc input",
    }
    if not include_peripheral_output:
        sources[_path_key(peripheral_path)] = "peripheral config input"
    outputs = ([] if not include_peripheral_output else [peripheral_path]) + [
        libxr_path,
        app_path,
        app_header_path,
        config_path,
        project.board_c,
        project.board_h,
        project.pinmux_c,
        project.pinmux_h,
    ]
    for path in [project.hpmpc_path, peripheral_path] + outputs:
        _require_inside_project(path, project.root)

    output_keys: Dict[str, str] = {}
    for path in outputs:
        key = _path_key(path)
        if key in output_keys:
            raise GenerationPathError(
                "HPM_OUTPUT_PATH_CONFLICT",
                "Generated paths collide: {} and {}".format(output_keys[key], path),
            )
        if key in sources:
            raise GenerationPathError(
                "HPM_OUTPUT_PATH_CONFLICT",
                "Generated path collides with {}: {}".format(sources[key], path),
            )
        output_keys[key] = path
    return outputs


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8-sig", newline="") as source:
        return source.read()


def _relative_posix(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def _same_content(path: str, content: str) -> bool:
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as source:
        return source.read() == content.encode("utf-8")


def _temporary_path(path: str, kind: str) -> str:
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{}-{}-".format(os.path.basename(path), kind),
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    os.close(descriptor)
    return temporary


def _commit_files(rendered: Dict[str, str], ordered_paths: List[str]) -> None:
    """Commit all changed files and restore every target if one replace fails."""
    changed = [
        path for path in ordered_paths if not _same_content(path, rendered[path])
    ]
    staged: Dict[str, str] = {}
    backups: Dict[str, str] = {}
    committed: List[str] = []
    created_directories: List[str] = []
    try:
        for path in changed:
            directory = os.path.dirname(path)
            missing = []
            current = directory
            while current and not os.path.isdir(current):
                missing.append(current)
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
            os.makedirs(directory, exist_ok=True)
            created_directories.extend(reversed(missing))

            temporary = _temporary_path(path, "new")
            staged[path] = temporary
            with open(temporary, "wb") as output:
                output.write(rendered[path].encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            if os.path.isfile(path):
                os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
                backup = _temporary_path(path, "backup")
                backups[path] = backup
                shutil.copy2(path, backup)

        for path in changed:
            os.replace(staged[path], path)
            staged.pop(path)
            committed.append(path)
    except Exception as error:
        rollback_errors = []
        for path in reversed(committed):
            try:
                backup = backups.pop(path, "")
                if backup:
                    os.replace(backup, path)
                elif os.path.exists(path):
                    os.unlink(path)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise GenerationCommitError(
                "Generation failed and rollback was incomplete: {}".format(
                    "; ".join(rollback_errors)
                )
            ) from error
        raise GenerationCommitError(
            "Generation commit failed: {}".format(error)
        ) from error
    finally:
        for temporary in list(staged.values()) + list(backups.values()):
            try:
                os.unlink(temporary)
            except OSError:
                pass
        for directory in reversed(created_directories):
            try:
                os.rmdir(directory)
            except OSError:
                pass


def _with_newline(content: str, newline: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _generation_diagnostic(error: Exception) -> Diagnostic:
    code = "HPM_GENERATION_FAILED"
    if isinstance(error, GenerationPathError):
        code = error.code
    elif isinstance(error, GenerationCommitError):
        code = "HPM_GENERATION_COMMIT_FAILED"
    elif isinstance(error, ConfigFileError):
        code = "HPM_PERIPHERAL_CONFIG_INVALID"
    elif isinstance(error, MarkerConflictError):
        code = "HPM_BOARD_MARKER_CONFLICT"
    elif isinstance(error, MalformedCFunctionError):
        code = "HPM_BOARD_FUNCTION_MALFORMED"
    elif isinstance(error, BoardFileError):
        code = "HPM_BOARD_FILE_MISSING"
    elif isinstance(error, MissingBoardHelperError):
        code = "HPM_BOARD_HELPER_MISSING"
    elif isinstance(error, MissingClockFunctionError):
        code = "HPM_BOARD_CLOCK_FUNCTION_MISSING"
    elif isinstance(error, UnmanagedCanHelperError):
        code = "HPM_BOARD_CAN_MARKER_REQUIRED"
    elif isinstance(error, UnsupportedClockSourceError):
        code = "HPM_CLOCK_SOURCE_UNSUPPORTED"
    elif isinstance(error, BoardGenerationError):
        code = "HPM_BOARD_GENERATION_FAILED"
    elif isinstance(error, LibxrConfigError):
        code = "HPM_LIBXR_CONFIG_INVALID"
    return Diagnostic(code, ERROR, str(error))


def _apply_normalized_peripheral_pins(
    parsed_project: Dict[str, Any], normalized_config: Dict[str, Any]
) -> None:
    peripheral_groups = parsed_project.get("Peripherals")
    if not isinstance(peripheral_groups, dict):
        return

    parsed_by_instance: Dict[str, Dict[str, Any]] = {}
    for group in peripheral_groups.values():
        if not isinstance(group, dict):
            continue
        for instance, entry in group.items():
            if isinstance(instance, str) and isinstance(entry, dict):
                parsed_by_instance[instance] = entry

    for group_name in ("spi", "i2c", "uart", "mcan"):
        group = normalized_config.get(group_name)
        if not isinstance(group, dict):
            continue
        for instance, config in group.items():
            parsed = parsed_by_instance.get(instance)
            pins = config.get("pins") if isinstance(config, dict) else None
            if parsed is not None and isinstance(pins, dict):
                parsed["Pins"] = dict(pins)


def generate(
    directory: str,
    hpmpc_path: str = "",
    peripheral_config_path: str = "hpm_peripherals.yaml",
    libxr_config_path: str = "User/libxr_config.yaml",
    config_output_path: str = ".config.yaml",
    app_output_path: str = "User/app_main.cpp",
    use_xrobot: bool = False,
    use_hw_cntr: bool = False,
    candidate_config: Any = None,
) -> Dict[str, Any]:
    """Validate and render every managed HPM output before committing files."""
    warnings = []
    try:
        project = discover_project(directory, hpmpc_path)
        peripheral_path = _resolve_path(
            project.root, peripheral_config_path, "hpm_peripherals.yaml"
        )
        libxr_path = _resolve_path(
            project.root, libxr_config_path, "User/libxr_config.yaml"
        )
        config_path = _resolve_path(project.root, config_output_path, ".config.yaml")
        app_path = _resolve_path(project.root, app_output_path, "User/app_main.cpp")
        app_header_path = os.path.join(os.path.dirname(app_path), "app_main.h")
        include_peripheral_output = candidate_config is not None
        ordered_paths = _validate_generation_paths(
            project,
            peripheral_path,
            libxr_path,
            config_path,
            app_path,
            app_header_path,
            include_peripheral_output,
        )
        generated_files = [
            _relative_posix(path, project.root) for path in ordered_paths
        ]
        candidate = candidate_config
        if candidate is None:
            candidate = (
                read_config(peripheral_path) if os.path.isfile(peripheral_path) else {}
            )
        validation = validate_config(project, candidate)
        warnings = validation.warnings
        if not validation.valid:
            return generate_envelope(
                False, [], errors=validation.errors, warnings=validation.warnings
            )

        rendered = render_board_files(project, validation.normalized_config)
        settings = merge_libxr_config(
            read_libxr_config(libxr_path),
            validation.normalized_config,
            active_instances=selected_peripheral_instances(
                project, validation.normalized_config
            ),
        )
        parsed_project = parse_hpmpc_file(project.hpmpc_path, project.board_h)
        _apply_normalized_peripheral_pins(parsed_project, validation.normalized_config)
        existing_app = _read_text(app_path)
        app_content = generate_code(
            parsed_project,
            settings,
            use_xrobot=use_xrobot,
            use_hw_cntr=use_hw_cntr,
            existing=existing_app,
        )
        if existing_app:
            app_content = _with_newline(app_content, detect_newline(existing_app))
        existing_header = _read_text(app_header_path)
        header_newline = detect_newline(existing_header or existing_app)
        rendered[libxr_path] = serialize_libxr_config(settings)
        rendered[config_path] = yaml.safe_dump(
            parsed_project, allow_unicode=True, sort_keys=False
        )
        rendered[app_path] = app_content
        rendered[app_header_path] = _with_newline(_APP_MAIN_HEADER, header_newline)
        if include_peripheral_output:
            rendered[peripheral_path] = serialize_config(validation.normalized_config)

        _commit_files(rendered, ordered_paths)
        return generate_envelope(True, generated_files, warnings=validation.warnings)
    except (OSError, ValueError, yaml.YAMLError, BoardGenerationError) as error:
        return generate_envelope(
            False, [], errors=(_generation_diagnostic(error),), warnings=warnings
        )
