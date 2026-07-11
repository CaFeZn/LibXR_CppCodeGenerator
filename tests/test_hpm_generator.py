import json
import tempfile
import unittest
from pathlib import Path

from libxr.GeneratorCode import detect_platform
from libxr.GeneratorCodeHPM import generate_code, load_settings
from libxr.PeripheralAnalyzerHPM import parse_hpmpc_file


class HPMGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        board_dir = root / "boards" / "test_board"
        board_dir.mkdir(parents=True)
        self.hpmpc = board_dir / "pinmux.hpmpc"
        self.board_header = board_dir / "board.h"
        (root / "CMakeLists.txt").write_text(
            "set(APP_NAME hpm_test_canfd)\n", encoding="utf-8"
        )
        self.board_header.write_text(
            """#define BOARD_APP_I2C_BASE HPM_I2C3
#define BOARD_APP_I2C_CLK_NAME clock_i2c3
#define BOARD_APP_I2C_IRQ IRQn_I2C3
#define BOARD_APP_SPI_BASE HPM_SPI1
#define BOARD_APP_SPI_CLK_NAME clock_spi1
#define BOARD_APP_SPI_IRQ IRQn_SPI1
#define BOARD_SPI_CS_GPIO_CTRL HPM_GPIO0
#define BOARD_SPI_CS_PIN IOC_PAD_PA26
#define BOARD_APP_CAN_BASE HPM_MCAN2
#define BOARD_APP_CAN_CLK_NAME clock_can2
#define BOARD_APP_CAN_IRQn IRQn_MCAN2
#define BOARD_APP_GPIO_CTRL HPM_GPIO0
#define BOARD_APP_GPIO_INDEX GPIO_DI_GPIOA
#define BOARD_APP_GPIO_PIN 3
#define BOARD_APP_GPIO_IRQ IRQn_GPIO0_A
""",
            encoding="utf-8",
        )
        document = {
            "clientKey": "must-not-leak",
            "secretKey": "must-not-leak",
            "content": {
                "info": {
                    "projectName": "pinmux.hpmpc",
                    "sdkName": "1.10.0",
                    "socName": "HPM5361",
                    "packageName": "QFN48",
                },
                "pinmux": {
                    "functions": {
                        "init_i2c3_pins": {
                            "selectPins": {
                                "PB13": {"signal": "I2C3.C.SCL"},
                                "PB12": {"signal": "I2C3.C.SDA"},
                            }
                        },
                        "init_spi1_pins": {
                            "selectPins": {
                                "PA27": {"signal": "SPI1.A.SCLK"},
                                "PA28": {"signal": "SPI1.A.MISO"},
                                "PA29": {"signal": "SPI1.A.MOSI"},
                            }
                        },
                    }
                },
                "clock": {"functions": {"init_clocks": {}}},
            },
        }
        self.hpmpc.write_text(json.dumps(document), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parser_combines_hpmpc_and_board_app_macros(self):
        project = parse_hpmpc_file(str(self.hpmpc))

        self.assertEqual(project["Mcu"]["Type"], "HPM5361")
        self.assertEqual(project["Peripherals"]["I2C"]["I2C3"]["Pins"]["SCL"], "PB13")
        self.assertEqual(project["Peripherals"]["MCAN"]["MCAN2"]["Kind"], "FDCAN")
        self.assertEqual(
            project["GPIO"]["spi_cs"]["Pin"],
            "GPIO_GET_PIN_INDEX(BOARD_SPI_CS_PIN)",
        )
        serialized = json.dumps(project)
        self.assertNotIn("clientKey", serialized)
        self.assertNotIn("secretKey", serialized)

    def test_generator_emits_supported_hpm_drivers_and_container(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        code = generate_code(project, load_settings(""), use_hw_cntr=True)

        self.assertIn("LibXR::HPMI2C i2c3", code)
        self.assertIn("LibXR::HPMSPI spi1", code)
        self.assertIn("board_init_spi_pins_with_gpio_as_cs(BOARD_APP_SPI_BASE);", code)
        self.assertIn("sizeof(spi1_tx_buffer)), false);", code)
        self.assertIn("LibXR::HPMCANFD mcan2", code)
        self.assertIn("board_init_can(BOARD_APP_CAN_BASE);", code)
        self.assertIn("mcan2.SetConfig(mcan2_config);", code)
        self.assertIn("LibXR::Entry<LibXR::FDCAN>{mcan2", code)
        self.assertIn("LibXR::HPMGPIO spi_cs", code)
        self.assertIn('#include "hpm_mcan.hpp"', code)
        self.assertNotIn('#include "hpm_can.hpp"', code)

    def test_generator_preserves_user_blocks(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        existing = """/* User Code Begin 2 */
  KeepThisCall();
  /* User Code End 2 */
"""
        code = generate_code(project, load_settings(""), existing=existing)
        self.assertIn("KeepThisCall();", code)

    def test_pinmux_without_board_header_stays_inactive(self):
        project = parse_hpmpc_file(str(self.hpmpc), board_header="missing-board.h")
        self.assertFalse(project["Peripherals"]["I2C"]["I2C3"]["Enabled"])
        code = generate_code(project, load_settings(""))
        self.assertNotIn("LibXR::HPMI2C i2c3", code)

    def test_generic_generator_detects_hpm_yaml(self):
        yaml_path = Path(self.temp_dir.name) / "config.yaml"
        yaml_path.write_text("Mcu:\n  Platform: HPM\n", encoding="utf-8")
        self.assertEqual(detect_platform(str(yaml_path)), "HPM")


if __name__ == "__main__":
    unittest.main()
