"""Reconcile stale HPM Pinmux Tool data with generated pinmux C evidence."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .hpmpc import HpmpcData, PinmuxPeripheral

_COMMUNICATION_INSTANCE_RE = re.compile(
    r"^(?P<instance>(?:UART|I2C|SPI|MCAN|CAN)\d+)_(?P<role>[A-Z0-9_]+)$"
)
_COMMUNICATION_TYPES = {"UART", "I2C", "SPI", "MCAN", "CAN"}
_FUNCTION_RE = re.compile(
    r"^[ \t]*void\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*" r"\(\s*void\s*\)\s*\{",
    re.MULTILINE,
)
_FUNC_CTL_WRITE_RE = re.compile(r"\.\s*FUNC_CTL\s*=")
_FUNC_CTL_ASSIGNMENT_RE = re.compile(
    r"HPM_(?P<controller>IOC|PIOC|BIOC)\s*->\s*PAD\s*"
    r"\[\s*(?:[A-Z]+_)?PAD_(?P<pad>P[A-Z][0-9]{2})\s*\]\s*"
    r"\.\s*FUNC_CTL\s*=\s*(?P<rhs>[^;]+);"
)
_CONTROL_MACRO_RE = re.compile(
    r"\b(?P<controller>IOC|PIOC|BIOC)_(?P<pad>P[A-Z][0-9]{2})_"
    r"FUNC_CTL_(?P<target>[A-Z][A-Z0-9_]*)\b"
)
_ANALOG_MASK_RE = re.compile(r"\b(?:IOC|PIOC|BIOC)_PAD_FUNC_CTL_ANALOG_MASK\b")
_KNOWN_NONCOMMUNICATION_RE = re.compile(
    r"^(?:GPIO_[A-Z]_[0-9]+|PGPIO_[A-Z]_[0-9]+|SOC_GPIO_[A-Z]_[0-9]+|"
    r"GPTMR[0-9]+_.+|PWM[0-9]+_.+|USB[0-9]+_.+|ACMP_.+|SOC_P[A-Z]_[0-9]+|"
    r"SOC_REF[0-9]+)$"
)
_GPIO_RE = re.compile(r"^GPIO_[A-Z]_[0-9]+$")
_BOARD_SPI_CS_RE = re.compile(
    r"^[ \t]*#define\s+BOARD_SPI_CS_PIN\s+[^\r\n]*?"
    r"(?:[A-Z]+_)?PAD_(?P<pad>P[A-Z][0-9]{2})\b",
    re.MULTILINE,
)


@dataclass
class _FunctionEvidence:
    communication: Dict[str, Dict[str, str]] = field(default_factory=dict)
    gpio_pads: List[str] = field(default_factory=list)


def _append_unique(values: List[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _function_bodies(source: str) -> List[Tuple[str, Optional[str]]]:
    result: List[Tuple[str, Optional[str]]] = []
    for match in _FUNCTION_RE.finditer(source):
        open_brace = source.find("{", match.start(), match.end())
        depth = 0
        end = None
        for index in range(open_brace, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        body = None if end is None else source[open_brace + 1 : end]
        result.append((match.group("name"), body))
    return result


def _normalize_role(role: str) -> str:
    match = re.match(r"^CS_([0-9]+)$", role)
    return "CS{}".format(match.group(1)) if match else role


def _function_names_instance(function_name: str, instance: str) -> bool:
    return instance.lower() in function_name.lower().split("_")


def _parse_function(body: Optional[str]) -> Optional[_FunctionEvidence]:
    if body is None:
        return None
    assignments = list(_FUNC_CTL_ASSIGNMENT_RE.finditer(body))
    if not assignments or len(assignments) != len(_FUNC_CTL_WRITE_RE.findall(body)):
        return None

    evidence = _FunctionEvidence()
    for assignment in assignments:
        rhs = assignment.group("rhs")
        if _ANALOG_MASK_RE.search(rhs):
            return None
        macros = list(_CONTROL_MACRO_RE.finditer(rhs))
        if len(macros) != 1:
            return None
        macro = macros[0]
        pad = assignment.group("pad")
        if (
            macro.group("controller") != assignment.group("controller")
            or macro.group("pad") != pad
        ):
            return None

        target = macro.group("target")
        communication = _COMMUNICATION_INSTANCE_RE.match(target)
        if communication is not None:
            instance = communication.group("instance")
            role = _normalize_role(communication.group("role"))
            pins = evidence.communication.setdefault(instance, {})
            if role in pins and pins[role] != pad:
                return None
            pins[role] = pad
            continue
        if not _KNOWN_NONCOMMUNICATION_RE.match(target):
            return None
        if _GPIO_RE.match(target):
            _append_unique(evidence.gpio_pads, pad)
    return evidence


def _recover_gpio_spi_cs(
    function_name: str, evidence: _FunctionEvidence, board_header: str
) -> None:
    if "with_gpio_as_cs" not in function_name:
        return
    spi_instances = [
        instance for instance in evidence.communication if instance.startswith("SPI")
    ]
    if len(spi_instances) != 1 or len(evidence.gpio_pads) != 1:
        return
    pins = evidence.communication[spi_instances[0]]
    if "SCLK" not in pins or not ({"MISO", "MOSI"} & set(pins)):
        return
    configured_cs = _BOARD_SPI_CS_RE.search(board_header)
    gpio_pad = evidence.gpio_pads[0]
    if configured_cs is not None and configured_cs.group("pad") != gpio_pad:
        return
    if not any(role.startswith("CS") for role in pins):
        pins["CS0"] = gpio_pad


def _rebuild_peripheral(data: HpmpcData, peripheral: PinmuxPeripheral) -> None:
    pins: Dict[str, str] = {}
    annotations: List[str] = []
    for function_name in peripheral.functions:
        function_pins = peripheral.function_pins.get(function_name, {})
        pins.update(function_pins)
        for annotation in data.function_annotations.get(function_name, []):
            _append_unique(annotations, annotation)
        pin_annotations = data.pin_annotations.get(function_name, {})
        for role, pad in function_pins.items():
            for annotation in pin_annotations.get((peripheral.instance, role, pad), []):
                _append_unique(annotations, annotation)
    peripheral.pins = pins
    peripheral.annotations = annotations


def reconcile_pinmux_data(
    data: HpmpcData, pinmux_source: str, board_header: str = ""
) -> HpmpcData:
    """Use only confidently decoded C functions to correct communication mappings."""

    parsed_functions = _function_bodies(pinmux_source)
    by_instance = {peripheral.instance: peripheral for peripheral in data.peripherals}

    for function_name, body in parsed_functions:
        if function_name not in data.pinmux_functions:
            data.pinmux_functions.append(function_name)
        evidence = _parse_function(body)
        if evidence is None:
            continue
        _recover_gpio_spi_cs(function_name, evidence, board_header)
        hpmpc_pins = {
            instance: dict(peripheral.function_pins[function_name])
            for instance, peripheral in by_instance.items()
            if function_name in peripheral.function_pins
        }

        for existing in by_instance.values():
            if existing.type not in _COMMUNICATION_TYPES:
                continue
            existing.function_pins.pop(function_name, None)
            if function_name in existing.functions:
                existing.functions.remove(function_name)

        for instance, function_pins in evidence.communication.items():
            function_pins = dict(function_pins)
            if _function_names_instance(function_name, instance):
                # The C file may lag a newly saved .hpmpc; use it to prove
                # function membership, but keep the latest route for that instance.
                function_pins.update(hpmpc_pins.get(instance, {}))
            peripheral = by_instance.get(instance)
            if peripheral is None:
                match = re.match(r"^(UART|I2C|SPI|MCAN|CAN)([0-9]+)$", instance)
                if match is None:
                    continue
                peripheral = PinmuxPeripheral(
                    instance=instance,
                    type=match.group(1),
                    index=int(match.group(2)),
                    pins={},
                    functions=[],
                    annotations=[],
                )
                by_instance[instance] = peripheral
            peripheral.function_pins[function_name] = function_pins
            _append_unique(peripheral.functions, function_name)

    peripherals = [
        peripheral
        for peripheral in by_instance.values()
        if peripheral.type not in _COMMUNICATION_TYPES or peripheral.functions
    ]
    for peripheral in peripherals:
        _rebuild_peripheral(data, peripheral)
    data.peripherals = sorted(peripherals, key=lambda peripheral: peripheral.instance)
    return data


__all__ = ["reconcile_pinmux_data"]
