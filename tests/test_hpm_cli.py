import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hpm_test_utils import CLIENT_KEY, SECRET_KEY, create_hpm_project, write_hpmpc
from libxr import ConfigHPMProject
from libxr.PackageInfo import LibXRPackageInfo


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


class HPMInspectCliContractTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = create_hpm_project(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def inspect(self, *arguments):
        return invoke_hpm_cli(
            [
                "inspect",
                "-d",
                str(self.project["root"]),
                *arguments,
                "--format",
                "json",
            ]
        )

    def test_inspect_json_is_a_clean_versioned_machine_readable_document(self):
        relative_input = "boards/test_board/pinmux.hpmpc"
        exit_code, stdout, stderr = self.inspect("-i", relative_input)

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["protocol_version"], 1)
        self.assertIsInstance(payload["generator_version"], str)
        self.assertTrue(payload["generator_version"].strip())
        self.assertEqual(payload["project"]["board"], "test_board")
        self.assertEqual(payload["project"]["soc"], "HPM5361")
        self.assertEqual(payload["project"]["package"], "BGA289")
        self.assertEqual(payload["project"]["sdk"], "1.11.0")
        self.assertEqual(payload["project"]["hpmpc"], relative_input)
        self.assertNotIn("\\", payload["project"]["hpmpc"])
        self.assertEqual(
            payload["pinmux_functions"],
            ["init_uart3_pins", "init_mcan0_pins"],
        )

        peripherals = {item["instance"]: item for item in payload["peripherals"]}
        self.assertEqual(peripherals["UART3"]["type"], "UART")
        self.assertEqual(peripherals["UART3"]["pins"], {"TXD": "PB15", "RXD": "PB14"})
        self.assertEqual(peripherals["UART3"]["functions"], ["init_uart3_pins"])
        self.assertEqual(peripherals["UART3"]["annotations"], ["console"])

        self.assertNotIn("clientKey", stdout)
        self.assertNotIn("secretKey", stdout)
        self.assertNotIn(CLIENT_KEY, stdout)
        self.assertNotIn(SECRET_KEY, stdout)
        self.assertFalse((self.project["root"] / ".config.yaml").exists())
        self.assertFalse((self.project["root"] / "User").exists())

    def test_inspect_rejects_an_explicit_missing_input_without_fallback(self):
        exit_code, stdout, stderr = self.inspect(
            "-i", "boards/test_board/missing.hpmpc"
        )

        self.assertNotEqual(exit_code, 0)
        self.assertNotIn("unrecognized arguments", stderr.lower())
        self.assertFalse((self.project["root"] / ".config.yaml").exists())
        self.assertFalse((self.project["root"] / "User").exists())
        if stdout.strip():
            json.loads(stdout)

    def test_inspect_selects_first_boards_hpmpc_deterministically(self):
        write_hpmpc(
            self.project["root"] / "boards" / "z_other_board" / "pinmux.hpmpc",
            "other.hpmpc",
        )

        exit_code, stdout, stderr = self.inspect()

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["project"]["board"], "test_board")
        self.assertEqual(payload["project"]["hpmpc"], "boards/test_board/pinmux.hpmpc")
        self.assertFalse((self.project["root"] / ".config.yaml").exists())
        self.assertFalse((self.project["root"] / "User").exists())


class HPMLegacyCliCompatibilityTest(unittest.TestCase):
    def test_legacy_generate_invocation_remains_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = create_hpm_project(Path(temp_dir))
            config_output = project["root"] / ".config.yaml"
            code_output = project["root"] / "User" / "app_main.cpp"

            exit_code, _, stderr = invoke_hpm_cli(
                [
                    "-d",
                    str(project["root"]),
                    "-i",
                    str(project["hpmpc"]),
                    "--config-output",
                    str(config_output),
                    "-o",
                    str(code_output),
                    "--hw-cntr",
                ]
            )

            self.assertEqual(exit_code, 0, stderr)
            self.assertTrue(config_output.is_file())
            self.assertTrue(code_output.is_file())
            self.assertTrue((code_output.parent / "app_main.h").is_file())
            self.assertTrue((code_output.parent / "libxr_config.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
