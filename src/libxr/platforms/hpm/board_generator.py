"""Render HPM board and pinmux glue without writing project files."""

import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .clock import clock_source_symbol
from .markers import detect_newline, has_marker_block, replace_marker_block
from .project import HpmProject


class BoardGenerationError(RuntimeError):
    """Base class for HPM board rendering failures."""


class BoardFileError(BoardGenerationError):
    """Raised when a required board source file cannot be read."""


class MissingBoardHelperError(BoardGenerationError):
    """Raised when an enabled feature requires missing board helpers."""

    def __init__(self, helpers: Sequence[str]) -> None:
        self.helpers = tuple(helpers)
        super().__init__(
            "missing HPM board helpers: {}".format(", ".join(self.helpers))
        )


class MissingClockFunctionError(BoardGenerationError):
    """Raised when a selected peripheral has no board clock function."""

    def __init__(self, function_name: str, peripheral_base: str) -> None:
        self.function_name = function_name
        self.peripheral_base = peripheral_base
        super().__init__(
            "{} is missing; cannot configure {}".format(function_name, peripheral_base)
        )


class MalformedCFunctionError(BoardGenerationError):
    """Raised when a target C function has unbalanced braces."""

    def __init__(self, function_name: str) -> None:
        self.function_name = function_name
        super().__init__("cannot find the end of C function {}".format(function_name))


class UnmanagedCanHelperError(BoardGenerationError):
    """Raised before replacing existing CAN helpers outside a marker block."""

    def __init__(self, function_names: Sequence[str]) -> None:
        self.function_names = tuple(function_names)
        super().__init__(
            "existing CAN board helpers have no HPM Peripheral Config marker: {}".format(
                ", ".join(self.function_names)
            )
        )


class UnsupportedClockSourceError(BoardGenerationError):
    """Raised when a config references no known HPM SDK clock symbol."""

    def __init__(self, source: Any) -> None:
        self.source = source
        super().__init__("unsupported HPM clock source: {}".format(source))


def _read_source(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="strict", newline="") as source:
            return source.read()
    except (OSError, UnicodeError) as error:
        raise BoardFileError("cannot read HPM board file: {}".format(path)) from error


