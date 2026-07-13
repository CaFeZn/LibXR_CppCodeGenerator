#!/usr/bin/env python3
"""Parse HPM Pinmux Tool projects into LibXR's common YAML schema."""

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional

import yaml


logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

_DEFINE_RE = re.compile(r"^\s*#define\s+([A-Za-z_]\w*)\s+(.+?)\s*$")
_INSTANCE_RE = re.compile(r"^(UART|I2C|SPI|MCAN|CAN|ADC|USB|GPTMR|PWM)(\d+)$")
_APP_TYPES = ("UART", "I2C", "SPI", "CAN", "ADC16")


def _strip_c_comments(value: str) -> str:
    value = re.sub(r"/\*.*?\*/", "", value)
    return value.split("//", 1)[0].strip()


def _read_defines(path: Optional[str]) -> Dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    defines = {}
    with open(path, "r", encoding="utf-8", errors="replace") as source:
        for line in source:
            match = _DEFINE_RE.match(line)
            if match:
                defines[match.group(1)] = _strip_c_comments(match.group(2))
    return defines


def _resolve_define(name: str, defines: Dict[str, str]) -> str:
    value = defines.get(name, name)
    seen = {name}
    while value in defines and value not in seen:
        seen.add(value)
        value = defines[value]
    return value.strip("() ")


def _find_nearby(start: str, filename: str) -> Optional[str]:
    directory = os.path.dirname(os.path.abspath(start))
    while True:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _find_project_cmake(start: str) -> Optional[str]:
    directory = os.path.dirname(os.path.abspath(start))
    fallback = None
    while True:
        candidate = os.path.join(directory, "CMakeLists.txt")
        if os.path.isfile(candidate):
            fallback = candidate
            with open(candidate, "r", encoding="utf-8", errors="replace") as source:
                if re.search(r"set\s*\(\s*APP_NAME\b", source.read(), re.IGNORECASE):
                    return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return fallback
        directory = parent


