import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from hpm_test_utils import (
    CLIENT_KEY,
    SECRET_KEY,
    create_hpm_validation_project,
    valid_hpm_peripheral_config,
    write_hpm_peripheral_config,
)
from libxr import ConfigHPMProject
from libxr.PackageInfo import LibXRPackageInfo

DIAGNOSTIC_FIELDS = {"code", "level", "peripheral", "field", "message"}


def invoke_hpm_cli(arguments, stdin_text=""):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with mock.patch.object(sys, "argv", ["xr_hpm_cfg"] + list(arguments)):
        with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
            with mock.patch.object(
                LibXRPackageInfo, "check_and_print", return_value=None
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    try:
                        ConfigHPMProject.main()
                    except SystemExit as error:
                        exit_code = error.code if isinstance(error.code, int) else 1
    return exit_code, stdout.getvalue(), stderr.getvalue()


def snapshot_files(root: Path):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class HPMValidateCliContractTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = create_hpm_validation_project(Path(self.temp_dir.name))
        self.config_path = self.project["root"] / "hpm_peripherals.yaml"
        write_hpm_peripheral_config(self.config_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def validate(self, *arguments, stdin_config=None):
        command = [
            "validate",
            "-d",
            str(self.project["root"]),
            "-i",
            "boards/hpm5361evklite/pinmux.hpmpc",
            "--peripheral-config",
            "hpm_peripherals.yaml",
            *arguments,
            "--format",
            "json",
        ]
        stdin_text = ""
        if stdin_config is not None:
            command.append("--config-stdin")
            stdin_text = yaml.safe_dump(stdin_config, sort_keys=False)
        return invoke_hpm_cli(command, stdin_text)

    def assert_protocol_metadata(self, payload):
        self.assertEqual(payload["protocol_version"], 1)
        self.assertIsInstance(payload["generator_version"], str)
        self.assertTrue(payload["generator_version"].strip())

    def assert_no_credentials(self, stdout, stderr=""):
        serialized = stdout + stderr
        self.assertNotIn("clientKey", serialized)
        self.assertNotIn("secretKey", serialized)
        self.assertNotIn(CLIENT_KEY, serialized)
        self.assertNotIn(SECRET_KEY, serialized)

    def test_validate_unexpected_failure_keeps_the_json_contract(self):
        with mock.patch(
            "libxr.platforms.hpm.cli.validate",
            side_effect=RuntimeError("sensitive implementation detail"),
        ):
            exit_code, stdout, stderr = self.validate()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["errors"][0]["code"], "HPM_INTERNAL_ERROR")
        self.assertNotIn("sensitive implementation detail", stdout)

    def test_validate_file_returns_normalized_json_without_writing(self):
        before = snapshot_files(self.project["root"])

        exit_code, stdout, stderr = self.validate()

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assert_protocol_metadata(payload)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], [])
        self.assertIsInstance(payload["warnings"], list)
        normalized = payload["normalized_config"]
        self.assertEqual(normalized["version"], 1)
        self.assertEqual(normalized["project"]["board"], "hpm5361evklite")
        self.assertEqual(
            normalized["project"]["hpmpc"],
            "boards/hpm5361evklite/pinmux.hpmpc",
        )
        self.assertEqual(
            normalized["spi"]["SPI1"]["pins"],
            {"CS0": "PA26", "SCLK": "PA27", "MISO": "PA28", "MOSI": "PA29"},
        )
        self.assertEqual(
            normalized["uart"]["UART3"]["pins"],
            {"TXD": "PB15", "RXD": "PB14"},
        )
        self.assert_no_credentials(stdout, stderr)
        self.assertEqual(snapshot_files(self.project["root"]), before)

    def test_config_stdin_validates_unsaved_candidate_without_writing(self):
        candidate = valid_hpm_peripheral_config()
        candidate["uart"]["UART3"]["baudrate"] = 921_600
        before = snapshot_files(self.project["root"])

        exit_code, stdout, stderr = self.validate(stdin_config=candidate)

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(
            payload["normalized_config"]["uart"]["UART3"]["baudrate"], 921_600
        )
        self.assert_no_credentials(stdout, stderr)
        self.assertEqual(snapshot_files(self.project["root"]), before)

    def test_write_persists_the_normalized_stdin_candidate_explicitly(self):
        candidate = valid_hpm_peripheral_config()
        candidate["uart"]["UART3"]["baudrate"] = 460_800
        before = snapshot_files(self.project["root"])

        exit_code, stdout, stderr = self.validate("--write", stdin_config=candidate)

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, payload["normalized_config"])
        self.assertEqual(persisted["uart"]["UART3"]["baudrate"], 460_800)
        self.assert_no_credentials(stdout, stderr)
        after = snapshot_files(self.project["root"])
        changed = {
            name
            for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        }
        self.assertEqual(changed, {"hpm_peripherals.yaml"})

    def test_invalid_candidate_returns_stable_diagnostics_without_writing(self):
        candidate = copy.deepcopy(valid_hpm_peripheral_config())
        spi = candidate["spi"]["SPI1"]
        spi.update(
            {
                "auto_clock": False,
                "clock_source": "osc24m",
                "clock_divider": 1,
                "peripheral_clock_hz": 24_000_000,
                "sclk_hz": 1_000,
            }
        )
        i2c = candidate["i2c"]["I2C3"]
        i2c.update(
            {
                "auto_clock": False,
                "clock_source": "osc24m",
                "clock_divider": 256,
                "peripheral_clock_hz": 93_750,
                "bus_hz": 1_000_000,
            }
        )
        uart = candidate["uart"]["UART3"]
        uart.update(
            {
                "auto_clock": False,
                "clock_source": "osc24m",
                "clock_divider": 1,
                "peripheral_clock_hz": 24_000_000,
                "baudrate": 10_000_000,
                "rx_dma_channel": 4,
                "tx_dma_channel": 4,
            }
        )
        mcan = candidate["mcan"]["MCAN0"]
        mcan.update(
            {
                "auto_clock": False,
                "clock_source": "pll0_clk0",
                "clock_divider": 12,
                "peripheral_clock_hz": 80_000_000,
                "bitrate": 333_333,
            }
        )
        before = snapshot_files(self.project["root"])

        exit_code, stdout, stderr = self.validate(stdin_config=candidate)

        self.assertNotEqual(exit_code, 0)
        self.assertTrue(stdout.strip(), stderr)
        payload = json.loads(stdout)
        self.assert_protocol_metadata(payload)
        self.assertFalse(payload["valid"])
        self.assertIsInstance(payload["normalized_config"], dict)
        diagnostics = payload["errors"]
        self.assertGreaterEqual(len(diagnostics), 5)
        for diagnostic in diagnostics:
            self.assertEqual(set(diagnostic), DIAGNOSTIC_FIELDS)
            self.assertEqual(diagnostic["level"], "error")
            self.assertIsInstance(diagnostic["message"], str)
            self.assertTrue(diagnostic["message"].strip())

        by_code = {diagnostic["code"]: diagnostic for diagnostic in diagnostics}
        expected = {
            "HPM_SPI_CLOCK_UNREACHABLE": ("SPI1", "sclk_hz"),
            "HPM_I2C_TIMING_UNREACHABLE": ("I2C3", "bus_hz"),
            "HPM_UART_BAUDRATE_UNREACHABLE": ("UART3", "baudrate"),
            "HPM_CAN_NOMINAL_TIMING_UNREACHABLE": ("MCAN0", "bitrate"),
        }
        for code, (peripheral, field) in expected.items():
            self.assertIn(code, by_code)
            self.assertEqual(by_code[code]["peripheral"], peripheral)
            self.assertEqual(by_code[code]["field"], field)

        self.assertIn("HPM_DMA_CHANNEL_CONFLICT", by_code)
        dma_conflict = by_code["HPM_DMA_CHANNEL_CONFLICT"]
        self.assertEqual(dma_conflict["peripheral"], "UART3")
        self.assertIn(dma_conflict["field"], {"rx_dma_channel", "tx_dma_channel"})
        self.assert_no_credentials(stdout, stderr)
        self.assertEqual(snapshot_files(self.project["root"]), before)


if __name__ == "__main__":
    unittest.main()
