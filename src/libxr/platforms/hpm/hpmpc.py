"""Parse HPM Pinmux Tool files into a credential-free project model."""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

_INSTANCE_RE = re.compile(r"^(UART|I2C|SPI|MCAN|CAN|ADC|USB|GPTMR|PWM)(\d+)$")
_SIGNAL_INDEX_RE = re.compile(r"\[(\d+)\]")


class HpmpcParseError(ValueError):
    """Raised when an HPM Pinmux Tool file cannot be parsed."""


@dataclass
class PinmuxPeripheral:
    """One peripheral instance discovered from pinmux functions."""

    instance: str
    type: str
    index: int
    pins: Dict[str, str]
    functions: List[str]
    annotations: List[str]
    function_pins: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance": self.instance,
            "type": self.type,
            "index": self.index,
            "pins": dict(self.pins),
            "functions": list(self.functions),
            "annotations": list(self.annotations),
            "function_pins": {
                name: dict(pins) for name, pins in self.function_pins.items()
            },
        }


@dataclass
class HpmpcData:
    """Credential-free data needed to inspect and configure an HPM project."""

    soc_name: str
    package_name: str
    sdk_name: str
    project_name: str
    pinmux_functions: List[str]
    peripherals: List[PinmuxPeripheral]
    clock_functions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "soc": self.soc_name,
            "package": self.package_name,
            "sdk": self.sdk_name,
            "project_name": self.project_name,
            "pinmux_functions": list(self.pinmux_functions),
            "peripherals": [peripheral.to_dict() for peripheral in self.peripherals],
            "clock_functions": list(self.clock_functions),
        }


def _object(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _metadata_text(value: Any, default: str) -> str:
    return default if value is None else str(value)


def _signal_role(signal: str) -> str:
    role = signal.rsplit(".", 1)[-1]
    return _SIGNAL_INDEX_RE.sub(r"\1", role)


def parse_hpmpc(path: str) -> HpmpcData:
    """Read one .hpmpc file without retaining credential or signing fields."""

    try:
        with open(path, "r", encoding="utf-8-sig") as source:
            root = json.load(source)
    except json.JSONDecodeError as error:
        raise HpmpcParseError(
            "Invalid HPM Pinmux Tool JSON: {}".format(path)
        ) from error

    if not isinstance(root, dict):
        raise HpmpcParseError("HPM Pinmux Tool root must be an object: {}".format(path))

    content_value = root.get("content")
    if not isinstance(content_value, dict):
        raise HpmpcParseError(
            "HPM Pinmux Tool file is missing content: {}".format(path)
        )
    content = content_value
    info = _object(content.get("info"))
    pinmux = _object(content.get("pinmux"))
    functions_value = pinmux.get("functions", {})
    if not isinstance(functions_value, dict):
        raise HpmpcParseError(
            "HPM Pinmux Tool pinmux.functions must be an object: {}".format(path)
        )
    functions = functions_value
    clock = _object(content.get("clock"))
    clock_functions = _object(clock.get("functions"))

    by_instance: Dict[str, PinmuxPeripheral] = {}
    for function_name, function_value in functions.items():
        function = _object(function_value)
        selected_pins = _object(function.get("selectPins"))
        annotation_value = function.get("annotation")
        annotation = annotation_value if isinstance(annotation_value, str) else ""

        for pad, pin_value in selected_pins.items():
            pin = _object(pin_value)
            signal_value = pin.get("signal")
            signal = signal_value if isinstance(signal_value, str) else ""
            instance = signal.split(".", 1)[0]
            match = _INSTANCE_RE.match(instance)
            if not match:
                continue

            peripheral = by_instance.get(instance)
            if peripheral is None:
                peripheral = PinmuxPeripheral(
                    instance=instance,
                    type=match.group(1),
                    index=int(match.group(2)),
                    pins={},
                    functions=[],
                    annotations=[],
                )
                by_instance[instance] = peripheral

            role = _signal_role(signal)
            peripheral.pins[role] = str(pad)
            peripheral.function_pins.setdefault(str(function_name), {})[role] = str(pad)
            if function_name not in peripheral.functions:
                peripheral.functions.append(function_name)
            if annotation and annotation not in peripheral.annotations:
                peripheral.annotations.append(annotation)
            pin_annotation_value = pin.get("annotation")
            pin_annotation = (
                pin_annotation_value if isinstance(pin_annotation_value, str) else ""
            )
            if pin_annotation and pin_annotation not in peripheral.annotations:
                peripheral.annotations.append(pin_annotation)

    peripherals = sorted(
        by_instance.values(), key=lambda peripheral: peripheral.instance
    )
    return HpmpcData(
        soc_name=_metadata_text(info.get("socName"), "Unknown"),
        package_name=_metadata_text(info.get("packageName"), "Unknown"),
        sdk_name=_metadata_text(info.get("sdkName"), "Unknown"),
        project_name=_metadata_text(info.get("projectName"), ""),
        pinmux_functions=list(functions.keys()),
        peripherals=peripherals,
        clock_functions=list(clock_functions.keys()),
    )