def _parse_app_name(cmake_path: Optional[str]) -> str:
    if not cmake_path:
        return ""
    with open(cmake_path, "r", encoding="utf-8", errors="replace") as source:
        text = source.read()
    match = re.search(r"set\s*\(\s*APP_NAME\s+([^\s\)]+)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _signal_role(signal: str) -> str:
    role = signal.rsplit(".", 1)[-1]
    return re.sub(r"\[(\d+)\]", r"\1", role)


def _collect_pinmux(functions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    peripherals = defaultdict(lambda: {"Pins": {}, "PinmuxFunctions": []})
    for function_name, function in functions.items():
        selected = function.get("selectPins", {}) if isinstance(function, dict) else {}
        annotation = function.get("annotation", "") if isinstance(function, dict) else ""
        for pad, pin_data in selected.items():
            signal = pin_data.get("signal", "") if isinstance(pin_data, dict) else ""
            instance = signal.split(".", 1)[0]
            if not _INSTANCE_RE.match(instance):
                continue
            entry = peripherals[instance]
            entry["Pins"][_signal_role(signal)] = pad
            if function_name not in entry["PinmuxFunctions"]:
                entry["PinmuxFunctions"].append(function_name)
            if annotation:
                entry.setdefault("Annotations", [])
                if annotation not in entry["Annotations"]:
                    entry["Annotations"].append(annotation)
    return dict(peripherals)


def _macro_if_present(defines: Dict[str, str], name: str) -> Optional[str]:
    return name if name in defines else None


def _instance_from_base(base_macro: str, defines: Dict[str, str]) -> Optional[str]:
    value = _resolve_define(base_macro, defines)
    match = re.search(r"HPM_(UART|I2C|SPI|MCAN|CAN|ADC|USB|GPTMR|PWM)(\d+)", value)
    return "".join(match.groups()) if match else None


def _add_app_peripherals(
    output: Dict[str, Dict[str, Any]],
    defines: Dict[str, str],
    discovered: Dict[str, Dict[str, Any]],
    app_name: str,
) -> None:
    for macro_type in _APP_TYPES:
        base_macro = "BOARD_APP_{}_BASE".format(macro_type)
        if base_macro not in defines:
            continue
        instance = _instance_from_base(base_macro, defines)
        if not instance:
            continue
        instance_type = _INSTANCE_RE.match(instance).group(1)
        output_type = "ADC" if instance_type == "ADC" else instance_type
        config = dict(discovered.get(instance, {}))
        config["Base"] = base_macro
        prefix = "BOARD_APP_{}".format(macro_type)
        clock = _macro_if_present(defines, prefix + "_CLK_NAME")
        irq = _macro_if_present(defines, prefix + "_IRQ") or _macro_if_present(
            defines, prefix + "_IRQn"
        )
        if clock:
            config["Clock"] = clock
        if irq:
            config["IRQ"] = irq
        if instance.startswith("MCAN"):
            config["Kind"] = "FDCAN" if "canfd" in app_name.lower() else "CAN"
            output_type = "MCAN"
        if instance.startswith("SPI") and "BOARD_SPI_CS_GPIO_CTRL" in defines:
            config["UseGpioCs"] = True
        output.setdefault(output_type, {})[instance] = config


def _instance_index(instance: str) -> int:
    match = re.search(r"(\d+)$", instance)
    return int(match.group(1)) if match else 0


def _mcan_kind(instance_config: Dict[str, Any], app_name: str) -> str:
    hints = [app_name]
    hints.extend(str(value) for value in instance_config.get("PinmuxFunctions", []))
    hints.extend(str(value) for value in instance_config.get("Annotations", []))
    text = " ".join(hints).lower()
    return "FDCAN" if any(marker in text for marker in ("canfd", "fdcan", "can fd")) else "CAN"


def _add_discovered_mcan(
    output: Dict[str, Dict[str, Any]], discovered: Dict[str, Dict[str, Any]], app_name: str
) -> None:
    for instance, discovered_config in discovered.items():
        if not instance.startswith("MCAN"):
            continue
        mcan_group = output.setdefault("MCAN", {})
        if instance in mcan_group:
            continue
        index = _instance_index(instance)
        config = dict(discovered_config)
        config["Base"] = "HPM_{}".format(instance)
        config["Clock"] = "clock_can{}".format(index)
        config["IRQ"] = "IRQn_{}".format(instance)
        config["Kind"] = _mcan_kind(config, app_name)
        mcan_group[instance] = config


def _add_discovered_communication(
    output: Dict[str, Dict[str, Any]], discovered: Dict[str, Dict[str, Any]]
) -> None:
    for instance, discovered_config in discovered.items():
        match = _INSTANCE_RE.match(instance)
        if not match or match.group(1) not in {"UART", "I2C", "SPI"}:
            continue
        peripheral_type = match.group(1)
        group = output.setdefault(peripheral_type, {})
        if instance in group:
            continue
        config = dict(discovered_config)
        config["Base"] = "HPM_{}".format(instance)
        config["Clock"] = "clock_{}".format(instance.lower())
        if peripheral_type == "UART":
            config["IRQ"] = "IRQn_{}".format(instance)
        group[instance] = config


def _add_gpio(
    gpio: Dict[str, Dict[str, Any]], defines: Dict[str, str], prefix: str, direction: str
) -> None:
    controller = prefix + "_GPIO_CTRL"
    index = prefix + "_GPIO_INDEX"
    pin = prefix + "_GPIO_PIN"
    if pin not in defines and prefix + "_PIN" in defines:
        pin = prefix + "_PIN"
    if controller not in defines:
        return
    if index in defines and pin in defines:
        port_expression = index
        pin_expression = pin
        pad = None
    elif pin in defines and "IOC_PAD_" in _resolve_define(pin, defines):
        port_expression = "GPIO_GET_PORT_INDEX({})".format(pin)
        pin_expression = "GPIO_GET_PIN_INDEX({})".format(pin)
        pad = _resolve_define(pin, defines).replace("IOC_PAD_", "")
    else:
        return
    name = prefix.lower().replace("board_", "", 1)
    config = {
        "Controller": controller,
        "Port": port_expression,
        "Pin": pin_expression,
        "Direction": direction,
    }
    irq = prefix + "_GPIO_IRQ"
    if irq in defines:
        config["IRQ"] = irq
    if pad:
        config["Pad"] = pad
    gpio[name] = config


def _add_pwm(peripherals: Dict[str, Dict[str, Any]], defines: Dict[str, str]) -> None:
    base = "BOARD_GPTMR_PWM"
    channel = "BOARD_GPTMR_PWM_CHANNEL"
    clock = "BOARD_GPTMR_PWM_CLK_NAME"
    if not all(name in defines for name in (base, channel, clock)):
        return
    resolved = _resolve_define(base, defines)
    match = re.search(r"HPM_GPTMR(\d+)", resolved)
    if not match:
        return
    name = "GPTMR{}_CH{}".format(match.group(1), _resolve_define(channel, defines))
    peripherals.setdefault("PWM", {})[name] = {
        "Base": base,
        "Clock": clock,
        "Channel": channel,
        "CompareIndex": channel,
    }


def parse_hpmpc_file(
    hpmpc_path: str,
    board_header: Optional[str] = None,
    cmake_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse one .hpmpc file without copying credential fields into the result."""
    with open(hpmpc_path, "r", encoding="utf-8-sig") as source:
        root = json.load(source)
    content = root.get("content")
    if not isinstance(content, dict):
        raise ValueError("Invalid HPM configuration: missing content object")

    info = content.get("info", {})
    functions = content.get("pinmux", {}).get("functions", {})
    if not isinstance(functions, dict):
        raise ValueError("Invalid HPM configuration: pinmux.functions must be an object")

    if board_header is None:
        board_header = _find_nearby(hpmpc_path, "board.h")
    if cmake_path is None:
        cmake_path = _find_project_cmake(hpmpc_path)
    defines = _read_defines(board_header)
    discovered = _collect_pinmux(functions)
    app_name = _parse_app_name(cmake_path)

    peripherals: Dict[str, Dict[str, Any]] = {}
    _add_app_peripherals(peripherals, defines, discovered, app_name)
    if defines:
        _add_discovered_mcan(peripherals, discovered, app_name)
        _add_discovered_communication(peripherals, discovered)
    else:
        for instance, config in discovered.items():
            peripheral_type = _INSTANCE_RE.match(instance).group(1)
            inactive_config = dict(config)
            inactive_config["Enabled"] = False
            peripherals.setdefault(peripheral_type, {})[instance] = inactive_config

    gpio: Dict[str, Dict[str, Any]] = {}
    _add_gpio(gpio, defines, "BOARD_APP", "INPUT")
    _add_gpio(gpio, defines, "BOARD_SPI_CS", "OUTPUT_PUSH_PULL")
    _add_pwm(peripherals, defines)

    clock_functions = content.get("clock", {}).get("functions", {})
    return {
        "Mcu": {
            "Platform": "HPM",
            "Family": "HPM",
            "Type": info.get("socName", "Unknown"),
            "Package": info.get("packageName", "Unknown"),
            "SDK": info.get("sdkName", "Unknown"),
        },
        "GPIO": gpio,
        "Peripherals": peripherals,
        "HPM": {
            "ConfigFile": os.path.basename(hpmpc_path),
            "ProjectName": info.get("projectName", ""),
            "BoardHeader": os.path.basename(board_header) if board_header else None,
            "PinmuxFunctions": list(functions.keys()),
            "ClockFunctions": list(clock_functions.keys())
            if isinstance(clock_functions, dict)
            else [],
        },
    }


def find_hpmpc_files(directory: str) -> Iterable[str]:
    for root, dirs, files in os.walk(directory):
        dirs[:] = [
            name
            for name in dirs
            if name not in {".git", "LibXR"}
            and not name.lower().startswith(("build", "hpm_sdk"))
        ]
        for filename in files:
            if filename.endswith(".hpmpc"):
                yield os.path.join(root, filename)


def save_to_yaml(data: Dict[str, Any], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as target:
        yaml.safe_dump(data, target, allow_unicode=True, sort_keys=False)


def main() -> None:
    from libxr.PackageInfo import LibXRPackageInfo

    LibXRPackageInfo.check_and_print()
    parser = argparse.ArgumentParser(description="Parse an HPM Pinmux Tool project")
    parser.add_argument("-d", "--directory", required=True, help="HPM project directory")
    parser.add_argument("-i", "--input", help="Explicit .hpmpc file")
    parser.add_argument("-o", "--output", help="Output YAML path")
    parser.add_argument("--board-header", help="Explicit board.h path")
    args = parser.parse_args()

    if args.input:
        hpmpc_path = os.path.abspath(args.input)
    else:
        matches = list(find_hpmpc_files(os.path.abspath(args.directory)))
        if len(matches) != 1:
            logging.error("Expected exactly one .hpmpc file, found %d", len(matches))
            sys.exit(1)
        hpmpc_path = matches[0]
    if not os.path.isfile(hpmpc_path):
        logging.error("HPM configuration not found: %s", hpmpc_path)
        sys.exit(1)

    try:
        data = parse_hpmpc_file(hpmpc_path, args.board_header)
        output = args.output or os.path.splitext(hpmpc_path)[0] + ".yaml"
        save_to_yaml(data, output)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logging.error("HPM configuration parsing failed: %s", error)
        sys.exit(1)

    count = sum(len(group) for group in data["Peripherals"].values())
    logging.info("Parsed %s: %d GPIO(s), %d peripheral(s)", hpmpc_path, len(data["GPIO"]), count)
    logging.info("Configuration exported to: %s", output)


if __name__ == "__main__":
    main()
