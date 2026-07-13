import json
import tempfile
import unittest
from pathlib import Path

from hpm_test_utils import CLIENT_KEY, SECRET_KEY, create_hpm_project, write_hpmpc
from libxr.PeripheralAnalyzerHPM import find_hpmpc_files, parse_hpmpc_file
from libxr.platforms.hpm.project import discover_project


class HPMProjectCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = create_hpm_project(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_parser_preserves_metadata_annotations_and_credentials_boundary(
        self,
    ):
        parsed = parse_hpmpc_file(str(self.project["hpmpc"]))

        self.assertEqual(parsed["Mcu"]["Type"], "HPM5361")
        self.assertEqual(parsed["Mcu"]["Package"], "BGA289")
        self.assertEqual(parsed["Mcu"]["SDK"], "1.11.0")
        uart = parsed["Peripherals"]["UART"]["UART3"]
        self.assertEqual(uart["Pins"], {"TXD": "PB15", "RXD": "PB14"})
        self.assertEqual(uart["PinmuxFunctions"], ["init_uart3_pins"])
        self.assertEqual(uart["Annotations"], ["console"])

        serialized = json.dumps(parsed)
        self.assertNotIn("clientKey", serialized)
        self.assertNotIn("secretKey", serialized)
        self.assertNotIn(CLIENT_KEY, serialized)
        self.assertNotIn(SECRET_KEY, serialized)

    def test_project_search_reports_multiple_sources_but_ignores_generated_trees(self):
        root = self.project["root"]
        second = root / "boards" / "other_board" / "pinmux.hpmpc"
        write_hpmpc(second, "other.hpmpc")
        write_hpmpc(root / "build-debug" / "ignored.hpmpc")
        write_hpmpc(root / "hpm_sdk_localized" / "ignored.hpmpc")

        discovered = {
            Path(file_path).resolve() for file_path in find_hpmpc_files(str(root))
        }

        self.assertEqual(
            discovered,
            {self.project["hpmpc"].resolve(), second.resolve()},
        )

    def test_explicit_missing_hpmpc_does_not_fall_back_to_another_file(self):
        missing = self.project["board_dir"] / "missing.hpmpc"

        with self.assertRaises(FileNotFoundError):
            parse_hpmpc_file(str(missing))

    def test_project_preserves_pins_per_pinmux_function(self):
        hpmpc = self.project["hpmpc"]
        document = json.loads(hpmpc.read_text(encoding="utf-8"))
        functions = document["content"]["pinmux"]["functions"]
        functions["init_all_pins"] = {
            "selectPins": {
                "PB08": {
                    "signal": "MCAN0.C.TXD",
                    "annotation": "alternate TX",
                },
                "PB09": {
                    "signal": "MCAN0.C.RXD",
                    "annotation": "alternate RX",
                },
            }
        }
        hpmpc.write_text(json.dumps(document, indent=2), encoding="utf-8")

        project = discover_project(str(self.project["root"]), str(hpmpc))
        mcan = next(
            peripheral
            for peripheral in project.peripherals
            if peripheral.instance == "MCAN0"
        )

        self.assertEqual(
            mcan.function_pins,
            {
                "init_mcan0_pins": {"TXD": "PA00", "RXD": "PA01"},
                "init_all_pins": {"TXD": "PB08", "RXD": "PB09"},
            },
        )
        self.assertIn("alternate TX", mcan.annotations)
        self.assertIn("alternate RX", mcan.annotations)


if __name__ == "__main__":
    unittest.main()
