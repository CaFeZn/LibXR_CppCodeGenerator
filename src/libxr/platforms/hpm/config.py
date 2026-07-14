"""HPM peripheral configuration model, persistence, and validation."""

import copy
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

from .clock import (
    clock_sources_for_soc,
    default_clock_settings,
    peripheral_clock_hz,
    resolve_can_clock,
    resolve_spi_clock,
    resolve_spi_prescaler,
    resolve_uart_clock,
    uart_baudrate_error,
)
from .diagnostics import Diagnostic, ERROR, WARNING
from .hpmpc import PinmuxPeripheral
from .project import HpmProject
from .timing import can_timing_supported, resolve_i2c_timing

CONFIG_VERSION = 1
UART_DMA_CHANNEL_MIN = 0
UART_DMA_CHANNEL_MAX = 31
SPI_PRESCALERS = (1, 2, 4, 8, 16, 32, 64, 128, 256)
I2C_BUS_RATES = (100000, 400000, 1000000)

_COMMON_FIELD_TYPES = {
    "enabled": "boolean",
    "auto_clock": "boolean",
    "clock_source": "string",
    "clock_divider": "integer",
    "peripheral_clock_hz": "integer",
    "pins": "pins",
}
_GROUP_FIELD_TYPES = {
    "spi": {
        **_COMMON_FIELD_TYPES,
        "buffer_size": "integer",
        "sclk_hz": "integer",
        "spi_mode": "integer",
        "prescaler": "string",
        "actual_sclk_hz": "integer",
        "clock_polarity": "string",
        "clock_phase": "string",
        "cs_active_low": "boolean",
        "double_buffer": "boolean",
        "use_dma": "boolean",
        "tx_dma_channel": "integer",
        "rx_dma_channel": "integer",
        "use_gpio_cs": "boolean",
    },
    "i2c": {
        **_COMMON_FIELD_TYPES,
        "bus_hz": "integer",
        "address_mode": "string",
        "use_dma": "boolean",
        "dma_channel": "integer",
    },
    "uart": {
        **_COMMON_FIELD_TYPES,
        "baudrate": "integer",
        "rx_buffer_size": "integer",
        "tx_buffer_size": "integer",
        "tx_queue_size": "integer",
        "parity": "string",
        "data_bits": "integer",
        "stop_bits": "integer",
        "use_dma": "boolean",
        "tx_dma_channel": "integer",
        "rx_dma_channel": "integer",
    },
    "mcan": {
        **_COMMON_FIELD_TYPES,
        "mode": "string",
        "bitrate": "integer",
        "sample_point": "number",
        "data_bitrate": "integer",
        "data_sample_point": "number",
        "brs": "boolean",
        "queue_size": "integer",
        "loopback": "boolean",
        "listen_only": "boolean",
        "one_shot": "boolean",
        "esi": "boolean",
    },
}
_OPTIONAL_FIELDS = {
    "spi": {"spi_mode", "tx_dma_channel", "rx_dma_channel"},
    "i2c": {"dma_channel"},
    "uart": {"tx_dma_channel", "rx_dma_channel"},
    "mcan": {
        "sample_point",
        "data_bitrate",
        "data_sample_point",
        "brs",
        "queue_size",
        "esi",
    },
}
_FIELD_ALLOWED_VALUES = {
    ("spi", "spi_mode"): (0, 1, 2, 3),
    ("spi", "clock_polarity"): ("LOW", "HIGH"),
    ("spi", "clock_phase"): ("EDGE_1", "EDGE_2"),
    ("i2c", "address_mode"): ("7bit", "10bit"),
    ("uart", "parity"): ("NO_PARITY", "EVEN", "ODD"),
    ("uart", "data_bits"): (5, 6, 7, 8),
    ("uart", "stop_bits"): (1, 2),
    ("mcan", "mode"): ("can", "fdcan"),
}


class ConfigFileError(ValueError):
    """Raised when an HPM peripheral YAML file is not an object."""


@dataclass(frozen=True)
class ValidationResult:
    """Normalized candidate configuration and its structured diagnostics."""

    normalized_config: Dict[str, Any]
    errors: List[Diagnostic]
    warnings: List[Diagnostic]

    @property
    def valid(self) -> bool:
        return not self.errors


