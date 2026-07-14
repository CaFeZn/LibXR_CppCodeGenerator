"""Merge HPM peripheral settings into LibXR YAML without losing user-owned data."""

import copy
import os
import re
import stat
import tempfile
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set

import yaml


class LibxrConfigError(ValueError):
    """Raised when a LibXR configuration document is not a YAML mapping."""


_SPI_FIELDS = (
    "buffer_size",
    "sclk_hz",
    "spi_mode",
    "prescaler",
    "actual_sclk_hz",
    "use_gpio_cs",
    "clock_polarity",
    "clock_phase",
    "cs_active_low",
    "double_buffer",
)
_I2C_FIELDS = ("bus_hz", "address_mode", "peripheral_clock_hz")
_UART_FIELDS = (
    "baudrate",
    "rx_buffer_size",
    "tx_buffer_size",
    "tx_queue_size",
    "parity",
    "data_bits",
    "stop_bits",
    "peripheral_clock_hz",
    "rx_dma_channel",
    "tx_dma_channel",
)
_CAN_FIELDS = (
    "bitrate",
    "peripheral_clock_hz",
    "loopback",
    "listen_only",
    "one_shot",
)
_FDCAN_FIELDS = ("data_bitrate", "data_sample_point", "brs", "esi")
_SPI_MANAGED_FIELDS = _SPI_FIELDS + (
    "use_dma",
    "tx_dma_channel",
    "rx_dma_channel",
)
_I2C_MANAGED_FIELDS = _I2C_FIELDS + ("use_dma", "dma_channel")
_UART_MANAGED_FIELDS = _UART_FIELDS + ("use_dma",)
_MCAN_MANAGED_FIELDS = (
    _CAN_FIELDS
    + _FDCAN_FIELDS
    + (
        "queue_size",
        "index",
        "sample_point",
    )
)


def _mapping(value: Any) -> Dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _group(config: Mapping[str, Any], name: str) -> Dict[str, Any]:
    return _mapping(config.get(name))


