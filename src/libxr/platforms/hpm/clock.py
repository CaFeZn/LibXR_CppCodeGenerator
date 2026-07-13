"""HPM peripheral clock sources and communication clock solvers."""

import math
from typing import Any, Dict, List, Optional

from .timing import can_timing_supported

_SOURCE_SYMBOLS = {
    "osc24m": "clk_src_osc24m",
    "pll0_clk0": "clk_src_pll0_clk0",
    "pll0_clk1": "clk_src_pll0_clk1",
    "pll0_clk2": "clk_src_pll0_clk2",
    "pll1_clk0": "clk_src_pll1_clk0",
    "pll1_clk1": "clk_src_pll1_clk1",
    "pll1_clk2": "clk_src_pll1_clk2",
    "pll1_clk3": "clk_src_pll1_clk3",
}
_HPM5361_SOURCE_HZ = {
    "osc24m": 24_000_000,
    "pll0_clk0": 960_000_000,
    "pll0_clk1": 600_000_000,
    "pll0_clk2": 400_000_000,
}
_SPI_PRESCALERS = (1, 2, 4, 8, 16, 32, 64, 128, 256)
_MAX_COMMUNICATION_CLOCK_HZ = 200_000_000


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _trunc_div(dividend: int, divisor: int) -> int:
    quotient = abs(dividend) // abs(divisor)
    return -quotient if (dividend < 0) != (divisor < 0) else quotient


def _source_frequencies(soc: str, board: str = "") -> Dict[str, int]:
    if soc.upper() == "HPM5361" and board.lower() == "hpm5361evklite":
        return dict(_HPM5361_SOURCE_HZ)
    return {"osc24m": 24_000_000}


def clock_sources_for_soc(soc: str, board: str = "") -> List[Dict[str, Any]]:
    """Return protocol-ready clock sources supported for one SoC and board."""

    return [
        {"id": source, "c_symbol": _SOURCE_SYMBOLS[source], "hz": frequency}
        for source, frequency in _source_frequencies(soc, board).items()
    ]


def clock_source_symbol(source: str) -> Optional[str]:
    """Return the HPM SDK C symbol for a clock source identifier."""

    return _SOURCE_SYMBOLS.get(source)


def peripheral_clock_hz(soc: str, source: str, divider: Any, board: str = "") -> int:
    """Calculate a peripheral clock using an HPM source and integer divider."""

    source_hz = _source_frequencies(soc, board).get(source)
    if source_hz is None or not _is_integer(divider) or divider < 1 or divider > 256:
        return 0
    return source_hz // int(divider)


def _clock_candidates(soc: str, board: str = "") -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for source in clock_sources_for_soc(soc, board):
        for divider in range(1, 257):
            clock_hz = source["hz"] // divider
            if 0 < clock_hz <= _MAX_COMMUNICATION_CLOCK_HZ:
                candidates.append(
                    {
                        "auto_clock": True,
                        "clock_source": source["id"],
                        "clock_divider": divider,
                        "peripheral_clock_hz": clock_hz,
                    }
                )
    return candidates


def resolve_spi_clock(
    soc: str, target_hz: Any, board: str = ""
) -> Optional[Dict[str, Any]]:
    """Select the fastest SPI clock not exceeding the requested SCLK."""

    if not _is_finite_number(target_hz) or target_hz <= 0:
        return None
    best: Optional[Dict[str, Any]] = None
    for candidate in _clock_candidates(soc, board):
        for prescaler in _SPI_PRESCALERS:
            clock_hz = candidate["peripheral_clock_hz"]
            if clock_hz % prescaler != 0:
                continue
            actual_hz = clock_hz // prescaler
            if actual_hz <= 0 or actual_hz > target_hz:
                continue
            current = dict(candidate)
            current.update(
                {
                    "prescaler": "DIV_{}".format(prescaler),
                    "actual_sclk_hz": actual_hz,
                }
            )
            if (
                best is None
                or current["actual_sclk_hz"] > best["actual_sclk_hz"]
                or (
                    current["actual_sclk_hz"] == best["actual_sclk_hz"]
                    and current["peripheral_clock_hz"] < best["peripheral_clock_hz"]
                )
            ):
                best = current
    return best


