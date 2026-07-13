import copy
import tempfile
import unittest
from pathlib import Path

from hpm_test_utils import create_hpm_validation_project, valid_hpm_peripheral_config
from libxr.platforms.hpm.config import (
    capabilities_for_project,
    normalize_config,
    read_config,
    selected_peripheral_instances,
    validate_config,
    write_config,
)
from libxr.platforms.hpm.hpmpc import PinmuxPeripheral
from libxr.platforms.hpm.project import discover_project


class HpmConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        fixture = create_hpm_validation_project(Path(self.temporary.name))
        self.root = fixture["root"]
        self.project = discover_project(
            str(self.root), "boards/hpm5361evklite/pinmux.hpmpc"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_normalize_preserves_unmanaged_data_and_replaces_inspected_data(self):
        candidate = valid_hpm_peripheral_config()
        candidate["custom_top"] = {"keep": True}
        candidate["project"]["custom_project"] = "keep"
        candidate["spi"]["SPI1"]["custom_spi"] = 7

        normalized = normalize_config(self.project, candidate)

        self.assertEqual(normalized["custom_top"], {"keep": True})
        self.assertEqual(normalized["project"]["custom_project"], "keep")
        self.assertEqual(normalized["spi"]["SPI1"]["custom_spi"], 7)
        self.assertEqual(normalized["project"]["soc"], "HPM5361")
        self.assertEqual(
            normalized["spi"]["SPI1"]["pins"],
            {"CS0": "PA26", "SCLK": "PA27", "MISO": "PA28", "MOSI": "PA29"},
        )

    def test_selected_pinmux_function_uses_its_exact_peripheral_pins(self):
        mcan = next(
            peripheral
            for peripheral in self.project.peripherals
            if peripheral.instance == "MCAN0"
        )
        mcan.functions = ["init_all_pins", "init_mcan0_pins"]
        mcan.function_pins = {
            "init_all_pins": {"TXD": "PB08", "RXD": "PB09"},
            "init_mcan0_pins": {"TXD": "PA00", "RXD": "PA01"},
        }
        self.project.pinmux_functions.insert(0, "init_all_pins")
        candidate = valid_hpm_peripheral_config()
        candidate["project"]["pinmux_functions"] = ["init_all_pins"]

        result = validate_config(self.project, candidate)

        self.assertTrue(result.valid, result.errors)
        self.assertEqual(
            result.normalized_config["mcan"]["MCAN0"]["pins"],
            {"TXD": "PB08", "RXD": "PB09"},
        )

    def test_conflicting_selected_pinmux_functions_are_rejected(self):
        mcan = next(
            peripheral
            for peripheral in self.project.peripherals
            if peripheral.instance == "MCAN0"
        )
        mcan.functions = ["init_all_pins", "init_mcan0_pins"]
        mcan.function_pins = {
            "init_all_pins": {"TXD": "PB08", "RXD": "PB09"},
            "init_mcan0_pins": {"TXD": "PA00", "RXD": "PA01"},
        }
        self.project.pinmux_functions.insert(0, "init_all_pins")
        candidate = valid_hpm_peripheral_config()
        candidate["project"]["pinmux_functions"] = [
            "init_all_pins",
            "init_mcan0_pins",
        ]

        result = validate_config(self.project, candidate)

        self.assertFalse(result.valid)
        conflicts = [
            diagnostic
            for diagnostic in result.errors
            if diagnostic.code == "HPM_PINMUX_PIN_CONFLICT"
        ]
        self.assertEqual({item.field for item in conflicts}, {"pins.TXD", "pins.RXD"})

    def test_different_selected_signals_cannot_share_one_pad(self):
        mcan = next(
            peripheral
            for peripheral in self.project.peripherals
            if peripheral.instance == "MCAN0"
        )
        mcan.function_pins["init_mcan0_pins"]["TXD"] = "PB15"
        candidate = valid_hpm_peripheral_config()
        candidate["project"]["pinmux_functions"] = [
            "init_uart3_pins",
            "init_mcan0_pins",
        ]

        result = validate_config(self.project, candidate)

        conflicts = [
            diagnostic
            for diagnostic in result.errors
            if diagnostic.code == "HPM_PINMUX_PIN_CONFLICT"
        ]
        self.assertFalse(result.valid)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("PB15", conflicts[0].message)
        self.assertIn("UART3.TXD", conflicts[0].message)
        self.assertIn("MCAN0.TXD", conflicts[0].message)

    def test_selected_peripheral_instances_match_pinmux_functions(self):
        candidate = valid_hpm_peripheral_config()
        candidate["project"]["pinmux_functions"] = ["init_uart3_pins"]
        normalized = normalize_config(self.project, candidate)

        self.assertEqual(
            selected_peripheral_instances(self.project, normalized),
            {"uart3"},
        )

    def test_mcan_defaults_to_classic_can_without_explicit_user_mode(self):
        normalized = normalize_config(self.project, {})

        mcan = normalized["mcan"]["MCAN0"]
        self.assertEqual(mcan["mode"], "can")
        self.assertNotIn("data_bitrate", mcan)
        self.assertNotIn("data_sample_point", mcan)
        self.assertNotIn("brs", mcan)

    def test_invalid_structural_and_field_types_never_report_valid(self):
        cases = {}

        quoted_boolean = valid_hpm_peripheral_config()
        quoted_boolean["spi"]["SPI1"]["enabled"] = "false"
        cases["quoted boolean"] = (
            quoted_boolean,
            "HPM_CONFIG_FIELD_TYPE",
        )

        empty_number = valid_hpm_peripheral_config()
        empty_number["spi"]["SPI1"]["sclk_hz"] = ""
        cases["empty number"] = (empty_number, "HPM_CONFIG_FIELD_TYPE")

        invalid_disabled_enum = valid_hpm_peripheral_config()
        invalid_disabled_enum["uart"]["UART3"]["enabled"] = False
        invalid_disabled_enum["uart"]["UART3"]["parity"] = "INVALID"
        cases["disabled enum"] = (
            invalid_disabled_enum,
            "HPM_CONFIG_FIELD_VALUE",
        )

        invalid_group = valid_hpm_peripheral_config()
        invalid_group["spi"] = []
        cases["group list"] = (invalid_group, "HPM_CONFIG_GROUP_TYPE")

        invalid_entry = valid_hpm_peripheral_config()
        invalid_entry["spi"]["SPI1"] = []
        cases["entry list"] = (invalid_entry, "HPM_CONFIG_ENTRY_TYPE")

        for name, (candidate, expected_code) in cases.items():
            with self.subTest(name=name):
                result = validate_config(self.project, candidate)
                self.assertFalse(result.valid)
                self.assertIn(expected_code, {item.code for item in result.errors})

    def test_enabled_uart_dma_channels_are_allocated_globally(self):
        candidate = valid_hpm_peripheral_config()
        uart3 = candidate["uart"]["UART3"]
        uart3.pop("tx_dma_channel")
        uart3.pop("rx_dma_channel")
        uart4 = copy.deepcopy(uart3)
        uart4["pins"] = {"TXD": "STALE", "RXD": "STALE"}
        candidate["uart"]["UART4"] = uart4
        candidate["project"]["pinmux_functions"].append("init_uart4_pins")
        self.project.pinmux_functions.append("init_uart4_pins")
        self.project.peripherals.append(
            PinmuxPeripheral(
                "UART4",
                "UART",
                4,
                {"TXD": "PC01", "RXD": "PC02"},
                ["init_uart4_pins"],
                [],
            )
        )

        result = validate_config(self.project, candidate)
        channels = []
        for instance in ("UART3", "UART4"):
            entry = result.normalized_config["uart"][instance]
            channels.extend((entry["tx_dma_channel"], entry["rx_dma_channel"]))

        self.assertEqual(channels, [0, 1, 2, 3])
        self.assertEqual(len(channels), len(set(channels)))
        self.assertFalse(
            any(item.code.startswith("HPM_DMA_CHANNEL") for item in result.errors)
        )

    def test_dma_conflicts_ranges_and_unsupported_policies_are_structured(self):
        candidate = valid_hpm_peripheral_config()
        candidate["uart"]["UART3"]["tx_dma_channel"] = 32
        candidate["uart"]["UART3"]["rx_dma_channel"] = 32
        candidate["spi"]["SPI1"]["use_dma"] = True
        candidate["i2c"]["I2C3"].update({"use_dma": True, "dma_channel": 5})

        result = validate_config(self.project, candidate)
        error_codes = {item.code for item in result.errors}
        warning_codes = {item.code for item in result.warnings}

        self.assertIn("HPM_UART_DMA_CHANNEL_RANGE", error_codes)
        self.assertIn("HPM_SPI_DMA_UNSUPPORTED", warning_codes)
        self.assertIn("HPM_I2C_FIXED_DMA_CHANNEL_UNSUPPORTED", warning_codes)
        self.assertIn("HPM_I2C_DMA_RUNTIME_ALLOCATED", warning_codes)

    def test_validation_reports_values_repaired_during_normalization(self):
        candidate = valid_hpm_peripheral_config()
        candidate["version"] = 2
        candidate["spi"]["SPI1"].update(
            {
                "auto_clock": False,
                "clock_source": "not_a_clock",
                "clock_divider": 0,
                "spi_mode": 7,
                "clock_polarity": "INVALID",
                "clock_phase": "INVALID",
            }
        )

        result = validate_config(self.project, candidate)
        codes = {item.code for item in result.errors}

        self.assertTrue(
            {
                "HPM_CONFIG_VERSION_UNSUPPORTED",
                "HPM_CLOCK_SOURCE_INVALID",
                "HPM_CLOCK_DIVIDER_INVALID",
                "HPM_SPI_MODE_INVALID",
                "HPM_SPI_CPOL_INVALID",
                "HPM_SPI_CPHA_INVALID",
            }.issubset(codes)
        )
        normalized = result.normalized_config["spi"]["SPI1"]
        self.assertEqual(normalized["clock_source"], "osc24m")
        self.assertEqual(normalized["clock_divider"], 1)
        self.assertIn(normalized["spi_mode"], (0, 1, 2, 3))
        self.assertIn(normalized["clock_polarity"], ("LOW", "HIGH"))
        self.assertIn(normalized["clock_phase"], ("EDGE_1", "EDGE_2"))

    def test_capabilities_expose_ui_options(self):
        capabilities = capabilities_for_project(self.project)

        self.assertEqual(capabilities["spi"]["modes"], [0, 1, 2, 3])
        self.assertFalse(capabilities["spi"]["dma_supported"])
        self.assertFalse(capabilities["i2c"]["fixed_dma_channel"])
        self.assertEqual(capabilities["uart"]["dma"]["channel_count"], 32)
        self.assertEqual(capabilities["mcan"]["modes"], ["can", "fdcan"])

    def test_yaml_write_is_atomic_and_round_trips(self):
        path = self.root / "hpm_peripherals.yaml"
        config = normalize_config(self.project, valid_hpm_peripheral_config())

        write_config(str(path), config)

        self.assertEqual(read_config(str(path)), config)
        self.assertEqual(list(path.parent.glob(".hpm_peripherals.yaml-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
