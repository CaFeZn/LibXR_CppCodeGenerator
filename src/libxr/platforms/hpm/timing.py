"""HPM communication timing checks shared by validation and clock selection."""

import math
import struct
from typing import Any, Dict, Optional

_CAN_TIMING_LIMITS = {
    "can": {
        "tq_min": 8,
        "tq_max": 384,
        "seg1_max": 256,
        "seg2_min": 2,
        "seg2_max": 128,
        "min_diff": 2,
    },
    "fdcan_nominal": {
        "tq_min": 8,
        "tq_max": 288,
        "seg1_max": 256,
        "seg2_min": 1,
        "seg2_max": 32,
        "min_diff": 2,
    },
    "fdcan_data": {
        "tq_min": 8,
        "tq_max": 48,
        "seg1_max": 32,
        "seg2_min": 2,
        "seg2_max": 16,
        "min_diff": 1,
    },
}


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
    """Divide integers with JavaScript Math.trunc semantics."""

    if divisor == 0:
        raise ZeroDivisionError("integer division by zero")
    quotient = abs(dividend) // abs(divisor)
    return -quotient if (dividend < 0) != (divisor < 0) else quotient


def _float32(value: float) -> float:
    """Round a number exactly as JavaScript Math.fround does."""

    return struct.unpack("!f", struct.pack("!f", float(value)))[0]


def can_timing_supported(
    clock_hz: Any,
    bitrate: Any,
    sample_point: Any,
    kind: str,
    max_prescaler: Any = 256,
) -> bool:
    """Return whether HPM CAN timing limits can represent the requested bus timing."""

    limits = _CAN_TIMING_LIMITS.get(kind)
    if limits is None:
        return False
    if not _is_finite_number(clock_hz) or not _is_integer(bitrate):
        return False
    if not _is_finite_number(sample_point) or sample_point <= 0 or sample_point >= 1:
        return False
    if not _is_finite_number(max_prescaler) or max_prescaler < 1:
        return False

    bitrate_value = int(bitrate)
    if bitrate_value <= 0:
        return False
    total = math.floor(clock_hz / bitrate_value)
    if total < limits["tq_min"] or clock_hz % bitrate_value != 0:
        return False

    requested_permille = math.trunc(_float32(float(sample_point)) * 1000)
    prescaler_limit = min(256, math.floor(max_prescaler))
    start_prescaler = 1
    while start_prescaler <= prescaler_limit:
        prescaler = start_prescaler
        while prescaler <= prescaler_limit and (
            total // prescaler > limits["tq_max"] or total % prescaler != 0
        ):
            prescaler += 1
        if prescaler > prescaler_limit:
            return False

        time_quanta = total // prescaler
        if time_quanta < limits["tq_min"]:
            return False
        segment2 = (time_quanta - limits["min_diff"]) // 2
        segment1 = time_quanta - segment2
        while segment2 > limits["seg2_max"]:
            segment2 -= 1
            segment1 += 1
        while segment1 * 1000 // time_quanta < requested_permille:
            segment1 += 1
            segment2 -= 1
        if segment1 * 1000 // time_quanta > requested_permille:
            return False
        if segment2 >= limits["seg2_min"] and segment1 <= limits["seg1_max"]:
            return True
        start_prescaler = prescaler + 1
    return False


def resolve_i2c_timing(clock_hz: Any, bus_hz: Any) -> Optional[Dict[str, int]]:
    """Resolve HPM I2C timing register values for a supported standard bus rate."""

    if bus_hz not in (100_000, 400_000, 1_000_000):
        return None
    if not _is_integer(clock_hz) or clock_hz <= 0:
        return None

    clock_value = int(clock_hz)
    bus_value = int(bus_hz)
    clock_period = _trunc_div(10_000_000_000, clock_value)
    if clock_period <= 0:
        return None
    if bus_value == 100_000:
        timing = {
            "high": 40_000,
            "low": 47_000,
            "ratio": 1,
            "setup": 2_500,
            "hold": 3_000,
            "period": 100_000,
        }
    elif bus_value == 400_000:
        timing = {
            "high": 6_000,
            "low": 13_000,
            "ratio": 2,
            "setup": 1_000,
            "hold": 3_000,
            "period": 25_000,
        }
    else:
        timing = {
            "high": 2_600,
            "low": 5_000,
            "ratio": 2,
            "setup": 500,
            "hold": 0,
            "period": 10_000,
        }

    spike_filter = _trunc_div(500, clock_period)
    setup_data = max(
        _trunc_div(timing["setup"] - 2 * clock_period, clock_period) - 2 - spike_filter,
        0,
    )
    hold_data = max(
        _trunc_div(timing["hold"] - 2 * clock_period, clock_period) - 2 - spike_filter,
        0,
    )
    high_limit = (
        _trunc_div(timing["high"] - 2 * clock_period, clock_period) - 2 - spike_filter
    )
    period_limit = (
        _trunc_div(
            _trunc_div(timing["period"], 1 + timing["ratio"]) - 2 * clock_period,
            clock_period,
        )
        - 2
        - spike_filter
    )
    low_limit = _trunc_div(
        _trunc_div(timing["low"] - 2 * clock_period, clock_period) - 2 - spike_filter,
        timing["ratio"],
    )
    scl_high = max(high_limit, period_limit, low_limit)
    if (
        spike_filter < 0
        or spike_filter > 7
        or setup_data > 31
        or hold_data > 31
        or scl_high < 0
        or scl_high > 511
    ):
        return None

    high_period = 2 * clock_period + (2 + spike_filter + scl_high) * clock_period
    actual_hz = _trunc_div(10_000_000_000, high_period * (1 + timing["ratio"]))
    if abs(actual_hz - bus_value) / bus_value > 0.05:
        return None
    return {
        "actual_hz": actual_hz,
        "t_sp": spike_filter,
        "t_sudat": setup_data,
        "t_hddat": hold_data,
        "t_sclhi": scl_high,
    }


__all__ = ["can_timing_supported", "resolve_i2c_timing"]