def _dict(value: Any) -> Dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _settings(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError("clock settings must be a mapping")


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _field_type_valid(value: Any, kind: str) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return _integer(value)
    if kind == "number":
        return _number(value)
    if kind == "string":
        return isinstance(value, str)
    if kind == "pins":
        return isinstance(value, Mapping) and all(
            isinstance(role, str) and isinstance(pad, str)
            for role, pad in value.items()
        )
    return False


def _has_diagnostic(
    errors: Sequence[Diagnostic], code: str, peripheral: str, field_name: str
) -> bool:
    return any(
        item.code == code and item.peripheral == peripheral and item.field == field_name
        for item in errors
    )


def _validate_config_shape(
    config: Mapping[str, Any],
    errors: List[Diagnostic],
    require_fields: bool,
) -> None:
    for group_name, field_types in _GROUP_FIELD_TYPES.items():
        group = config.get(group_name, {})
        if not isinstance(group, Mapping):
            errors.append(
                _diagnostic(
                    "HPM_CONFIG_GROUP_TYPE",
                    ERROR,
                    "{} must be an object keyed by peripheral instance.".format(
                        group_name
                    ),
                    field=group_name,
                )
            )
            continue
        for instance_value, entry in group.items():
            instance = str(instance_value)
            entry_path = "{}.{}".format(group_name, instance)
            if not isinstance(entry, Mapping):
                errors.append(
                    _diagnostic(
                        "HPM_CONFIG_ENTRY_TYPE",
                        ERROR,
                        "{} must be an object.".format(entry_path),
                        instance,
                        entry_path,
                    )
                )
                continue
            for field_name, kind in field_types.items():
                field_path = "{}.{}".format(entry_path, field_name)
                if field_name not in entry:
                    if (
                        require_fields
                        and field_name not in _OPTIONAL_FIELDS[group_name]
                    ):
                        errors.append(
                            _diagnostic(
                                "HPM_CONFIG_FIELD_MISSING",
                                ERROR,
                                "{} is required.".format(field_path),
                                instance,
                                field_name,
                            )
                        )
                    continue
                value = entry[field_name]
                if not _field_type_valid(value, kind):
                    if not _has_diagnostic(
                        errors, "HPM_CONFIG_FIELD_TYPE", instance, field_name
                    ):
                        errors.append(
                            _diagnostic(
                                "HPM_CONFIG_FIELD_TYPE",
                                ERROR,
                                "{} must be a {}.".format(field_path, kind),
                                instance,
                                field_name,
                            )
                        )
                    continue
                allowed = _FIELD_ALLOWED_VALUES.get((group_name, field_name))
                if allowed is not None and value not in allowed:
                    if not _has_diagnostic(
                        errors, "HPM_CONFIG_FIELD_VALUE", instance, field_name
                    ):
                        errors.append(
                            _diagnostic(
                                "HPM_CONFIG_FIELD_VALUE",
                                ERROR,
                                "{} has an unsupported value.".format(field_path),
                                instance,
                                field_name,
                            )
                        )


def _relative_posix(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def _read_board_header(project: HpmProject) -> str:
    try:
        with open(project.board_h, "r", encoding="utf-8", errors="replace") as source:
            return source.read()
    except OSError:
        return ""


def _clock_defaults(project: HpmProject) -> Dict[str, Any]:
    return _settings(default_clock_settings(project.soc_name, project.board_name))


def _spi_defaults(project: HpmProject, target_hz: int) -> Dict[str, Any]:
    resolved = resolve_spi_clock(project.soc_name, target_hz, project.board_name)
    if resolved is not None:
        return _settings(resolved)
    result = _clock_defaults(project)
    result.update({"prescaler": "DIV_1", "actual_sclk_hz": 0})
    return result


def _pins_for_functions(
    peripheral: PinmuxPeripheral, selected_functions: Sequence[str]
) -> Tuple[Dict[str, str], Dict[str, List[Tuple[str, str]]]]:
    function_pins = peripheral.function_pins
    if not function_pins:
        return copy.deepcopy(peripheral.pins), {}

    selected = set(selected_functions)
    applicable = [
        name for name in peripheral.functions if not selected or name in selected
    ]
    if not applicable:
        return copy.deepcopy(peripheral.pins), {}

    assignments: Dict[str, List[Tuple[str, str]]] = {}
    pins: Dict[str, str] = {}
    for function_name in applicable:
        for role, pad in function_pins.get(function_name, {}).items():
            assignments.setdefault(role, []).append((function_name, pad))
            pins.setdefault(role, pad)

    conflicts = {
        role: values
        for role, values in assignments.items()
        if len({pad for _, pad in values}) > 1
    }
    return pins or copy.deepcopy(peripheral.pins), conflicts


def default_config(
    project: HpmProject, selected_functions: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Return configuration defaults derived only from the inspected project."""

    selected = [] if selected_functions is None else list(selected_functions)
    available = set(project.pinmux_functions)
    selected = [name for name in selected if name in available]
    board_header = _read_board_header(project)
    has_gpio_spi_cs = all(
        marker in board_header
        for marker in (
            "BOARD_SPI_CS_GPIO_CTRL",
            "BOARD_SPI_CS_PIN",
            "BOARD_SPI_CS_ACTIVE_LEVEL",
        )
    )

    spi: Dict[str, Dict[str, Any]] = {}
    i2c: Dict[str, Dict[str, Any]] = {}
    uart: Dict[str, Dict[str, Any]] = {}
    mcan: Dict[str, Dict[str, Any]] = {}
    for peripheral in project.peripherals:
        pins, _ = _pins_for_functions(peripheral, selected)
        if peripheral.type == "SPI":
            entry = _spi_defaults(project, 20000000)
            entry.update(
                {
                    "enabled": True,
                    "buffer_size": 256,
                    "sclk_hz": 20000000,
                    "spi_mode": 0,
                    "clock_polarity": "LOW",
                    "clock_phase": "EDGE_1",
                    "cs_active_low": True,
                    "double_buffer": False,
                    "use_dma": False,
                    "use_gpio_cs": has_gpio_spi_cs
                    and any(role.startswith("CS") for role in pins),
                    "pins": pins,
                }
            )
            spi[peripheral.instance] = entry
        elif peripheral.type == "I2C":
            entry = _clock_defaults(project)
            entry.update(
                {
                    "enabled": True,
                    "bus_hz": 100000,
                    "address_mode": "7bit",
                    "use_dma": False,
                    "pins": pins,
                }
            )
            i2c[peripheral.instance] = entry
        elif peripheral.type == "UART":
            resolved = resolve_uart_clock(project.soc_name, 115200, project.board_name)
            entry = (
                _settings(resolved)
                if resolved is not None
                else _clock_defaults(project)
            )
            entry.update(
                {
                    "enabled": False,
                    "baudrate": 115200,
                    "rx_buffer_size": 256,
                    "tx_buffer_size": 256,
                    "tx_queue_size": 5,
                    "parity": "NO_PARITY",
                    "data_bits": 8,
                    "stop_bits": 1,
                    "use_dma": True,
                    "pins": pins,
                }
            )
            uart[peripheral.instance] = entry
        elif peripheral.type in ("MCAN", "CAN"):
            mode = "can"
            resolved = resolve_can_clock(
                project.soc_name,
                500000,
                0.875,
                mode,
                2000000,
                0.75,
                project.board_name,
                mode == "fdcan",
            )
            entry = (
                _settings(resolved)
                if resolved is not None
                else _clock_defaults(project)
            )
            entry.update(
                {
                    "enabled": True,
                    "mode": mode,
                    "bitrate": 500000,
                    "queue_size": 8,
                    "sample_point": 0.875,
                    "loopback": False,
                    "listen_only": False,
                    "one_shot": False,
                    "pins": pins,
                }
            )
            if mode == "fdcan":
                entry.update(
                    {
                        "data_bitrate": 2000000,
                        "data_sample_point": 0.75,
                        "brs": True,
                        "esi": False,
                    }
                )
            mcan[peripheral.instance] = entry

    return {
        "version": CONFIG_VERSION,
        "project": {
            "board": project.board_name,
            "soc": project.soc_name,
            "package": project.package_name,
            "sdk": project.sdk_name,
            "hpmpc": _relative_posix(project.hpmpc_path, project.root),
            "pinmux_functions": selected,
        },
        "spi": spi,
        "i2c": i2c,
        "uart": uart,
        "mcan": mcan,
    }


def _normalize_spi(entry: Dict[str, Any], original: Mapping[str, Any]) -> None:
    mode_value = entry.get("spi_mode", 0)
    mode = mode_value if _integer(mode_value) and 0 <= mode_value <= 3 else 0
    mode_polarity = "HIGH" if mode >= 2 else "LOW"
    mode_phase = "EDGE_2" if mode % 2 else "EDGE_1"
    if "clock_polarity" not in original and "spi_mode" in original:
        entry["clock_polarity"] = mode_polarity
    if "clock_phase" not in original and "spi_mode" in original:
        entry["clock_phase"] = mode_phase
    if entry.get("clock_polarity") not in ("LOW", "HIGH"):
        entry["clock_polarity"] = mode_polarity
    if entry.get("clock_phase") not in ("EDGE_1", "EDGE_2"):
        entry["clock_phase"] = mode_phase
    entry["spi_mode"] = (2 if entry["clock_polarity"] == "HIGH" else 0) + (
        1 if entry["clock_phase"] == "EDGE_2" else 0
    )
    entry["cs_active_low"] = entry.get("cs_active_low") is not False
    entry["use_dma"] = entry.get("use_dma") is True


def _normalize_manual_clock(project: HpmProject, entry: Dict[str, Any]) -> None:
    sources = list(clock_sources_for_soc(project.soc_name, project.board_name))
    source_ids = [
        source.get("id") if isinstance(source, Mapping) else getattr(source, "id", None)
        for source in sources
    ]
    if entry.get("clock_source") not in source_ids:
        entry["clock_source"] = source_ids[0] if source_ids else "osc24m"
    divider = entry.get("clock_divider")
    if (
        not isinstance(divider, int)
        or isinstance(divider, bool)
        or divider < 1
        or divider > 256
    ):
        divider = 1
        entry["clock_divider"] = divider
    entry["peripheral_clock_hz"] = peripheral_clock_hz(
        project.soc_name,
        entry["clock_source"],
        divider,
        project.board_name,
    )


def _normalize_clocks(project: HpmProject, config: Dict[str, Any]) -> None:
    for entry in config["spi"].values():
        if entry.get("auto_clock") is not False:
            resolved = resolve_spi_clock(
                project.soc_name, entry.get("sclk_hz"), project.board_name
            )
            if resolved is not None:
                entry.update(_settings(resolved))
            else:
                entry["actual_sclk_hz"] = 0
        else:
            _normalize_manual_clock(project, entry)
            resolved = resolve_spi_prescaler(
                entry.get("peripheral_clock_hz"), entry.get("sclk_hz")
            )
            if resolved is not None:
                entry.update(_settings(resolved))
            else:
                entry["actual_sclk_hz"] = 0

    for entry in config["i2c"].values():
        if entry.get("auto_clock") is not False:
            entry.update(_clock_defaults(project))
        else:
            _normalize_manual_clock(project, entry)

    for entry in config["uart"].values():
        if entry.get("auto_clock") is not False:
            resolved = resolve_uart_clock(
                project.soc_name, entry.get("baudrate"), project.board_name
            )
            if resolved is not None:
                entry.update(_settings(resolved))
            else:
                entry["peripheral_clock_hz"] = 0
        else:
            _normalize_manual_clock(project, entry)

    for entry in config["mcan"].values():
        if entry.get("auto_clock") is not False:
            resolved = resolve_can_clock(
                project.soc_name,
                entry.get("bitrate"),
                entry.get("sample_point", 0.875),
                entry.get("mode"),
                entry.get("data_bitrate"),
                entry.get("data_sample_point"),
                project.board_name,
                entry.get("brs") is True,
            )
            if resolved is not None:
                entry.update(_settings(resolved))
            else:
                entry["peripheral_clock_hz"] = 0
        else:
            _normalize_manual_clock(project, entry)


def _allocate_uart_dma(config: Dict[str, Any]) -> None:
    enabled = [
        (instance, entry)
        for instance, entry in sorted(config["uart"].items())
        if entry.get("enabled") is True and entry.get("use_dma") is True
    ]
    used: Set[int] = set()
    for _, entry in enabled:
        for field in ("tx_dma_channel", "rx_dma_channel"):
            channel = entry.get(field)
            if (
                _integer(channel)
                and UART_DMA_CHANNEL_MIN <= channel <= UART_DMA_CHANNEL_MAX
            ):
                used.add(channel)

    available = iter(
        channel
        for channel in range(UART_DMA_CHANNEL_MIN, UART_DMA_CHANNEL_MAX + 1)
        if channel not in used
    )
    for _, entry in enabled:
        for field in ("tx_dma_channel", "rx_dma_channel"):
            if field not in entry or entry[field] is None:
                entry[field] = next(available, None)


def normalize_config(
    project: HpmProject, candidate: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Merge user behavior with project-derived defaults and detected pins."""

    source = _dict(candidate)
    source_project = _dict(source.get("project"))
    selected_value = source_project.get("pinmux_functions", [])
    selected = list(selected_value) if isinstance(selected_value, list) else []
    defaults = default_config(project, selected)
    result = source
    result["version"] = CONFIG_VERSION

    project_config = source_project
    project_config.update(defaults["project"])
    result["project"] = project_config

    for group in ("spi", "i2c", "uart", "mcan"):
        source_group = source.get(group)
        source_group = source_group if isinstance(source_group, dict) else {}
        normalized_group: Dict[str, Dict[str, Any]] = {}
        for instance, base in defaults[group].items():
            original = source_group.get(instance)
            original = original if isinstance(original, dict) else {}
            entry = copy.deepcopy(base)
            entry.update(copy.deepcopy(original))
            entry["pins"] = copy.deepcopy(base["pins"])
            if group == "spi":
                _normalize_spi(entry, original)
            elif group == "uart":
                entry["use_dma"] = True
            elif group == "mcan":
                entry.setdefault("queue_size", 8)
                entry.setdefault("sample_point", 0.875)
                if entry.get("mode") == "can":
                    for name in (
                        "data_bitrate",
                        "data_sample_point",
                        "brs",
                        "esi",
                    ):
                        entry.pop(name, None)
                elif entry.get("mode") == "fdcan":
                    entry.setdefault("data_bitrate", 2000000)
                    entry.setdefault("data_sample_point", 0.75)
                    entry.setdefault("brs", True)
                    entry.setdefault("esi", False)
            normalized_group[instance] = entry
        result[group] = normalized_group

    _normalize_clocks(project, result)
    _allocate_uart_dma(result)
    return result


def read_config(path: str) -> Dict[str, Any]:
    """Read an HPM peripheral YAML document without normalizing it."""

    try:
        with open(path, "r", encoding="utf-8-sig") as source:
            value = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise ConfigFileError("Invalid HPM peripheral YAML: {}".format(path)) from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigFileError(
            "HPM peripheral YAML root must be an object: {}".format(path)
        )
    return value


def load_config(path: str, project: HpmProject) -> Dict[str, Any]:
    """Load and normalize a config, returning defaults when the file is absent."""

    candidate = read_config(path) if os.path.isfile(path) else {}
    return normalize_config(project, candidate)


def serialize_config(config: Mapping[str, Any]) -> str:
    """Serialize a normalized HPM peripheral configuration deterministically."""

    return yaml.safe_dump(
        dict(config),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def write_config(path: str, config: Mapping[str, Any]) -> None:
    """Atomically replace an HPM peripheral YAML file."""

    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{}-".format(os.path.basename(target)), suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(serialize_config(config))
            output.flush()
            os.fsync(output.fileno())
        if os.path.exists(target):
            os.chmod(temporary, stat.S_IMODE(os.stat(target).st_mode))
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _selected(project: HpmProject, config: Mapping[str, Any], instance: str) -> bool:
    project_config = config.get("project", {})
    selected = set(project_config.get("pinmux_functions", []))
    if not selected:
        return True
    for peripheral in project.peripherals:
        if peripheral.instance == instance:
            functions = set(peripheral.functions)
            functions.add("init_{}_pins".format(instance.lower()))
            return bool(selected.intersection(functions))
    return False


def selected_peripheral_instances(
    project: HpmProject, config: Mapping[str, Any]
) -> Set[str]:
    """Return lower-case instances active in the selected Pinmux functions."""

    return {
        peripheral.instance.lower()
        for peripheral in project.peripherals
        if _selected(project, config, peripheral.instance)
    }


def _diagnostic(
    code: str,
    level: str,
    message: str,
    peripheral: Optional[str] = None,
    field: Optional[str] = None,
) -> Diagnostic:
    return Diagnostic(code, level, message, peripheral, field)


def _validate_original_values(
    project: HpmProject,
    candidate: Mapping[str, Any],
    normalized: Mapping[str, Any],
    errors: List[Diagnostic],
) -> None:
    version = candidate.get("version")
    if version is not None and (not _integer(version) or version != CONFIG_VERSION):
        errors.append(
            _diagnostic(
                "HPM_CONFIG_VERSION_UNSUPPORTED",
                ERROR,
                "HPM peripheral config version must be {}.".format(CONFIG_VERSION),
                field="version",
            )
        )

    sources = clock_sources_for_soc(project.soc_name, project.board_name)
    source_ids = {
        source.get("id") if isinstance(source, Mapping) else getattr(source, "id", None)
        for source in sources
    }
    for group in ("spi", "i2c", "uart", "mcan"):
        candidate_group = candidate.get(group, {})
        if not isinstance(candidate_group, dict):
            continue
        for instance, original in candidate_group.items():
            if not isinstance(original, dict):
                continue
            normalized_entry = normalized.get(group, {}).get(instance)
            if (
                not isinstance(normalized_entry, dict)
                or normalized_entry.get("enabled") is not True
                or not _selected(project, normalized, instance)
            ):
                continue
            if original.get("auto_clock") is False:
                if original.get("clock_source") not in source_ids:
                    errors.append(
                        _diagnostic(
                            "HPM_CLOCK_SOURCE_INVALID",
                            ERROR,
                            "Clock source is not available for this SoC and board.",
                            instance,
                            "clock_source",
                        )
                    )
                divider = original.get("clock_divider")
                if (
                    not isinstance(divider, int)
                    or isinstance(divider, bool)
                    or divider < 1
                    or divider > 256
                ):
                    errors.append(
                        _diagnostic(
                            "HPM_CLOCK_DIVIDER_INVALID",
                            ERROR,
                            "Clock divider must be an integer from 1 through 256.",
                            instance,
                            "clock_divider",
                        )
                    )
            if group != "spi":
                continue
            if "spi_mode" in original and (
                not _integer(original["spi_mode"])
                or original["spi_mode"] not in (0, 1, 2, 3)
            ):
                errors.append(
                    _diagnostic(
                        "HPM_SPI_MODE_INVALID",
                        ERROR,
                        "SPI mode must be 0, 1, 2, or 3.",
                        instance,
                        "spi_mode",
                    )
                )
            if "clock_polarity" in original and original["clock_polarity"] not in (
                "LOW",
                "HIGH",
            ):
                errors.append(
                    _diagnostic(
                        "HPM_SPI_CPOL_INVALID",
                        ERROR,
                        "SPI clock polarity must be LOW or HIGH.",
                        instance,
                        "clock_polarity",
                    )
                )
            if "clock_phase" in original and original["clock_phase"] not in (
                "EDGE_1",
                "EDGE_2",
            ):
                errors.append(
                    _diagnostic(
                        "HPM_SPI_CPHA_INVALID",
                        ERROR,
                        "SPI clock phase must be EDGE_1 or EDGE_2.",
                        instance,
                        "clock_phase",
                    )
                )


def _validate_selected_functions(
    project: HpmProject,
    candidate: Mapping[str, Any],
    normalized: Mapping[str, Any],
    warnings: List[Diagnostic],
) -> None:
    candidate_project = candidate.get("project", {})
    requested = candidate_project.get("pinmux_functions", [])
    if isinstance(requested, list):
        available = set(project.pinmux_functions)
        for name in requested:
            if isinstance(name, str) and name not in available:
                warnings.append(
                    _diagnostic(
                        "HPM_PINMUX_FUNCTION_UNKNOWN",
                        WARNING,
                        "Pinmux function '{}' is no longer available and was removed.".format(
                            name
                        ),
                        field="project.pinmux_functions",
                    )
                )
    selected = normalized["project"]["pinmux_functions"]
    if selected and not any(
        _selected(project, normalized, peripheral.instance)
        for peripheral in project.peripherals
        if peripheral.type in ("SPI", "I2C", "UART", "MCAN", "CAN")
    ):
        warnings.append(
            _diagnostic(
                "HPM_PINMUX_NO_COMMUNICATION_PERIPHERAL",
                WARNING,
                "Selected Pinmux functions contain no communication peripheral.",
                field="project.pinmux_functions",
            )
        )


def _validate_selected_pin_conflicts(
    project: HpmProject,
    normalized: Mapping[str, Any],
    errors: List[Diagnostic],
) -> None:
    project_config = normalized.get("project", {})
    selected_value = (
        project_config.get("pinmux_functions", [])
        if isinstance(project_config, Mapping)
        else []
    )
    selected = selected_value if isinstance(selected_value, list) else []
    pad_owners: Dict[str, Dict[Tuple[str, str], List[str]]] = {}
    for peripheral in project.peripherals:
        if selected and not _selected(project, normalized, peripheral.instance):
            continue
        _, conflicts = _pins_for_functions(peripheral, selected)
        for role, assignments in conflicts.items():
            details = ", ".join(
                "{} ({})".format(pad, function_name)
                for function_name, pad in assignments
            )
            errors.append(
                _diagnostic(
                    "HPM_PINMUX_PIN_CONFLICT",
                    ERROR,
                    "Selected Pinmux functions assign {}.{} to multiple pads: {}.".format(
                        peripheral.instance, role, details
                    ),
                    peripheral.instance,
                    "pins.{}".format(role),
                )
            )

        applicable = [
            name for name in peripheral.functions if not selected or name in selected
        ]
        for function_name in applicable:
            for role, pad in peripheral.function_pins.get(function_name, {}).items():
                owners = pad_owners.setdefault(pad, {})
                owners.setdefault((peripheral.instance, role), []).append(function_name)

    for pad, owners in pad_owners.items():
        if len(owners) < 2:
            continue
        details = ", ".join(
            "{}.{} ({})".format(instance, role, ", ".join(functions))
            for (instance, role), functions in owners.items()
        )
        errors.append(
            _diagnostic(
                "HPM_PINMUX_PIN_CONFLICT",
                ERROR,
                "Selected Pinmux functions assign pad {} to multiple signals: {}.".format(
                    pad, details
                ),
                field="project.pinmux_functions",
            )
        )


def _validate_dma(
    project: HpmProject,
    candidate: Mapping[str, Any],
    config: Mapping[str, Any],
    errors: List[Diagnostic],
    warnings: List[Diagnostic],
) -> None:
    candidate_spi = candidate.get("spi", {})
    candidate_spi = candidate_spi if isinstance(candidate_spi, dict) else {}
    for instance, entry in config["spi"].items():
        original = candidate_spi.get(instance, {})
        if (
            entry.get("enabled") is True
            and _selected(project, config, instance)
            and isinstance(original, dict)
            and original.get("use_dma") is True
        ):
            warnings.append(
                _diagnostic(
                    "HPM_SPI_DMA_UNSUPPORTED",
                    WARNING,
                    "SPI DMA policy is not supported by the current HPM backend.",
                    instance,
                    "use_dma",
                )
            )

    candidate_i2c = candidate.get("i2c", {})
    candidate_i2c = candidate_i2c if isinstance(candidate_i2c, dict) else {}
    for instance, entry in config["i2c"].items():
        if entry.get("enabled") is not True or not _selected(project, config, instance):
            continue
        original = candidate_i2c.get(instance, {})
        if not isinstance(original, dict):
            continue
        if original.get("dma_channel") is not None:
            warnings.append(
                _diagnostic(
                    "HPM_I2C_FIXED_DMA_CHANNEL_UNSUPPORTED",
                    WARNING,
                    "I2C fixed DMA channels are unsupported and are not reserved.",
                    instance,
                    "dma_channel",
                )
            )
        if original.get("use_dma") is True:
            warnings.append(
                _diagnostic(
                    "HPM_I2C_DMA_RUNTIME_ALLOCATED",
                    WARNING,
                    "Asynchronous I2C operations allocate a DMA channel at runtime.",
                    instance,
                    "use_dma",
                )
            )

    owners: Dict[int, Tuple[str, str]] = {}
    for instance, entry in sorted(config["uart"].items()):
        if entry.get("enabled") is not True or not _selected(project, config, instance):
            continue
        for field in ("tx_dma_channel", "rx_dma_channel"):
            channel = entry.get(field)
            if not _integer(channel) or not (
                UART_DMA_CHANNEL_MIN <= channel <= UART_DMA_CHANNEL_MAX
            ):
                code = (
                    "HPM_UART_DMA_CHANNEL_UNAVAILABLE"
                    if channel is None
                    else "HPM_UART_DMA_CHANNEL_RANGE"
                )
                errors.append(
                    _diagnostic(
                        code,
                        ERROR,
                        "UART DMA channel must be an integer from 0 through 31.",
                        instance,
                        field,
                    )
                )
                continue
            previous = owners.get(channel)
            if previous is not None:
                errors.append(
                    _diagnostic(
                        "HPM_DMA_CHANNEL_CONFLICT",
                        ERROR,
                        "UART DMA channel {} is already used by {}.{}.".format(
                            channel, previous[0], previous[1]
                        ),
                        instance,
                        field,
                    )
                )
            else:
                owners[channel] = (instance, field)


def validate_config(
    project: HpmProject, candidate: Optional[Mapping[str, Any]] = None
) -> ValidationResult:
    """Normalize and validate a candidate without modifying project files."""

    original = _dict(candidate)
    config = normalize_config(project, original)
    errors: List[Diagnostic] = []
    warnings: List[Diagnostic] = []
    _validate_config_shape(original, errors, False)
    _validate_config_shape(config, errors, True)
    _validate_original_values(project, original, config, errors)
    _validate_selected_functions(project, original, config, warnings)
    _validate_selected_pin_conflicts(project, config, errors)

    if (
        project.soc_name.upper() != "HPM5361"
        or project.board_name.lower() != "hpm5361evklite"
    ):
        warnings.append(
            _diagnostic(
                "HPM_CLOCK_TREE_LIMITED",
                WARNING,
                "Only the 24 MHz oscillator is modeled for this board.",
                field="project.board",
            )
        )

    for group in ("spi", "i2c", "uart", "mcan"):
        for instance, entry in config[group].items():
            if entry.get("enabled") is not True or not _selected(
                project, config, instance
            ):
                continue
            clock_hz = entry.get("peripheral_clock_hz")
            if not _number(clock_hz) or clock_hz <= 0 or clock_hz > 200000000:
                errors.append(
                    _diagnostic(
                        "HPM_CLOCK_OUT_OF_RANGE",
                        ERROR,
                        "Peripheral clock must be between 1 Hz and 200000000 Hz.",
                        instance,
                        "peripheral_clock_hz",
                    )
                )

    for instance, entry in config["spi"].items():
        if entry.get("enabled") is not True or not _selected(project, config, instance):
            continue
        if not _integer(entry.get("buffer_size")) or entry["buffer_size"] <= 0:
            errors.append(
                _diagnostic(
                    "HPM_SPI_BUFFER_SIZE_INVALID",
                    ERROR,
                    "SPI buffer_size must be a positive integer.",
                    instance,
                    "buffer_size",
                )
            )
        if entry.get("double_buffer") is True and (
            not _integer(entry.get("buffer_size")) or entry["buffer_size"] < 2
        ):
            errors.append(
                _diagnostic(
                    "HPM_SPI_DOUBLE_BUFFER_SIZE",
                    ERROR,
                    "SPI double_buffer requires buffer_size >= 2.",
                    instance,
                    "double_buffer",
                )
            )
        if entry.get("spi_mode") not in (0, 1, 2, 3):
            errors.append(
                _diagnostic(
                    "HPM_SPI_MODE_INVALID",
                    ERROR,
                    "SPI mode must be 0, 1, 2, or 3.",
                    instance,
                    "spi_mode",
                )
            )
        if entry.get("clock_polarity") not in ("LOW", "HIGH"):
            errors.append(
                _diagnostic(
                    "HPM_SPI_CLOCK_POLARITY_INVALID",
                    ERROR,
                    "SPI clock polarity must be LOW or HIGH.",
                    instance,
                    "clock_polarity",
                )
            )
        if entry.get("clock_phase") not in ("EDGE_1", "EDGE_2"):
            errors.append(
                _diagnostic(
                    "HPM_SPI_CLOCK_PHASE_INVALID",
                    ERROR,
                    "SPI clock phase must be EDGE_1 or EDGE_2.",
                    instance,
                    "clock_phase",
                )
            )
        actual = entry.get("actual_sclk_hz")
        requested = entry.get("sclk_hz")
        if (
            not _number(actual)
            or not _number(requested)
            or actual <= 0
            or requested <= 0
            or actual > requested
        ):
            errors.append(
                _diagnostic(
                    "HPM_SPI_CLOCK_UNREACHABLE",
                    ERROR,
                    "Requested SPI SCLK cannot be generated by the selected clock.",
                    instance,
                    "sclk_hz",
                )
            )
        elif actual != requested:
            warnings.append(
                _diagnostic(
                    "HPM_SPI_CLOCK_ADJUSTED",
                    WARNING,
                    "SPI SCLK was adjusted from {} Hz to {} Hz.".format(
                        requested, actual
                    ),
                    instance,
                    "sclk_hz",
                )
            )
        if (
            entry.get("use_gpio_cs") is not True
            and entry.get("cs_active_low") is not True
        ):
            errors.append(
                _diagnostic(
                    "HPM_SPI_HARDWARE_CS_ACTIVE_HIGH",
                    ERROR,
                    "Hardware SPI chip-select is fixed active-low; use GPIO CS for active-high.",
                    instance,
                    "cs_active_low",
                )
            )
        if entry.get("use_gpio_cs") is not True:
            warnings.append(
                _diagnostic(
                    "HPM_SPI_HARDWARE_CS_FIXED",
                    WARNING,
                    "Hardware SPI chip-select is fixed active-low.",
                    instance,
                    "use_gpio_cs",
                )
            )

    gpio_cs = [
        (instance, entry)
        for instance, entry in config["spi"].items()
        if entry.get("enabled") is True
        and entry.get("use_gpio_cs") is True
        and _selected(project, config, instance)
    ]
    if len({entry.get("cs_active_low") for _, entry in gpio_cs}) > 1:
        errors.append(
            _diagnostic(
                "HPM_SPI_GPIO_CS_POLARITY_CONFLICT",
                ERROR,
                "GPIO SPI chip-select polarity must be shared by all instances.",
                field="spi.*.cs_active_low",
            )
        )
    if len(gpio_cs) > 1:
        errors.append(
            _diagnostic(
                "HPM_SPI_GPIO_CS_COUNT",
                ERROR,
                "This board exposes one global GPIO SPI chip-select.",
                field="spi.*.use_gpio_cs",
            )
        )

    for instance, entry in config["i2c"].items():
        if entry.get("enabled") is not True or not _selected(project, config, instance):
            continue
        if entry.get("bus_hz") not in I2C_BUS_RATES:
            errors.append(
                _diagnostic(
                    "HPM_I2C_BUS_RATE_UNSUPPORTED",
                    ERROR,
                    "I2C bus_hz must be 100000, 400000, or 1000000.",
                    instance,
                    "bus_hz",
                )
            )
        if entry.get("address_mode") not in ("7bit", "10bit"):
            errors.append(
                _diagnostic(
                    "HPM_I2C_ADDRESS_MODE_INVALID",
                    ERROR,
                    "I2C address_mode must be 7bit or 10bit.",
                    instance,
                    "address_mode",
                )
            )
        if (
            resolve_i2c_timing(entry.get("peripheral_clock_hz"), entry.get("bus_hz"))
            is None
        ):
            errors.append(
                _diagnostic(
                    "HPM_I2C_TIMING_UNREACHABLE",
                    ERROR,
                    "I2C timing cannot be represented by the selected peripheral clock.",
                    instance,
                    "bus_hz",
                )
            )

    for instance, entry in config["uart"].items():
        if entry.get("enabled") is not True or not _selected(project, config, instance):
            continue
        if (
            uart_baudrate_error(entry.get("peripheral_clock_hz"), entry.get("baudrate"))
            is None
        ):
            errors.append(
                _diagnostic(
                    "HPM_UART_BAUDRATE_UNREACHABLE",
                    ERROR,
                    "UART baudrate is not reachable within the HPM SDK tolerance.",
                    instance,
                    "baudrate",
                )
            )
        for field, minimum in (
            ("rx_buffer_size", 1),
            ("tx_buffer_size", 2),
            ("tx_queue_size", 1),
        ):
            if not _integer(entry.get(field)) or entry[field] < minimum:
                errors.append(
                    _diagnostic(
                        "HPM_UART_{}_INVALID".format(field.upper()),
                        ERROR,
                        "UART {} must be an integer of at least {}.".format(
                            field, minimum
                        ),
                        instance,
                        field,
                    )
                )
        if entry.get("parity") not in ("NO_PARITY", "EVEN", "ODD"):
            errors.append(
                _diagnostic(
                    "HPM_UART_PARITY_INVALID",
                    ERROR,
                    "UART parity must be NO_PARITY, EVEN, or ODD.",
                    instance,
                    "parity",
                )
            )
        if entry.get("data_bits") not in (5, 6, 7, 8):
            errors.append(
                _diagnostic(
                    "HPM_UART_DATA_BITS_INVALID",
                    ERROR,
                    "UART data_bits must be between 5 and 8.",
                    instance,
                    "data_bits",
                )
            )
        if entry.get("stop_bits") not in (1, 2):
            errors.append(
                _diagnostic(
                    "HPM_UART_STOP_BITS_INVALID",
                    ERROR,
                    "UART stop_bits must be 1 or 2.",
                    instance,
                    "stop_bits",
                )
            )

    for instance, entry in config["mcan"].items():
        if entry.get("enabled") is not True or not _selected(project, config, instance):
            continue
        mode = entry.get("mode")
        if mode not in ("can", "fdcan"):
            errors.append(
                _diagnostic(
                    "HPM_CAN_MODE_INVALID",
                    ERROR,
                    "CAN mode must be can or fdcan.",
                    instance,
                    "mode",
                )
            )
            continue
        nominal_kind = "fdcan_nominal" if mode == "fdcan" else "can"
        if not can_timing_supported(
            entry.get("peripheral_clock_hz"),
            entry.get("bitrate"),
            entry.get("sample_point", 0.875),
            nominal_kind,
        ):
            errors.append(
                _diagnostic(
                    "HPM_CAN_NOMINAL_TIMING_UNREACHABLE",
                    ERROR,
                    "CAN nominal bitrate/sample point cannot be represented.",
                    instance,
                    "bitrate",
                )
            )
        if mode == "fdcan" and not can_timing_supported(
            entry.get("peripheral_clock_hz"),
            entry.get("data_bitrate", entry.get("bitrate")),
            entry.get("data_sample_point", entry.get("sample_point", 0.75)),
            "fdcan_data",
            2 if entry.get("brs") is True else 256,
        ):
            errors.append(
                _diagnostic(
                    "HPM_CAN_DATA_TIMING_UNREACHABLE",
                    ERROR,
                    "CAN FD data bitrate/sample point cannot be represented.",
                    instance,
                    "data_bitrate",
                )
            )
        if not _integer(entry.get("queue_size")) or entry["queue_size"] <= 0:
            errors.append(
                _diagnostic(
                    "HPM_CAN_QUEUE_SIZE_INVALID",
                    ERROR,
                    "CAN queue_size must be a positive integer.",
                    instance,
                    "queue_size",
                )
            )
        if entry.get("loopback") is True and entry.get("listen_only") is True:
            errors.append(
                _diagnostic(
                    "HPM_CAN_MODE_CONFLICT",
                    ERROR,
                    "CAN loopback and listen_only cannot both be enabled.",
                    instance,
                    "loopback",
                )
            )

    _validate_dma(project, original, config, errors, warnings)
    return ValidationResult(config, errors, warnings)


def capabilities_for_project(project: HpmProject) -> Dict[str, Any]:
    """Return UI field options and limits without duplicating them in clients."""

    uart_dma_channels = 32 if project.soc_name.upper() == "HPM5361" else 0
    return {
        "spi": {
            "modes": [0, 1, 2, 3],
            "clock_polarities": ["LOW", "HIGH"],
            "clock_phases": ["EDGE_1", "EDGE_2"],
            "prescalers": ["DIV_{}".format(value) for value in SPI_PRESCALERS],
            "buffer_size": {"min": 1},
            "clock_divider": {"min": 1, "max": 256},
            "dma_supported": False,
            "gpio_cs": {"max_instances": 1},
        },
        "i2c": {
            "bus_rates": list(I2C_BUS_RATES),
            "address_modes": ["7bit", "10bit"],
            "fixed_dma_channel": False,
            "runtime_dma_allocation": True,
        },
        "uart": {
            "parity": ["NO_PARITY", "EVEN", "ODD"],
            "data_bits": [5, 6, 7, 8],
            "stop_bits": [1, 2],
            "dma": {
                "automatic": True,
                "channel_min": UART_DMA_CHANNEL_MIN,
                "channel_max": UART_DMA_CHANNEL_MAX,
                "channel_count": uart_dma_channels,
            },
        },
        "mcan": {
            "modes": ["can", "fdcan"],
            "sample_point": {"min": 0.5, "max": 0.95, "step": 0.001},
            "data_fields": [
                "data_bitrate",
                "data_sample_point",
                "brs",
                "esi",
            ],
            "queue_size": {"min": 1},
        },
    }
