import unittest

from libxr.platforms.hpm.clock import (
    clock_source_symbol,
    clock_sources_for_soc,
    default_clock_settings,
    peripheral_clock_hz,
    resolve_can_clock,
    resolve_spi_clock,
    resolve_spi_prescaler,
    resolve_uart_clock,
    uart_baudrate_error,
)
from libxr.platforms.hpm.timing import can_timing_supported, resolve_i2c_timing


class HPMClockTest(unittest.TestCase):
    def test_hpm5361_clock_sources_use_protocol_field_names(self):
        self.assertEqual(
            clock_sources_for_soc("HPM5361", "hpm5361evklite"),
            [
                {"id": "osc24m", "c_symbol": "clk_src_osc24m", "hz": 24_000_000},
                {
                    "id": "pll0_clk0",
                    "c_symbol": "clk_src_pll0_clk0",
                    "hz": 960_000_000,
                },
                {
                    "id": "pll0_clk1",
                    "c_symbol": "clk_src_pll0_clk1",
                    "hz": 600_000_000,
                },
                {
                    "id": "pll0_clk2",
                    "c_symbol": "clk_src_pll0_clk2",
                    "hz": 400_000_000,
                },
            ],
        )
        self.assertEqual(clock_source_symbol("pll0_clk0"), "clk_src_pll0_clk0")
        self.assertIsNone(clock_source_symbol("unsupported"))

    def test_hpm5361_spi_auto_clock_reaches_20_mhz_exactly(self):
        result = resolve_spi_clock("HPM5361", 20_000_000, "hpm5361evklite")

        self.assertIsNotNone(result)
        self.assertEqual(result["actual_sclk_hz"], 20_000_000)
        self.assertEqual(result["peripheral_clock_hz"], 20_000_000)
        self.assertEqual(result["clock_source"], "pll0_clk0")
        self.assertEqual(result["clock_divider"], 48)
        self.assertEqual(result["prescaler"], "DIV_1")

    def test_unverified_hpm5361_board_exposes_oscillator_only(self):
        result = resolve_spi_clock("HPM5361", 20_000_000, "custom_board")

        self.assertIsNotNone(result)
        self.assertEqual(result["clock_source"], "osc24m")
        self.assertEqual(result["actual_sclk_hz"], 12_000_000)
        self.assertEqual(
            default_clock_settings("HPM5361", "custom_board"),
            {
                "auto_clock": True,
                "clock_source": "osc24m",
                "clock_divider": 1,
                "peripheral_clock_hz": 24_000_000,
            },
        )

    def test_manual_spi_and_peripheral_clock_checks_match_plugin(self):
        self.assertEqual(
            resolve_spi_prescaler(80_000_000, 20_000_000),
            {"prescaler": "DIV_4", "actual_sclk_hz": 20_000_000},
        )
        self.assertIsNone(resolve_spi_prescaler(95_142_857, 20_000_000))
        self.assertEqual(
            peripheral_clock_hz("HPM5361", "pll0_clk0", 48, "hpm5361evklite"),
            20_000_000,
        )
        self.assertEqual(
            peripheral_clock_hz("HPM5361", "pll0_clk0", 0, "hpm5361evklite"),
            0,
        )

    def test_uart_error_and_auto_clock_match_plugin(self):
        self.assertAlmostEqual(
            uart_baudrate_error(24_000_000, 115_200),
            0.001602564102564151,
        )
        self.assertIsNone(uart_baudrate_error(1_000_000, 2_000_000))
        self.assertEqual(
            resolve_uart_clock("HPM5361", 921_600, "hpm5361evklite"),
            {
                "auto_clock": True,
                "clock_source": "osc24m",
                "clock_divider": 1,
                "peripheral_clock_hz": 24_000_000,
            },
        )

    def test_can_and_fdcan_clock_selection_match_plugin(self):
        result = resolve_can_clock(
            "HPM5361",
            1_000_000,
            0.75,
            "fdcan",
            2_500_000,
            0.75,
            "hpm5361evklite",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["peripheral_clock_hz"], 80_000_000)
        self.assertTrue(can_timing_supported(80_000_000, 2_500_000, 0.75, "fdcan_data"))
        self.assertFalse(can_timing_supported(80_000_000, 1_000_000, 0.731, "can"))
        self.assertFalse(can_timing_supported(60_000_000, 125_000, 0.756, "can"))
        self.assertFalse(
            can_timing_supported(200_000_000, 2_000_000, 0.75, "fdcan_data", 2)
        )

    def test_can_sample_point_uses_math_fround_semantics(self):
        self.assertTrue(can_timing_supported(80_000_000, 1_000_000, 0.74999999, "can"))

    def test_i2c_timing_matches_hpm_plugin_integer_math(self):
        self.assertIsNone(resolve_i2c_timing(93_750, 1_000_000))
        self.assertEqual(
            resolve_i2c_timing(24_000_000, 1_000_000),
            {
                "actual_hz": 1_001_602,
                "t_sp": 1,
                "t_sudat": 0,
                "t_hddat": 0,
                "t_sclhi": 3,
            },
        )
        self.assertIsNone(resolve_i2c_timing(24_000_000, 123_456))


if __name__ == "__main__":
    unittest.main()
