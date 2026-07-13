import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from libxr import ConfigHPMProject
from libxr.PackageInfo import LibXRPackageInfo

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "hpm5361evklite"
REQUIRED_GENERATED_FILES = {
    ".config.yaml",
    "User/app_main.cpp",
    "User/libxr_config.yaml",
    "boards/hpm5361evklite/board.c",
    "boards/hpm5361evklite/board.h",
    "boards/hpm5361evklite/pinmux.c",
    "boards/hpm5361evklite/pinmux.h",
}
OPTIONAL_GENERATED_FILES = {"User/app_main.h"}


def invoke_hpm_cli(arguments):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with mock.patch.object(sys, "argv", ["xr_hpm_cfg"] + list(arguments)):
        with mock.patch.object(LibXRPackageInfo, "check_and_print", return_value=None):
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


class HPMGenerateCliContractTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name) / "project"
        shutil.copytree(FIXTURE_ROOT / "input", self.project_root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def generate(self):
        return invoke_hpm_cli(
            [
                "generate",
                "-d",
                str(self.project_root),
                "-i",
                "boards/hpm5361evklite/pinmux.hpmpc",
                "--peripheral-config",
                "hpm_peripherals.yaml",
                "--libxr-config",
                "User/libxr_config.yaml",
                "--config-output",
                ".config.yaml",
                "-o",
                "User/app_main.cpp",
                "--hw-cntr",
                "--format",
                "json",
            ]
        )

    def assert_generated_file_contract(self, payload):
        generated = payload["generated_files"]
        self.assertEqual(len(generated), len(set(generated)))
        self.assertTrue(REQUIRED_GENERATED_FILES.issubset(generated))
        allowed = REQUIRED_GENERATED_FILES | OPTIONAL_GENERATED_FILES
        self.assertTrue(set(generated).issubset(allowed))
        for relative_path in generated:
            self.assertFalse(Path(relative_path).is_absolute())
            self.assertNotIn("\\", relative_path)
            self.assertTrue((self.project_root / relative_path).is_file())

    def assert_matches_golden(self):
        expected_root = FIXTURE_ROOT / "expected"
        expected_files = [path for path in expected_root.rglob("*") if path.is_file()]
        self.assertTrue(expected_files)
        # app/.config lock the legacy Python contract; board/pinmux lock the
        # TypeScript migration baseline.
        for expected in expected_files:
            relative = expected.relative_to(expected_root)
            actual = self.project_root / relative
            self.assertTrue(actual.is_file(), relative.as_posix())
            self.assertEqual(
                actual.read_bytes(), expected.read_bytes(), relative.as_posix()
            )

    def test_generate_returns_json_preserves_user_data_and_matches_golden(self):
        before = snapshot_files(self.project_root)
        exit_code, stdout, stderr = self.generate()

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["protocol_version"], 1)
        self.assertTrue(payload["generator_version"].strip())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["errors"], [])
        self.assertIsInstance(payload["warnings"], list)
        self.assert_generated_file_contract(payload)
        self.assert_matches_golden()
        after = snapshot_files(self.project_root)
        changed = {
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        }
        reported_changes = {
            path
            for path in payload["generated_files"]
            if before.get(path) != after.get(path)
        }
        self.assertEqual(changed, reported_changes)

        app_main = (self.project_root / "User" / "app_main.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("// Golden user header area.", app_main)
        self.assertIn("// Golden user setup area.", app_main)
        self.assertIn("// Golden user loop area.", app_main)
        board = (self.project_root / "boards" / "hpm5361evklite" / "board.c").read_text(
            encoding="utf-8"
        )
        self.assertIn("golden_user_board_value", board)
        self.assertIn("Golden user board code before generated block", board)
        pinmux = (
            self.project_root / "boards" / "hpm5361evklite" / "pinmux.c"
        ).read_text(encoding="utf-8")
        self.assertIn("golden_user_pinmux_helper", pinmux)

        libxr_config = yaml.safe_load(
            (self.project_root / "User" / "libxr_config.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(libxr_config["custom_section"], {"keep": True})
        self.assertEqual(
            libxr_config["GPIO"]["user_led"]["direction"], "OUTPUT_PUSH_PULL"
        )
        self.assertEqual(libxr_config["PWM"]["user_pwm"]["frequency_hz"], 20000)
        self.assertEqual(libxr_config["SPI"]["spi1"]["custom_option"], 7)
        self.assertNotIn("clientKey", stdout)
        self.assertNotIn("secretKey", stdout)

    def test_generate_is_byte_idempotent_for_every_managed_file(self):
        first_code, first_stdout, first_stderr = self.generate()
        self.assertEqual(first_code, 0, first_stderr)
        first_payload = json.loads(first_stdout)
        self.assert_generated_file_contract(first_payload)
        first = snapshot_files(self.project_root)

        second_code, second_stdout, second_stderr = self.generate()
        self.assertEqual(second_code, 0, second_stderr)
        second_payload = json.loads(second_stdout)
        self.assertEqual(
            second_payload["generated_files"], first_payload["generated_files"]
        )
        second = snapshot_files(self.project_root)
        self.assertEqual(second, first)

    def test_config_yaml_pins_match_the_selected_pinmux_function(self):
        hpmpc_path = self.project_root / "boards" / "hpm5361evklite" / "pinmux.hpmpc"
        document = json.loads(hpmpc_path.read_text(encoding="utf-8"))
        document["content"]["pinmux"]["functions"]["init_all_pins"] = {
            "selectPins": {
                "PB08": {"signal": "MCAN0.A.TXD"},
                "PB09": {"signal": "MCAN0.A.RXD"},
            }
        }
        hpmpc_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

        peripheral_path = self.project_root / "hpm_peripherals.yaml"
        peripheral_config = yaml.safe_load(peripheral_path.read_text(encoding="utf-8"))
        peripheral_config["project"]["pinmux_functions"] = ["init_mcan0_pins"]
        peripheral_path.write_text(
            yaml.safe_dump(peripheral_config, sort_keys=False), encoding="utf-8"
        )

        exit_code, stdout, stderr = self.generate()

        self.assertEqual(exit_code, 0, stderr or stdout)
        generated_config = yaml.safe_load(
            (self.project_root / ".config.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            generated_config["Peripherals"]["MCAN"]["MCAN0"]["Pins"],
            {"TXD": "PA00", "RXD": "PA01"},
        )

    def test_invalid_configuration_does_not_write_any_file(self):
        config_path = self.project_root / "hpm_peripherals.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["spi"]["SPI1"]["auto_clock"] = False
        config["spi"]["SPI1"]["clock_source"] = "osc24m"
        config["spi"]["SPI1"]["clock_divider"] = 1
        config["spi"]["SPI1"]["sclk_hz"] = 1000
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        before = snapshot_files(self.project_root)

        exit_code, stdout, stderr = self.generate()

        self.assertNotEqual(exit_code, 0)
        self.assertTrue(stdout.strip(), stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["generated_files"], [])
        self.assertIn(
            "HPM_SPI_CLOCK_UNREACHABLE",
            {diagnostic["code"] for diagnostic in payload["errors"]},
        )
        self.assertEqual(snapshot_files(self.project_root), before)


if __name__ == "__main__":
    unittest.main()
