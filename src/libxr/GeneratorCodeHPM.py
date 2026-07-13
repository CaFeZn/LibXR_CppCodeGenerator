#!/usr/bin/env python3
"""Generate LibXR HPM application code from the common YAML schema."""

import argparse
import copy
import logging
import os
import re
import sys
from typing import Any, Dict, List, Tuple

import yaml


logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

DEFAULT_SETTINGS = {
    "SYSTEM": "None",
    "pinmux_functions": [],
    "disabled_peripherals": [],
    "SPI": {},
    "I2C": {},
    "UART": {},
    "CAN": {},
    "FDCAN": {},
    "PWM": {},
    "GPIO": {},
    "device_aliases": {},
}


def _deep_merge(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_configuration(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as source:
        data = yaml.safe_load(source)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping")
    for section in ("Mcu", "GPIO", "Peripherals"):
        if section not in data:
            raise ValueError("Missing required section: {}".format(section))
    platform = str(data["Mcu"].get("Platform", data["Mcu"].get("Family", ""))).upper()
    if platform != "HPM":
        raise ValueError("GeneratorCodeHPM requires an HPM configuration")
    return data


def load_settings(path: str) -> Dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if not path:
        return settings
    with open(path, "r", encoding="utf-8") as source:
        external = yaml.safe_load(source) or {}
    if not isinstance(external, dict):
        raise ValueError("LibXR configuration root must be a mapping")
    return _deep_merge(settings, external)


def _identifier(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_]", "_", value).lower()
    if result and result[0].isdigit():
        result = "device_" + result
    return result or "device"


def _int_suffix(value: str, default: int = 0) -> int:
    match = re.search(r"(\d+)$", value)
    return int(match.group(1)) if match else default


def _aliases(name: str, settings: Dict[str, Any]) -> List[str]:
    configured = settings.get("device_aliases", {}).get(name, {})
    if isinstance(configured, dict):
        aliases = configured.get("aliases", [name])
    elif isinstance(configured, list):
        aliases = configured
    else:
        aliases = [name]
    result = [str(alias) for alias in aliases]
    return result or [name]


def _selected_pinmux_functions(settings: Dict[str, Any]) -> List[str]:
    configured = settings.get("pinmux_functions", [])
    if not configured and isinstance(settings.get("HPM"), dict):
        configured = settings["HPM"].get("pinmux_functions", [])
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        return []
    return [str(name) for name in configured if str(name)]


def _matches_selected_functions(instance: str, config: Dict[str, Any], selected: set) -> bool:
    functions = config.get("PinmuxFunctions", [])
    if isinstance(functions, str):
        functions = [functions]
    if not isinstance(functions, list):
        return False
    if any(str(function) in selected for function in functions):
        return True
    return "init_{}_pins".format(_identifier(instance)) in selected


def _filter_project_by_pinmux_functions(
    project: Dict[str, Any], settings: Dict[str, Any]
) -> Dict[str, Any]:
    selected_functions = _selected_pinmux_functions(settings)
    configured_disabled = settings.get("disabled_peripherals", [])
    if isinstance(configured_disabled, str):
        configured_disabled = [configured_disabled]
    disabled = {
        str(instance).lower()
        for instance in configured_disabled
        if str(instance)
    } if isinstance(configured_disabled, list) else set()
    if not selected_functions and not disabled:
        return project
    selected = set(selected_functions)
    filtered = copy.deepcopy(project)
    peripherals = filtered.get("Peripherals", {})
    for group_name, group in list(peripherals.items()):
        if not isinstance(group, dict):
            continue
        kept = {
            instance: config
            for instance, config in group.items()
            if isinstance(config, dict)
            and str(instance).lower() not in disabled
            and (not selected or _matches_selected_functions(instance, config, selected))
        }
        if kept:
            peripherals[group_name] = kept
        else:
            peripherals.pop(group_name, None)
    filtered["GPIO"] = {
        name: config
        for name, config in filtered.get("GPIO", {}).items()
        if isinstance(config, dict)
        and (not selected or _matches_selected_functions(name, config, selected))
    }
    return filtered


def _preserve_user_block(existing: str, number: int) -> str:
    pattern = re.compile(
        r"/\* User Code Begin {} \*/(.*?)/\* User Code End {} \*/".format(number, number),
        re.DOTALL,
    )
    match = pattern.search(existing)
    if not match:
        return ""
    lines = match.group(1).strip("\r\n").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _gpio_direction(value: str) -> str:
    supported = {
        "INPUT",
        "OUTPUT_PUSH_PULL",
        "OUTPUT_OPEN_DRAIN",
        "RISING_INTERRUPT",
        "FALL_INTERRUPT",
        "BOTH_INTERRUPT",
    }
    normalized = str(value or "INPUT").upper()
    return normalized if normalized in supported else "INPUT"


def _spi_clock_polarity(value: Any) -> str:
    normalized = str(value or "LOW").upper()
    return normalized if normalized in {"LOW", "HIGH"} else "LOW"


def _spi_clock_phase(value: Any) -> str:
    normalized = str(value or "EDGE_1").upper()
    if normalized in {"EDGE1", "FIRST", "FIRST_EDGE"}:
        normalized = "EDGE_1"
    elif normalized in {"EDGE2", "SECOND", "SECOND_EDGE"}:
        normalized = "EDGE_2"
    return normalized if normalized in {"EDGE_1", "EDGE_2"} else "EDGE_1"


def _spi_mode_to_config(value: Any) -> Tuple[str, str]:
    try:
        mode = int(value)
    except (TypeError, ValueError):
        mode = 0
    if mode < 0 or mode > 3:
        mode = 0
    polarity = "HIGH" if mode >= 2 else "LOW"
    phase = "EDGE_2" if mode % 2 else "EDGE_1"
    return polarity, phase


def _spi_prescaler(value: Any) -> str:
    normalized = str(value or "DIV_4").upper()
    if normalized.isdigit():
        normalized = "DIV_{}".format(normalized)
    supported = {
        "DIV_1",
        "DIV_2",
        "DIV_4",
        "DIV_8",
        "DIV_16",
        "DIV_32",
        "DIV_64",
        "DIV_128",
        "DIV_256",
    }
    return normalized if normalized in supported else "DIV_4"


def _spi_prescaler_from_hz(clock_hz: Any, target_hz: Any) -> str:
    try:
        source = int(clock_hz)
        target = int(target_hz)
    except (TypeError, ValueError):
        return "DIV_4"
    if source <= 0 or target <= 0:
        return "DIV_4"
    for divider in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        if source // divider <= target:
            return "DIV_{}".format(divider)
    return "DIV_256"


def _generate_gpio(
    gpio: Dict[str, Any], settings: Dict[str, Any]
) -> Tuple[List[str], List[Tuple[str, str, List[str]]]]:
    code = []
    devices = []
    for name, config in gpio.items():
        variable = _identifier(name)
        controller = config.get("Controller", "HPM_GPIO0")
        port = config.get("Port", "0")
        pin = config.get("Pin", "0")
        irq = config.get("IRQ")
        arguments = "{}, {}, {}".format(controller, port, pin)
        if irq:
            arguments += ", {}".format(irq)
        code.append("  static LibXR::HPMGPIO {}({});".format(variable, arguments))
        direction = _gpio_direction(
            settings.get("GPIO", {}).get(name, {}).get("direction", config.get("Direction"))
        )
        pull = str(settings.get("GPIO", {}).get(name, {}).get("pull", "NONE")).upper()
        if pull not in {"NONE", "UP", "DOWN"}:
            pull = "NONE"
        code.append(
            "  {}.SetConfig({{LibXR::GPIO::Direction::{}, LibXR::GPIO::Pull::{}}});".format(
                variable, direction, pull
            )
        )
        devices.append((variable, "GPIO", _aliases(name, settings)))
    return code, devices


def _generate_peripherals(
    peripherals: Dict[str, Any], settings: Dict[str, Any]
) -> Tuple[List[str], List[str], List[Tuple[str, str, List[str]]]]:
    resources = []
    code = []
    devices = []
    unsupported = []

    uart_index = 0
    for instance, config in peripherals.get("UART", {}).items():
        if config.get("Enabled") is False:
            continue
        variable = _identifier(instance)
        cfg = settings["UART"].setdefault(variable, {})
        baudrate = int(cfg.setdefault("baudrate", 115200))
        rx_buffer_size = int(cfg.setdefault("rx_buffer_size", 256))
        tx_buffer_size = int(cfg.setdefault("tx_buffer_size", 256))
        tx_queue_size = int(cfg.setdefault("tx_queue_size", 5))
        parity = str(cfg.setdefault("parity", "NO_PARITY")).upper()
        if parity not in {"NO_PARITY", "EVEN", "ODD"}:
            parity = "NO_PARITY"
        data_bits = int(cfg.setdefault("data_bits", 8))
        stop_bits = int(cfg.setdefault("stop_bits", 1))
        rx_dma_channel = uart_index * 2
        tx_dma_channel = rx_dma_channel + 1
        uart_index += 1
        resources.extend(
            [
                "ATTR_PLACE_AT_NONCACHEABLE static uint8_t {}_rx_dma_buffer[{}];".format(
                    variable, rx_buffer_size
                ),
                "ATTR_PLACE_AT_NONCACHEABLE static uint8_t {}_tx_dma_buffer[{}];".format(
                    variable, tx_buffer_size * 2
                ),
            ]
        )
        code.extend(
            [
                "  static LibXR::HPMUART {}(".format(variable),
                "      {}, {}, {},".format(
                    config.get("Base", "HPM_" + instance),
                    config.get("Clock", "clock_" + variable),
                    config.get("IRQ", "IRQn_" + instance),
                ),
                "      LibXR::RawData({0}_rx_dma_buffer, sizeof({0}_rx_dma_buffer)),".format(
                    variable
                ),
                "      LibXR::RawData({0}_tx_dma_buffer, sizeof({0}_tx_dma_buffer)),".format(
                    variable
                ),
                "      {}, {}, {},".format(
                    rx_dma_channel, tx_dma_channel, tx_queue_size
                ),
                "      {{{}U, LibXR::UART::Parity::{}, {}U, {}U}});".format(
                    baudrate, parity, data_bits, stop_bits
                ),
            ]
        )
        devices.append((variable, "UART", _aliases(variable, settings)))

    for instance, config in peripherals.get("I2C", {}).items():
        if config.get("Enabled") is False:
            continue
        variable = _identifier(instance)
        cfg = settings["I2C"].setdefault(variable, {})
        bus_hz = int(cfg.setdefault("bus_hz", 100000))
        code.append(
            "  static LibXR::HPMI2C {}({}, {}, true, {{{}U}});".format(
                variable, config.get("Base", "HPM_" + instance),
                config.get("Clock", "clock_" + variable), bus_hz
            )
        )
        address_mode = str(cfg.setdefault("address_mode", "7bit")).lower()
        if address_mode == "10bit":
            code.append(
                "  ASSERT({}.SetAddressMode(LibXR::HPMI2C::AddressMode::ADDR_10BIT) == LibXR::ErrorCode::OK);".format(
                    variable
                )
            )
        devices.append((variable, "I2C", _aliases(variable, settings)))

    for instance, config in peripherals.get("SPI", {}).items():
        if config.get("Enabled") is False:
            continue
        variable = _identifier(instance)
        cfg = settings["SPI"].setdefault(variable, {})
        size = int(cfg.setdefault("buffer_size", 256))
        resources.extend(
            [
                "static uint8_t {}_rx_buffer[{}];".format(variable, size),
                "static uint8_t {}_tx_buffer[{}];".format(variable, size),
            ]
        )
        auto_board_init = "true"
        use_gpio_cs = bool(cfg.get("use_gpio_cs", config.get("UseGpioCs", False)))
        if use_gpio_cs:
            code.append(
                "  board_init_spi_pins_with_gpio_as_cs({});".format(
                    config.get("Base", "HPM_" + instance)
                )
            )
            auto_board_init = "false"
        mode_polarity, mode_phase = _spi_mode_to_config(cfg.get("spi_mode", 0))
        clock_polarity = _spi_clock_polarity(cfg.get("clock_polarity", mode_polarity))
        clock_phase = _spi_clock_phase(cfg.get("clock_phase", mode_phase))
        if "prescaler" in cfg:
            prescaler = _spi_prescaler(cfg.get("prescaler"))
        else:
            prescaler = _spi_prescaler_from_hz(
                cfg.get("peripheral_clock_hz"), cfg.get("sclk_hz")
            )
        double_buffer = "true" if cfg.get("double_buffer", False) else "false"
        configuration = "      {{LibXR::SPI::ClockPolarity::{}, LibXR::SPI::ClockPhase::{}, LibXR::SPI::Prescaler::{}, {}}}".format(
            clock_polarity,
            clock_phase,
            prescaler,
            double_buffer,
        )
        if use_gpio_cs:
            configuration += (
                ",\n      +[](bool selected) { board_write_spi_cs(BOARD_SPI_CS_PIN, "
                "selected ? BOARD_SPI_CS_ACTIVE_LEVEL : !BOARD_SPI_CS_ACTIVE_LEVEL); }"
            )
        code.extend(
            [
                "  static LibXR::HPMSPI {}(".format(variable),
                "      {}, {},".format(
                    config.get("Base", "HPM_" + instance),
                    config.get("Clock", "clock_" + variable),
                ),
                "      LibXR::RawData({0}_rx_buffer, sizeof({0}_rx_buffer)),".format(variable),
                "      LibXR::RawData({0}_tx_buffer, sizeof({0}_tx_buffer)), {1},".format(
                    variable, auto_board_init
                ),
                configuration + ");",
            ]
        )
        devices.append((variable, "SPI", _aliases(variable, settings)))

    for instance, config in peripherals.get("MCAN", {}).items():
        if config.get("Enabled") is False:
            continue
        variable = _identifier(instance)
        if variable in settings.get("CAN", {}):
            kind = "CAN"
        elif variable in settings.get("FDCAN", {}):
            kind = "FDCAN"
        else:
            kind = str(config.get("Kind", "CAN")).upper()
        setting_key = "FDCAN" if kind == "FDCAN" else "CAN"
        cfg = settings[setting_key].setdefault(variable, {})
        if kind == "FDCAN":
            queue_size = int(cfg.setdefault("queue_size", 8))
            index = int(cfg.setdefault("index", _int_suffix(instance)))
        else:
            queue_size = int(cfg.get("queue_size", 8))
            index = int(cfg.get("index", _int_suffix(instance)))
        class_name = "HPMCANFD" if kind == "FDCAN" else "HPMCAN"
        interface = "FDCAN" if kind == "FDCAN" else "CAN"
        base = config.get("Base", "HPM_" + instance)
        code.extend(
            [
                "  board_init_can({});".format(base),
                "  board_init_can_clock({});".format(base),
            ]
        )
        code.append(
            "  static LibXR::{} {}({}, {}, {}, {}, true, {});".format(
                class_name,
                variable,
                base,
                config.get("Clock", "clock_can{}".format(index)),
                index,
                config.get("IRQ", "IRQn_" + instance),
                queue_size,
            )
        )
        config_type = "FDCAN" if kind == "FDCAN" else "CAN"
        bitrate = int(cfg.setdefault("bitrate", 500000))
        code.extend(
            [
                "  LibXR::{}::Configuration {}_config{{}};".format(config_type, variable),
                "  {0}_config.bitrate = {1}U;".format(variable, bitrate),
            ]
        )
        if kind == "FDCAN":
            sample_point = float(cfg.setdefault("sample_point", 0.875))
            data_bitrate = int(cfg.setdefault("data_bitrate", 2000000))
            data_sample_point = float(cfg.setdefault("data_sample_point", 0.75))
            brs = "true" if cfg.setdefault("brs", True) else "false"
            esi = "true" if cfg.setdefault("esi", False) else "false"
            code.extend(
                [
                    "  {0}_config.sample_point = {1}f;".format(variable, sample_point),
                    "  {0}_config.data_bitrate = {1}U;".format(variable, data_bitrate),
                    "  {0}_config.data_sample_point = {1}f;".format(
                        variable, data_sample_point
                    ),
                    "  {0}_config.fd_mode.fd_enabled = true;".format(variable),
                    "  {0}_config.fd_mode.brs = {1};".format(variable, brs),
                    "  {0}_config.fd_mode.esi = {1};".format(variable, esi),
                ]
            )
        elif "sample_point" in cfg:
            sample_point = float(cfg["sample_point"])
            code.append("  {0}_config.sample_point = {1}f;".format(variable, sample_point))
        for field in ("loopback", "listen_only", "one_shot"):
            if cfg.get(field, False):
                code.append("  {0}_config.mode.{1} = true;".format(variable, field))
        code.append(
            "  ASSERT({0}.SetConfig({0}_config) == LibXR::ErrorCode::OK);".format(variable)
        )
        devices.append((variable, interface, _aliases(variable, settings)))

    for instance, config in peripherals.get("CAN", {}).items():
        if config.get("Enabled") is False:
            continue
        variable = _identifier(instance)
        cfg = settings["CAN"].setdefault(variable, {})
        queue_size = int(cfg.get("queue_size", 8))
        index = int(cfg.get("index", _int_suffix(instance)))
        base = config.get("Base", "HPM_" + instance)
        code.extend(
            [
                "  board_init_can({});".format(base),
                "  board_init_can_clock({});".format(base),
            ]
        )
        code.append(
            "  static LibXR::HPMCAN {}({}, {}, {}, {}, true, {});".format(
                variable,
                base,
                config.get("Clock", "clock_can{}".format(index)),
                index,
                config.get("IRQ", "IRQn_" + instance),
                queue_size,
            )
        )
        bitrate = int(cfg.setdefault("bitrate", 500000))
        code.extend(
            [
                "  LibXR::CAN::Configuration {}_config{{}};".format(variable),
                "  {0}_config.bitrate = {1}U;".format(variable, bitrate),
            ]
        )
        if "sample_point" in cfg:
            sample_point = float(cfg["sample_point"])
            code.append("  {0}_config.sample_point = {1}f;".format(variable, sample_point))
        for field in ("loopback", "listen_only", "one_shot"):
            if cfg.get(field, False):
                code.append("  {0}_config.mode.{1} = true;".format(variable, field))
        code.append(
            "  ASSERT({0}.SetConfig({0}_config) == LibXR::ErrorCode::OK);".format(variable)
        )
        devices.append((variable, "CAN", _aliases(variable, settings)))

    for instance, config in peripherals.get("PWM", {}).items():
        if config.get("Enabled") is False:
            continue
        variable = _identifier(instance)
        cfg = settings["PWM"].setdefault(variable, {})
        frequency = int(cfg.setdefault("frequency_hz", 1000))
        channel = config.get("Channel", _int_suffix(instance))
        compare = config.get("CompareIndex", channel)
        code.extend(
            [
                "  static LibXR::HPMPWM {}(".format(variable),
                "      reinterpret_cast<LibXRHpmPwmType*>({}), {}, {}, {});".format(
                    config.get("Base", "HPM_GPTMR0"),
                    config.get("Clock", "clock_gptmr0"),
                    channel,
                    compare,
                ),
                "  {}.SetConfig({{{}U}});".format(variable, frequency),
                "  {}.Enable();".format(variable),
            ]
        )
        devices.append((variable, "PWM", _aliases(variable, settings)))

    supported = {"UART", "I2C", "SPI", "MCAN", "CAN", "PWM"}
    for peripheral_type, instances in peripherals.items():
        active = any(config.get("Enabled") is not False for config in instances.values())
        if peripheral_type not in supported and active:
            unsupported.append(peripheral_type)
    if unsupported:
        logging.warning(
            "No LibXR HPM backend is available for: %s; keeping them in YAML only",
            ", ".join(sorted(unsupported)),
        )
    return resources, code, devices


def _hardware_container(devices: List[Tuple[str, str, List[str]]]) -> List[str]:
    if not devices:
        return []
    lines = ["  static LibXR::HardwareContainer peripherals("]
    entries = []
    for variable, interface, aliases in devices:
        alias_text = ", ".join('"{}"'.format(alias.replace('"', '\\"')) for alias in aliases)
        entries.append(
            "      LibXR::Entry<LibXR::{}>{{{}, {{{}}}}}".format(interface, variable, alias_text)
        )
    lines.append(",\n".join(entries) + ");")
    return lines


def generate_code(
    project: Dict[str, Any],
    settings: Dict[str, Any],
    use_xrobot: bool = False,
    use_hw_cntr: bool = False,
    existing: str = "",
) -> str:
    project = _filter_project_by_pinmux_functions(project, settings)
    use_hw_cntr = use_hw_cntr or use_xrobot
    resources, peripheral_code, devices = _generate_peripherals(
        project.get("Peripherals", {}), settings
    )
    gpio_code, gpio_devices = _generate_gpio(project.get("GPIO", {}), settings)
    devices = gpio_devices + devices

    headers = [
        '#include "app_main.h"',
        '#include "board.h"',
        '#include "hpm_timebase.hpp"',
        '#include "libxr.hpp"',
    ]
    peripheral_groups = project.get("Peripherals", {})
    if any(
        config.get("Enabled") is not False
        for config in peripheral_groups.get("UART", {}).values()
    ):
        headers.append('#include "hpm_uart.hpp"')
    if gpio_code:
        headers.append('#include "hpm_gpio.hpp"')
    if any(
        config.get("Enabled") is not False
        for config in peripheral_groups.get("I2C", {}).values()
    ):
        headers.append('#include "hpm_i2c.hpp"')
    if any(
        config.get("Enabled") is not False
        for config in peripheral_groups.get("SPI", {}).values()
    ):
        headers.append('#include "hpm_spi.hpp"')
    if any(
        config.get("Enabled") is not False
        for config in peripheral_groups.get("PWM", {}).values()
    ):
        headers.append('#include "hpm_pwm.hpp"')
    if any(
        config.get("Enabled") is not False
        for config in peripheral_groups.get("CAN", {}).values()
    ):
        headers.append('#include "hpm_can.hpp"')
    if any(
        config.get("Enabled") is not False
        for config in peripheral_groups.get("MCAN", {}).values()
    ):
        headers.append('#include "hpm_mcan.hpp"')
    if use_hw_cntr:
        headers.append('#include "app_framework.hpp"')
    if use_xrobot:
        headers.append('#include "xrobot_main.hpp"')

    user1 = _preserve_user_block(existing, 1)
    user2 = _preserve_user_block(existing, 2)
    user3 = _preserve_user_block(existing, 3)
    lines = headers + ["", "/* User Code Begin 1 */"]
    if user1:
        lines.append(user1)
    lines.extend(["/* User Code End 1 */", ""])
    lines.extend(resources)
    if resources:
        lines.append("")
    lines.extend(
        [
            'extern "C" void app_main(void)',
            "{",
            "  /* User Code Begin 2 */",
        ]
    )
    if user2:
        lines.append(user2)
    lines.extend(
        [
            "  /* User Code End 2 */",
            "  static LibXR::HPMTimebase timebase;",
            "  UNUSED(timebase);",
        ]
    )
    lines.extend(gpio_code)
    lines.extend(peripheral_code)
    if use_hw_cntr:
        lines.extend(_hardware_container(devices))
    lines.append("  /* User Code Begin 3 */")
    if user3:
        lines.append(user3)
    elif use_xrobot:
        lines.append("  XRobotMain(peripherals);")
    else:
        lines.extend(["  while (true)", "  {", "    LibXR::Thread::Sleep(UINT32_MAX);", "  }"])
    lines.extend(["  /* User Code End 3 */", "}", ""])
    return "\n".join(lines)


def generate_header(output_dir: str) -> None:
    content = """#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void app_main(void);

#ifdef __cplusplus
}
#endif
"""
    with open(os.path.join(output_dir, "app_main.h"), "w", encoding="utf-8") as target:
        target.write(content)


def write_outputs(
    project: Dict[str, Any],
    output: str,
    config_source: str = "",
    use_xrobot: bool = False,
    use_hw_cntr: bool = False,
) -> None:
    settings = load_settings(config_source)
    output = os.path.abspath(output)
    output_dir = os.path.dirname(output)
    os.makedirs(output_dir, exist_ok=True)
    existing = ""
    if os.path.isfile(output):
        with open(output, "r", encoding="utf-8") as source:
            existing = source.read()
    with open(output, "w", encoding="utf-8") as target:
        target.write(generate_code(project, settings, use_xrobot, use_hw_cntr, existing))
    generate_header(output_dir)
    with open(os.path.join(output_dir, "libxr_config.yaml"), "w", encoding="utf-8") as target:
        yaml.safe_dump(settings, target, allow_unicode=True, sort_keys=False)


def main() -> None:
    from libxr.PackageInfo import LibXRPackageInfo

    LibXRPackageInfo.check_and_print()
    parser = argparse.ArgumentParser(description="Generate LibXR code for an HPM project")
    parser.add_argument("-i", "--input", required=True, help="Parsed HPM YAML")
    parser.add_argument("-o", "--output", required=True, help="Output app_main.cpp")
    parser.add_argument("--xrobot", action="store_true", help="Generate XRobot integration")
    parser.add_argument("--hw-cntr", action="store_true", help="Generate HardwareContainer")
    parser.add_argument("--libxr-config", default="", help="LibXR settings YAML")
    args = parser.parse_args()
    try:
        project = load_configuration(args.input)
        write_outputs(project, args.output, args.libxr_config, args.xrobot, args.hw_cntr)
    except (OSError, ValueError, yaml.YAMLError) as error:
        logging.error("HPM code generation failed: %s", error)
        sys.exit(1)
    logging.info("Successfully generated: %s", os.path.abspath(args.output))


if __name__ == "__main__":
    main()