def _selected_fields(entry: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    return {field: copy.deepcopy(entry[field]) for field in fields if field in entry}


def _instance_index(instance: str) -> int:
    match = re.search(r"(\d+)$", instance)
    return int(match.group(1)) if match else 0


def _coalesce(value: Any, default: Any) -> Any:
    return default if value is None else value


def _managed_updates(
    peripheral_config: Mapping[str, Any],
    active_instances: Optional[Set[str]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    updates: Dict[str, Dict[str, Dict[str, Any]]] = {
        "SPI": {},
        "I2C": {},
        "UART": {},
        "CAN": {},
        "FDCAN": {},
    }

    for instance, value in _group(peripheral_config, "spi").items():
        if (
            isinstance(value, Mapping)
            and bool(value.get("enabled"))
            and (active_instances is None or str(instance).lower() in active_instances)
        ):
            updates["SPI"][str(instance).lower()] = _selected_fields(value, _SPI_FIELDS)

    for instance, value in _group(peripheral_config, "i2c").items():
        if (
            isinstance(value, Mapping)
            and bool(value.get("enabled"))
            and (active_instances is None or str(instance).lower() in active_instances)
        ):
            updates["I2C"][str(instance).lower()] = _selected_fields(value, _I2C_FIELDS)

    for instance, value in _group(peripheral_config, "uart").items():
        if (
            isinstance(value, Mapping)
            and bool(value.get("enabled"))
            and (active_instances is None or str(instance).lower() in active_instances)
        ):
            updates["UART"][str(instance).lower()] = _selected_fields(
                value, _UART_FIELDS
            )

    for instance, value in _group(peripheral_config, "mcan").items():
        if (
            not isinstance(value, Mapping)
            or not bool(value.get("enabled"))
            or (
                active_instances is not None
                and str(instance).lower() not in active_instances
            )
        ):
            continue
        section = "FDCAN" if value.get("mode") == "fdcan" else "CAN"
        entry = _selected_fields(value, _CAN_FIELDS)
        entry["queue_size"] = copy.deepcopy(_coalesce(value.get("queue_size"), 8))
        entry["index"] = _instance_index(str(instance))
        entry["sample_point"] = copy.deepcopy(
            _coalesce(value.get("sample_point"), 0.875)
        )
        if section == "FDCAN":
            entry.update(_selected_fields(value, _FDCAN_FIELDS))
            entry["data_bitrate"] = copy.deepcopy(
                _coalesce(value.get("data_bitrate"), 2_000_000)
            )
            entry["data_sample_point"] = copy.deepcopy(
                _coalesce(value.get("data_sample_point"), 0.75)
            )
            entry["brs"] = copy.deepcopy(_coalesce(value.get("brs"), True))
            entry["esi"] = copy.deepcopy(_coalesce(value.get("esi"), False))
        updates[section][str(instance).lower()] = entry
    return updates


def _managed_instances(
    peripheral_config: Mapping[str, Any], group_names: Iterable[str]
) -> Dict[str, Sequence[str]]:
    result: Dict[str, Sequence[str]] = {}
    for name in group_names:
        result[name] = [str(instance) for instance in _group(peripheral_config, name)]
    return result


def _merge_group(
    existing: Mapping[str, Any],
    section: str,
    managed: Sequence[str],
    updates: Mapping[str, Mapping[str, Any]],
    managed_fields: Sequence[str],
) -> Dict[str, Any]:
    result = _group(existing, section)
    for instance in managed:
        key = instance.lower()
        current = _mapping(result.get(key))
        for field in managed_fields:
            current.pop(field, None)
        if key in updates:
            current.update(copy.deepcopy(dict(updates[key])))
        if not current:
            result.pop(key, None)
            continue
        result[key] = current
    return result


def _mcan_sections(peripheral_config: Mapping[str, Any]) -> Dict[str, str]:
    sections = {}
    for instance, entry in _group(peripheral_config, "mcan").items():
        section = (
            "FDCAN"
            if isinstance(entry, Mapping) and entry.get("mode") == "fdcan"
            else "CAN"
        )
        sections[str(instance).lower()] = section
    return sections


def _without_fields(value: Any, fields: Sequence[str]) -> Dict[str, Any]:
    result = _mapping(value)
    for field in fields:
        result.pop(field, None)
    return result


def _merge_mcan_groups(
    existing: Mapping[str, Any],
    managed: Sequence[str],
    updates: Mapping[str, Mapping[str, Mapping[str, Any]]],
    active_sections: Mapping[str, str],
) -> Dict[str, Dict[str, Any]]:
    groups = {"CAN": _group(existing, "CAN"), "FDCAN": _group(existing, "FDCAN")}
    for instance in managed:
        key = instance.lower()
        active_name = active_sections.get(key, "CAN")
        inactive_name = "FDCAN" if active_name == "CAN" else "CAN"
        active = groups[active_name]
        inactive = groups[inactive_name]

        current = _without_fields(inactive.get(key), _MCAN_MANAGED_FIELDS)
        current.update(_without_fields(active.get(key), _MCAN_MANAGED_FIELDS))
        if key in updates[active_name]:
            current.update(copy.deepcopy(dict(updates[active_name][key])))

        inactive.pop(key, None)
        if current:
            active[key] = current
        else:
            active.pop(key, None)
    return groups


def _disabled_peripherals(
    existing: Mapping[str, Any],
    peripheral_config: Mapping[str, Any],
    active_instances: Optional[Set[str]] = None,
) -> Sequence[str]:
    managed_names = {
        instance.lower()
        for group in ("spi", "i2c", "uart", "mcan")
        for instance in _group(peripheral_config, group)
    }
    existing_disabled = existing.get("disabled_peripherals")
    preserved = (
        [
            str(instance)
            for instance in existing_disabled
            if str(instance).lower() not in managed_names
        ]
        if isinstance(existing_disabled, list)
        else []
    )
    disabled = []
    for group in ("spi", "i2c", "uart", "mcan"):
        for instance, entry in _group(peripheral_config, group).items():
            if (
                not isinstance(entry, Mapping)
                or not bool(entry.get("enabled"))
                or (
                    active_instances is not None
                    and str(instance).lower() not in active_instances
                )
            ):
                disabled.append(str(instance).lower())
    return list(dict.fromkeys(preserved + disabled))


def merge_libxr_config(
    existing: Mapping[str, Any],
    peripheral_config: Mapping[str, Any],
    active_instances: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return a merged LibXR config while preserving every user-owned field."""

    source = _mapping(existing)
    config = _mapping(peripheral_config)
    active = (
        None
        if active_instances is None
        else {str(instance).lower() for instance in active_instances}
    )
    updates = _managed_updates(config, active)
    managed = _managed_instances(config, ("spi", "i2c", "uart", "mcan"))
    mcan_instances = managed["mcan"]

    merged_spi = _merge_group(
        source,
        "SPI",
        managed["spi"],
        updates["SPI"],
        _SPI_MANAGED_FIELDS,
    )
    merged_i2c = _merge_group(
        source,
        "I2C",
        managed["i2c"],
        updates["I2C"],
        _I2C_MANAGED_FIELDS,
    )
    merged_uart = _merge_group(
        source,
        "UART",
        managed["uart"],
        updates["UART"],
        _UART_MANAGED_FIELDS,
    )
    merged_mcan = _merge_mcan_groups(
        source,
        mcan_instances,
        updates,
        _mcan_sections(config),
    )

    project = _group(config, "project")
    selected_functions = project.get("pinmux_functions")
    if not isinstance(selected_functions, list):
        selected_functions = []

    result = source
    if result.get("SYSTEM") is None:
        result["SYSTEM"] = "None"
    result["pinmux_functions"] = copy.deepcopy(selected_functions)
    result["disabled_peripherals"] = list(_disabled_peripherals(source, config, active))
    result["SPI"] = merged_spi
    result["I2C"] = merged_i2c
    result["UART"] = merged_uart
    result["CAN"] = merged_mcan["CAN"]
    result["FDCAN"] = merged_mcan["FDCAN"]
    for section in ("PWM", "GPIO", "device_aliases"):
        if result.get(section) is None:
            result[section] = {}
    return result


def read_libxr_config(path: str) -> Dict[str, Any]:
    """Read a LibXR YAML mapping, returning an empty mapping when absent."""

    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as source:
            value = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise LibxrConfigError("Invalid LibXR YAML: {}".format(path)) from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LibxrConfigError("LibXR YAML root must be an object: {}".format(path))
    return value


def serialize_libxr_config(config: Mapping[str, Any]) -> str:
    """Serialize a LibXR configuration using deterministic block-style YAML."""

    return yaml.safe_dump(
        copy.deepcopy(dict(config)),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def write_libxr_config(path: str, config: Mapping[str, Any]) -> None:
    """Atomically replace a LibXR YAML file in its destination directory."""

    content = serialize_libxr_config(config)
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{}-".format(os.path.basename(target)), suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
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


__all__ = [
    "LibxrConfigError",
    "merge_libxr_config",
    "read_libxr_config",
    "serialize_libxr_config",
    "write_libxr_config",
]