def _normalize_newlines(value: str, newline: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _append_text(source: str, value: str) -> str:
    newline = detect_newline(source)
    prefix = source.rstrip(" \t\r\n")
    rendered = _normalize_newlines(value, newline).rstrip(" \t\r\n")
    if not prefix:
        return rendered + newline
    return prefix + newline * 2 + rendered + newline


def _instance_index(instance: str) -> int:
    match = re.search(r"(\d+)$", instance)
    return int(match.group(1)) if match else 0


def _instance_selected(
    project: HpmProject, config: Mapping[str, Any], instance: str
) -> bool:
    project_config = config.get("project", {})
    selected_value = (
        project_config.get("pinmux_functions", [])
        if isinstance(project_config, Mapping)
        else []
    )
    selected = set(selected_value if isinstance(selected_value, list) else [])
    if not selected:
        return True
    for peripheral in project.peripherals:
        if peripheral.instance != instance:
            continue
        functions = set(peripheral.functions)
        functions.add("init_{}_pins".format(instance.lower()))
        return bool(selected.intersection(functions))
    return False


def _group(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name, {})
    return value if isinstance(value, Mapping) else {}


def _enabled_entries(
    project: HpmProject, config: Mapping[str, Any], group_name: str
) -> List[Tuple[str, Mapping[str, Any]]]:
    result = []
    for instance, value in _group(config, group_name).items():
        if (
            isinstance(value, Mapping)
            and value.get("enabled") is True
            and _instance_selected(project, config, str(instance))
        ):
            result.append((str(instance), value))
    return result


def _function_span(source: str, name: str) -> Optional[Tuple[int, int]]:
    match = re.search(
        r"^[ \t]*(?:uint32_t|void)\s+{}\s*\([^;]*\)\s*\{{".format(re.escape(name)),
        source,
        re.MULTILINE,
    )
    if match is None:
        return None
    open_brace = source.find("{", match.start(), match.end())
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return match.start(), index + 1
    raise MalformedCFunctionError(name)


def _replace_or_insert_function(source: str, name: str, body: str) -> str:
    newline = detect_newline(source)
    rendered = _normalize_newlines(body, newline)
    span = _function_span(source, name)
    if span is not None:
        return source[: span[0]] + rendered + source[span[1] :]
    hint = "/* for uart_rx_line_status case"
    if hint in source:
        return source.replace(hint, rendered + newline * 2 + hint, 1)
    return _append_text(source, rendered)


def _ensure_prototype(source: str, prototype: str) -> str:
    if prototype in source:
        return source
    newline = detect_newline(source)
    extern_end = re.search(r"#ifdef __cplusplus\r?\n}", source)
    if extern_end is not None:
        return (
            source[: extern_end.start()]
            + prototype
            + newline
            + source[extern_end.start() :]
        )
    prefix = source.rstrip(" \t\r\n")
    if not prefix:
        return prototype + newline
    return prefix + newline + prototype + newline


def _clean_managed_clock_lines(source: str) -> str:
    return re.sub(
        r"^[ \t]*clock_set_source_divider\([^;]+;[ \t]*"
        r"/\* HPM Peripheral Config: [A-Z0-9]+ \*/[ \t]*(?:\r?\n|$)",
        "",
        source,
        flags=re.MULTILINE,
    )


def _clock_line(clock_name: str, instance: str, settings: Mapping[str, Any]) -> str:
    source = settings.get("clock_source")
    if not isinstance(source, str):
        raise UnsupportedClockSourceError(source)
    symbol = clock_source_symbol(source)
    if symbol is None:
        raise UnsupportedClockSourceError(source)
    divider = settings.get("clock_divider")
    return (
        "clock_set_source_divider({}, {}, {}U); " "/* HPM Peripheral Config: {} */"
    ).format(clock_name, symbol, divider, instance)


def _update_clock_case(
    source: str,
    function_name: str,
    peripheral_base: str,
    clock_name: str,
    settings: Mapping[str, Any],
) -> str:
    span = _function_span(source, function_name)
    if span is None:
        raise MissingClockFunctionError(function_name, peripheral_base)
    newline = detect_newline(source)
    function_text = _normalize_newlines(source[span[0] : span[1]], newline)
    instance = (
        peripheral_base[len("HPM_") :]
        if peripheral_base.startswith("HPM_")
        else peripheral_base
    )
    set_clock = _clock_line(clock_name, instance, settings)
    branch_pattern = re.compile(
        r"((?:if|else\s+if)\s*\(\s*ptr\s*==\s*{}\s*\)\s*\{{)"
        r"([\s\S]*?)(\r?\n\s*\}})".format(re.escape(peripheral_base))
    )
    branch = branch_pattern.search(function_text)
    if branch is not None:
        body = branch.group(2)
        existing = re.compile(
            r"^[ \t]*clock_set_source_divider\(\s*{}\s*,[^;]+;".format(
                re.escape(clock_name)
            ),
            re.MULTILINE,
        )
        if existing.search(body):
            body = existing.sub("        " + set_clock, body, count=1)
        else:
            add_clock = re.compile(
                r"(^[ \t]*clock_add_to_group\(\s*{}\s*,[^;]+;)".format(
                    re.escape(clock_name)
                ),
                re.MULTILINE,
            )
            if add_clock.search(body):
                body = add_clock.sub(
                    lambda match: match.group(1) + newline + "        " + set_clock,
                    body,
                    count=1,
                )
            else:
                body = body.rstrip(" \t\r\n") + newline + "        " + set_clock
        replacement = branch.group(1) + body + branch.group(3)
        function_text = (
            function_text[: branch.start()]
            + replacement
            + function_text[branch.end() :]
        )
    else:
        keyword = (
            "else if" if re.search(r"\bif\s*\(\s*ptr\s*==", function_text) else "if"
        )
        new_branch = newline.join(
            (
                "    {} (ptr == {}) {{".format(keyword, peripheral_base),
                "        clock_add_to_group({}, 0);".format(clock_name),
                "        " + set_clock,
                "        return clock_get_frequency({});".format(clock_name),
                "    }",
                "",
            )
        )
        final_return = list(re.finditer(r"\r?\n    return ", function_text))
        insert_at = (
            final_return[-1].start()
            + len(final_return[-1].group(0))
            - len("    return ")
            if final_return
            else function_text.rfind("}")
        )
        function_text = (
            function_text[:insert_at] + new_branch + function_text[insert_at:]
        )
    return source[: span[0]] + function_text + source[span[1] :]


def _build_mcan_pinmux_function(
    instance: str, pins: Mapping[str, Any], newline: str
) -> str:
    lines = ["void init_{}_pins(void)".format(instance.lower()), "{"]
    for role in ("TXD", "RXD", "STBY"):
        pad = pins.get(role)
        if not pad:
            continue
        lines.append(
            "    HPM_IOC->PAD[IOC_PAD_{0}].FUNC_CTL = "
            "IOC_{0}_FUNC_CTL_{1}_{2};".format(pad, instance, role)
        )
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    lines.append("}")
    return newline.join(lines)


def _build_can_board_block(enabled: Sequence[Tuple[str, Mapping[str, Any]]]) -> str:
    clock_cases = []
    init_cases = []
    for offset, (instance, settings) in enumerate(enabled):
        keyword = "if" if offset == 0 else "else if"
        index = _instance_index(instance)
        source = settings.get("clock_source")
        if not isinstance(source, str):
            raise UnsupportedClockSourceError(source)
        symbol = clock_source_symbol(source)
        if symbol is None:
            raise UnsupportedClockSourceError(source)
        clock_cases.append(
            "\n".join(
                (
                    "    {} (ptr == HPM_{}) {{".format(keyword, instance),
                    "        clock_add_to_group(clock_can{}, 0);".format(index),
                    "        clock_set_source_divider(clock_can{}, {}, {}U);".format(
                        index, symbol, settings.get("clock_divider")
                    ),
                    "        freq = clock_get_frequency(clock_can{});".format(index),
                    "    }",
                )
            )
        )
        init_cases.append(
            "\n".join(
                (
                    "    {} (ptr == HPM_{}) {{".format(keyword, instance),
                    "        init_{}_pins();".format(instance.lower()),
                    "    }",
                )
            )
        )
    return "\n".join(
        (
            "void board_init_can(MCAN_Type *ptr)",
            "{",
            "    init_can_pins(ptr);",
            "}",
            "",
            "uint32_t board_init_can_clock(MCAN_Type *ptr)",
            "{",
            "    uint32_t freq = 0;",
            "\n".join(clock_cases),
            "    return freq;",
            "}",
            "",
            "void init_can_pins(MCAN_Type *ptr)",
            "{",
            "\n".join(init_cases),
            "}",
        )
    )


def _update_spi_cs_level(source: str, settings: Mapping[str, Any]) -> str:
    active_level = 0 if settings.get("cs_active_low") is True else 1
    pattern = re.compile(
        r"^#define\s+BOARD_SPI_CS_ACTIVE_LEVEL\s+\(?[01]U?\)?",
        re.MULTILINE,
    )
    if pattern.search(source) is None:
        raise MissingBoardHelperError(("BOARD_SPI_CS_ACTIVE_LEVEL",))
    return pattern.sub(
        "#define BOARD_SPI_CS_ACTIVE_LEVEL       ({}U)".format(active_level),
        source,
        count=1,
    )


def render_board_files(
    project: HpmProject, config: Mapping[str, Any]
) -> Dict[str, str]:
    """Return complete rendered board files without modifying the workspace."""

    pinmux_h = _read_source(project.pinmux_h)
    pinmux_c = _read_source(project.pinmux_c)
    board_h = _read_source(project.board_h)
    board_c = _read_source(project.board_c)

    gpio_cs = [
        item
        for item in _enabled_entries(project, config, "spi")
        if item[1].get("use_gpio_cs") is True
    ]
    if gpio_cs:
        required = []
        if "BOARD_SPI_CS_ACTIVE_LEVEL" not in board_h:
            required.append("BOARD_SPI_CS_ACTIVE_LEVEL")
        if "BOARD_SPI_CS_PIN" not in board_h:
            required.append("BOARD_SPI_CS_PIN")
        if "board_write_spi_cs" not in board_c:
            required.append("board_write_spi_cs")
        if required:
            raise MissingBoardHelperError(required)
        board_h = _update_spi_cs_level(board_h, gpio_cs[0][1])

    board_c = _clean_managed_clock_lines(board_c)
    clock_groups = (
        ("spi", "board_init_spi_clock", "clock_spi"),
        ("i2c", "board_init_i2c_clock", "clock_i2c"),
        ("uart", "board_init_uart_clock", "clock_uart"),
    )
    for group_name, function_name, clock_prefix in clock_groups:
        for instance, settings in _enabled_entries(project, config, group_name):
            board_c = _update_clock_case(
                board_c,
                function_name,
                "HPM_{}".format(instance),
                "{}{}".format(clock_prefix, _instance_index(instance)),
                settings,
            )

    enabled_mcan = _enabled_entries(project, config, "mcan")
    for instance, settings in enabled_mcan:
        name = instance.lower()
        pinmux_h = _ensure_prototype(pinmux_h, "void init_{}_pins(void);".format(name))
        pins = settings.get("pins", {})
        pins = pins if isinstance(pins, Mapping) else {}
        pinmux_c = _replace_or_insert_function(
            pinmux_c,
            "init_{}_pins".format(name),
            _build_mcan_pinmux_function(instance, pins, detect_newline(pinmux_c)),
        )

    marker_exists = has_marker_block(board_c)
    if enabled_mcan:
        prototypes = (
            "void board_init_can(MCAN_Type *ptr);",
            "uint32_t board_init_can_clock(MCAN_Type *ptr);",
            "void init_can_pins(MCAN_Type *ptr);",
        )
        for prototype in prototypes:
            board_h = _ensure_prototype(board_h, prototype)
        if not marker_exists:
            conflicts = [
                name
                for name in ("board_init_can", "board_init_can_clock", "init_can_pins")
                if _function_span(board_c, name) is not None
            ]
            if conflicts:
                raise UnmanagedCanHelperError(conflicts)
        board_c = replace_marker_block(
            board_c,
            _build_can_board_block(enabled_mcan),
            "void init_gptmr_pins",
        )
    elif marker_exists:
        board_c = replace_marker_block(board_c, "", "void init_gptmr_pins")

    return {
        os.path.abspath(project.pinmux_h): pinmux_h,
        os.path.abspath(project.pinmux_c): pinmux_c,
        os.path.abspath(project.board_h): board_h,
        os.path.abspath(project.board_c): board_c,
    }


__all__ = [
    "BoardFileError",
    "BoardGenerationError",
    "MalformedCFunctionError",
    "MissingBoardHelperError",
    "MissingClockFunctionError",
    "UnmanagedCanHelperError",
    "UnsupportedClockSourceError",
    "render_board_files",
]