def resolve_spi_prescaler(clock_hz: Any, target_hz: Any) -> Optional[Dict[str, Any]]:
    """Select an SPI prescaler for a fixed peripheral clock."""

    if (
        not _is_finite_number(clock_hz)
        or not _is_finite_number(target_hz)
        or clock_hz <= 0
        or target_hz <= 0
    ):
        return None
    for prescaler in _SPI_PRESCALERS:
        if clock_hz % prescaler != 0:
            continue
        actual_hz = math.floor(clock_hz / prescaler)
        if actual_hz <= target_hz:
            return {
                "prescaler": "DIV_{}".format(prescaler),
                "actual_sclk_hz": actual_hz,
            }
    return None


def uart_baudrate_error(clock_hz: Any, baudrate: Any) -> Optional[float]:
    """Return the relative UART baud error, or None when HPM cannot represent it."""

    if not _is_finite_number(clock_hz) or not _is_integer(baudrate):
        return None
    baudrate_value = int(baudrate)
    if baudrate_value < 200 or clock_hz < baudrate_value * 8:
        return None
    scaled = _trunc_div(math.trunc(clock_hz * 1000), baudrate_value)
    for oversample in range(8, 31, 2):
        divider = _trunc_div(
            scaled + oversample * 500,
            oversample * 1000,
        )
        if divider < 1 or divider > 0xFFFF:
            continue
        delta = abs(divider * oversample * 1000 - scaled)
        if delta == 0 or _trunc_div(delta * 100, scaled) <= 3:
            actual = clock_hz / (divider * oversample)
            return abs(actual - baudrate_value) / baudrate_value
    return None


def resolve_uart_clock(
    soc: str, baudrate: Any, board: str = ""
) -> Optional[Dict[str, Any]]:
    """Choose the HPM peripheral clock with the lowest acceptable UART baud error."""

    candidates = _clock_candidates(soc, board)
    for candidate in candidates:
        if (
            candidate["clock_source"] == "osc24m"
            and candidate["clock_divider"] == 1
            and uart_baudrate_error(candidate["peripheral_clock_hz"], baudrate)
            is not None
        ):
            return candidate

    valid = []
    for candidate in candidates:
        error = uart_baudrate_error(candidate["peripheral_clock_hz"], baudrate)
        if error is not None:
            valid.append((error, candidate["peripheral_clock_hz"], candidate))
    valid.sort(key=lambda item: (item[0], item[1]))
    return valid[0][2] if valid else None


def resolve_can_clock(
    soc: str,
    bitrate: Any,
    sample_point: Any,
    mode: str,
    data_bitrate: Any = None,
    data_sample_point: Any = None,
    board: str = "",
    brs: bool = False,
) -> Optional[Dict[str, Any]]:
    """Choose a peripheral clock supporting the requested CAN or CAN FD timing."""

    valid = []
    nominal_kind = "fdcan_nominal" if mode == "fdcan" else "can"
    for candidate in _clock_candidates(soc, board):
        clock_hz = candidate["peripheral_clock_hz"]
        if not can_timing_supported(clock_hz, bitrate, sample_point, nominal_kind):
            continue
        if mode == "fdcan" and not can_timing_supported(
            clock_hz,
            bitrate if data_bitrate is None else data_bitrate,
            sample_point if data_sample_point is None else data_sample_point,
            "fdcan_data",
            2 if brs else 256,
        ):
            continue
        valid.append(candidate)
    valid.sort(
        key=lambda candidate: (
            abs(candidate["peripheral_clock_hz"] - 80_000_000),
            candidate["peripheral_clock_hz"],
        )
    )
    return valid[0] if valid else None


def default_clock_settings(soc: str, board: str = "") -> Dict[str, Any]:
    """Return the plugin-compatible default HPM clock settings."""

    source = clock_sources_for_soc(soc, board)[0]
    return {
        "auto_clock": True,
        "clock_source": source["id"],
        "clock_divider": 1,
        "peripheral_clock_hz": source["hz"],
    }


__all__ = [
    "clock_source_symbol",
    "clock_sources_for_soc",
    "default_clock_settings",
    "peripheral_clock_hz",
    "resolve_can_clock",
    "resolve_spi_clock",
    "resolve_spi_prescaler",
    "resolve_uart_clock",
    "uart_baudrate_error",
]
