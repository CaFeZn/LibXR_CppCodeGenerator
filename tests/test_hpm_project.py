import json
import tempfile
import unittest
from pathlib import Path

from hpm_test_utils import CLIENT_KEY, SECRET_KEY, create_hpm_project, write_hpmpc
from libxr.PeripheralAnalyzerHPM import find_hpmpc_files, parse_hpmpc_file
from libxr.platforms.hpm.config import default_config
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

    def test_project_reconciles_sticky_hpmpc_functions_with_pinmux_c(self):
        hpmpc = self.project["hpmpc"]
        document = json.loads(hpmpc.read_text(encoding="utf-8"))
        document["content"]["pinmux"]["functions"] = {
            "init_all_pins": {
                "selectPins": {
                    "PA10": {"signal": "MCAN0.A.TXD"},
                    "PA11": {"signal": "MCAN0.A.RXD"},
                    "PC00": {"signal": "MCAN2.A.TXD"},
                    "PC01": {"signal": "MCAN2.A.RXD"},
                }
            },
            "init_mcan2_pins": {
                "annotation": "MCAN2 latest",
                "selectPins": {
                    "PB08": {"signal": "MCAN2.A.TXD"},
                    "PB09": {"signal": "MCAN2.A.RXD"},
                    "PA00": {
                        "signal": "MCAN0.A.TXD",
                        "annotation": "stale MCAN0 assignment",
                    },
                    "PA01": {"signal": "MCAN0.A.RXD"},
                },
            },
            "init_py_pins_as_pgpio": {
                "annotation": "PGPIO only",
                "selectPins": {
                    "PA00": {"signal": "MCAN0.A.TXD"},
                    "PA01": {"signal": "MCAN0.A.RXD"},
                },
            },
            "init_spi1_pins": {
                "selectPins": {
                    "PA26": {"signal": "SPI1.A.CS[0]"},
                    "PA27": {"signal": "SPI1.A.SCLK"},
                    "PA28": {"signal": "SPI1.A.MISO"},
                    "PA29": {"signal": "SPI1.A.MOSI"},
                }
            },
            "init_spi1_pins_with_gpio_as_cs": {
                "selectPins": {
                    "PA26": {"signal": "GPIO0.A.26"},
                    "PA27": {"signal": "SPI1.A.SCLK"},
                    "PA28": {"signal": "SPI1.A.MISO"},
                    "PA29": {"signal": "SPI1.A.MOSI"},
                }
            },
            "init_analog_pins": {"selectPins": {"PB13": {"signal": "ADC0.A.IN5"}}},
        }
        hpmpc.write_text(json.dumps(document, indent=2), encoding="utf-8")

        self.project["board_dir"].joinpath("pinmux.c").write_text(
            """void init_mcan2_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PA08].FUNC_CTL =
        IOC_PA08_FUNC_CTL_MCAN2_TXD | IOC_PAD_FUNC_CTL_LOOP_BACK_MASK;
    HPM_IOC->PAD[IOC_PAD_PA09].FUNC_CTL =
        IOC_PA09_FUNC_CTL_MCAN2_RXD | IOC_PAD_FUNC_CTL_LOOP_BACK_MASK;
    HPM_PIOC->PAD[IOC_PAD_PY05].FUNC_CTL = PIOC_PY05_FUNC_CTL_SOC_PY_05;
}
void init_py_pins_as_pgpio(void)
{
    HPM_PIOC->PAD[PIOC_PAD_PY00].FUNC_CTL = PIOC_PY00_FUNC_CTL_PGPIO_Y_0;
    HPM_PIOC->PAD[PIOC_PAD_PY01].FUNC_CTL = PIOC_PY01_FUNC_CTL_PGPIO_Y_1;
}
void init_mcan0_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PA00].FUNC_CTL = IOC_PA00_FUNC_CTL_MCAN0_TXD;
    HPM_IOC->PAD[IOC_PAD_PA01].FUNC_CTL = IOC_PA01_FUNC_CTL_MCAN0_RXD;
}
void init_i2c3_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PB13].FUNC_CTL =
        IOC_PB13_FUNC_CTL_I2C3_SCL | IOC_PAD_FUNC_CTL_LOOP_BACK_MASK;
    HPM_IOC->PAD[IOC_PAD_PB12].FUNC_CTL =
        IOC_PB12_FUNC_CTL_I2C3_SDA | IOC_PAD_FUNC_CTL_LOOP_BACK_MASK;
}
void init_spi1_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PA26].FUNC_CTL = IOC_PA26_FUNC_CTL_SPI1_CS_0;
    HPM_IOC->PAD[IOC_PAD_PA27].FUNC_CTL = IOC_PA27_FUNC_CTL_SPI1_SCLK;
    HPM_IOC->PAD[IOC_PAD_PA28].FUNC_CTL = IOC_PA28_FUNC_CTL_SPI1_MISO;
    HPM_IOC->PAD[IOC_PAD_PA29].FUNC_CTL = IOC_PA29_FUNC_CTL_SPI1_MOSI;
}
void init_spi1_pins_with_gpio_as_cs(void)
{
    HPM_IOC->PAD[IOC_PAD_PA26].FUNC_CTL = IOC_PA26_FUNC_CTL_GPIO_A_26;
    HPM_IOC->PAD[IOC_PAD_PA27].FUNC_CTL = IOC_PA27_FUNC_CTL_SPI1_SCLK;
    HPM_IOC->PAD[IOC_PAD_PA28].FUNC_CTL = IOC_PA28_FUNC_CTL_SPI1_MISO;
    HPM_IOC->PAD[IOC_PAD_PA29].FUNC_CTL = IOC_PA29_FUNC_CTL_SPI1_MOSI;
}
void init_analog_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PB13].FUNC_CTL = IOC_PAD_FUNC_CTL_ANALOG_MASK;
}
""",
            encoding="utf-8",
        )
        self.project["board_dir"].joinpath("board.h").write_text(
            """#pragma once
#define BOARD_SPI_CS_GPIO_CTRL HPM_GPIO0
#define BOARD_SPI_CS_PIN IOC_PAD_PA26
#define BOARD_SPI_CS_ACTIVE_LEVEL 0
""",
            encoding="utf-8",
        )

        project = discover_project(str(self.project["root"]), str(hpmpc))
        peripherals = {item.instance: item for item in project.peripherals}

        self.assertIn("init_mcan0_pins", project.pinmux_functions)
        self.assertEqual(
            peripherals["MCAN0"].function_pins,
            {
                "init_all_pins": {"TXD": "PA10", "RXD": "PA11"},
                "init_mcan0_pins": {"TXD": "PA00", "RXD": "PA01"},
            },
        )
        self.assertNotIn("MCAN2 latest", peripherals["MCAN0"].annotations)
        self.assertNotIn("stale MCAN0 assignment", peripherals["MCAN0"].annotations)
        self.assertNotIn("PGPIO only", peripherals["MCAN0"].annotations)
        self.assertEqual(
            peripherals["MCAN2"].function_pins,
            {
                "init_all_pins": {"TXD": "PC00", "RXD": "PC01"},
                "init_mcan2_pins": {"TXD": "PB08", "RXD": "PB09"},
            },
        )
        self.assertIn("MCAN2 latest", peripherals["MCAN2"].annotations)
        self.assertEqual(
            peripherals["I2C3"].function_pins,
            {"init_i2c3_pins": {"SCL": "PB13", "SDA": "PB12"}},
        )
        self.assertEqual(
            peripherals["SPI1"].function_pins["init_spi1_pins_with_gpio_as_cs"],
            {"SCLK": "PA27", "MISO": "PA28", "MOSI": "PA29", "CS0": "PA26"},
        )
        self.assertEqual(
            peripherals["ADC0"].function_pins,
            {"init_analog_pins": {"IN5": "PB13"}},
        )
        spi_defaults = default_config(project, ["init_spi1_pins_with_gpio_as_cs"])[
            "spi"
        ]["SPI1"]
        self.assertTrue(spi_defaults["use_gpio_cs"])
        self.assertEqual(spi_defaults["pins"]["CS0"], "PA26")

    def test_project_keeps_hpmpc_mappings_without_confident_c_evidence(self):
        hpmpc = self.project["hpmpc"]
        document = json.loads(hpmpc.read_text(encoding="utf-8"))
        document["content"]["pinmux"]["functions"] = {
            "init_unknown_pins": {
                "annotation": "keep unknown",
                "selectPins": {
                    "PA00": {"signal": "MCAN0.A.TXD"},
                    "PA01": {"signal": "MCAN0.A.RXD"},
                },
            },
            "init_analog_pins": {"selectPins": {"PB13": {"signal": "ADC0.A.IN5"}}},
            "init_malformed_pins": {
                "selectPins": {
                    "PB15": {"signal": "UART3.A.TXD"},
                    "PB14": {"signal": "UART3.A.RXD"},
                }
            },
        }
        hpmpc.write_text(json.dumps(document, indent=2), encoding="utf-8")
        self.project["board_dir"].joinpath("pinmux.c").write_text(
            """void init_unknown_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PA00].FUNC_CTL = IOC_PA00_FUNC_CTL_FUTURE0_TXD;
}
void init_analog_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PB13].FUNC_CTL = IOC_PAD_FUNC_CTL_ANALOG_MASK;
}
void init_malformed_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PB15].FUNC_CTL = IOC_PB15_FUNC_CTL_UART3_TXD;
""",
            encoding="utf-8",
        )

        project = discover_project(str(self.project["root"]), str(hpmpc))
        peripherals = {item.instance: item for item in project.peripherals}

        self.assertEqual(
            peripherals["MCAN0"].function_pins,
            {"init_unknown_pins": {"TXD": "PA00", "RXD": "PA01"}},
        )
        self.assertEqual(
            peripherals["ADC0"].function_pins,
            {"init_analog_pins": {"IN5": "PB13"}},
        )
        self.assertEqual(
            peripherals["UART3"].function_pins,
            {"init_malformed_pins": {"TXD": "PB15", "RXD": "PB14"}},
        )
        self.assertIn("keep unknown", peripherals["MCAN0"].annotations)


if __name__ == "__main__":
    unittest.main()
