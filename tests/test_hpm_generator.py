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
#define BOARD_APP_UART_BASE HPM_UART3
#define BOARD_APP_UART_CLK_NAME clock_uart3
#define BOARD_APP_UART_IRQ IRQn_UART3
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
                        "init_i2c2_pins": {
                            "selectPins": {
                                "PB10": {"signal": "I2C2.C.SCL"},
                                "PB11": {"signal": "I2C2.C.SDA"},
                            }
                        },
                        "init_spi1_pins": {
                            "selectPins": {
                                "PA27": {"signal": "SPI1.A.SCLK"},
                                "PA28": {"signal": "SPI1.A.MISO"},
                                "PA29": {"signal": "SPI1.A.MOSI"},
                            }
                        },
                        "init_uart3_pins": {
                            "selectPins": {
                                "PB15": {"signal": "UART3.A.TXD"},
                                "PB14": {"signal": "UART3.A.RXD"},
                            }
                        },
                        "init_mcan0_pins": {
                            "selectPins": {
                                "PA00": {"signal": "MCAN0.A.TXD"},
                                "PA01": {"signal": "MCAN0.A.RXD"},
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
        self.assertEqual(project["Peripherals"]["I2C"]["I2C2"]["Base"], "HPM_I2C2")
        self.assertEqual(project["Peripherals"]["UART"]["UART3"]["IRQ"], "BOARD_APP_UART_IRQ")
        self.assertEqual(project["Peripherals"]["MCAN"]["MCAN0"]["Base"], "HPM_MCAN0")
        self.assertEqual(project["Peripherals"]["MCAN"]["MCAN0"]["IRQ"], "IRQn_MCAN0")
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
        self.assertIn("LibXR::HPMUART uart3", code)
        self.assertIn("LibXR::Entry<LibXR::UART>{uart3", code)
        self.assertIn('#include "hpm_uart.hpp"', code)
        self.assertIn("LibXR::HPMSPI spi1", code)
        self.assertIn("board_init_spi_pins_with_gpio_as_cs(BOARD_APP_SPI_BASE);", code)
        self.assertIn("+[](bool selected) { board_write_spi_cs", code)
        self.assertIn("sizeof(spi1_tx_buffer)), false,", code)
        self.assertIn("LibXR::HPMCANFD mcan0", code)
        self.assertIn("LibXR::HPMCANFD mcan2", code)
        self.assertIn("board_init_can(BOARD_APP_CAN_BASE);", code)
        self.assertIn("ASSERT(mcan2.SetConfig(mcan2_config) == LibXR::ErrorCode::OK);", code)
        self.assertIn("LibXR::Entry<LibXR::FDCAN>{mcan2", code)
        self.assertIn("LibXR::HPMGPIO spi_cs", code)
        self.assertIn('#include "hpm_mcan.hpp"', code)
        self.assertNotIn('#include "hpm_can.hpp"', code)

    def test_generator_filters_by_selected_pinmux_functions(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_mcan0_pins"]
        code = generate_code(project, settings, use_hw_cntr=True)

        self.assertIn("LibXR::HPMCANFD mcan0", code)
        self.assertNotIn("LibXR::HPMI2C i2c3", code)
        self.assertNotIn("LibXR::HPMSPI spi1", code)
        self.assertNotIn("LibXR::HPMCANFD mcan2", code)
        self.assertNotIn("LibXR::HPMGPIO app", code)
        self.assertNotIn('#include "hpm_i2c.hpp"', code)
        self.assertNotIn('#include "hpm_spi.hpp"', code)
        self.assertNotIn('#include "hpm_gpio.hpp"', code)

    def test_generator_honors_disabled_peripherals(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["disabled_peripherals"] = ["uart3", "spi1", "i2c3", "mcan0", "mcan2"]
        code = generate_code(project, settings)

        self.assertNotIn("LibXR::HPMSPI spi1", code)
        self.assertNotIn("LibXR::HPMUART uart3", code)
        self.assertNotIn("LibXR::HPMI2C i2c3", code)
        self.assertNotIn("LibXR::HPMCANFD mcan0", code)
        self.assertNotIn("LibXR::HPMCANFD mcan2", code)

    def test_spi_yaml_can_disable_gpio_chip_select(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_spi1_pins"]
        settings["SPI"]["spi1"] = {"use_gpio_cs": False}
        code = generate_code(project, settings)

        self.assertNotIn("board_init_spi_pins_with_gpio_as_cs", code)
        self.assertNotIn("+[](bool selected)", code)

    def test_uart_settings_emit_dma_buffers_and_frame_format(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_uart3_pins"]
        settings["UART"]["uart3"] = {
            "baudrate": 921600,
            "rx_buffer_size": 128,
            "tx_buffer_size": 192,
            "tx_queue_size": 7,
            "parity": "EVEN",
            "data_bits": 7,
            "stop_bits": 2,
        }
        code = generate_code(project, settings)

        self.assertIn("uart3_rx_dma_buffer[128]", code)
        self.assertIn("uart3_tx_dma_buffer[384]", code)
        self.assertIn("LibXR::UART::Parity::EVEN, 7U, 2U", code)
        self.assertIn("0, 1, 7", code)

    def test_spi_settings_emit_libxr_spi_configuration(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_spi1_pins"]
        settings["SPI"]["spi1"] = {
            "buffer_size": 128,
            "clock_polarity": "HIGH",
            "clock_phase": "EDGE_2",
            "prescaler": "DIV_8",
            "double_buffer": True,
        }
        code = generate_code(project, settings)

        self.assertIn("static uint8_t spi1_rx_buffer[128];", code)
        self.assertIn("LibXR::SPI::ClockPolarity::HIGH", code)
        self.assertIn("LibXR::SPI::ClockPhase::EDGE_2", code)
        self.assertIn("LibXR::SPI::Prescaler::DIV_8", code)
        self.assertIn("LibXR::SPI::Prescaler::DIV_8, true}", code)

    def test_spi_target_clock_selects_prescaler(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_spi1_pins"]
        settings["SPI"]["spi1"] = {
            "peripheral_clock_hz": 80_000_000,
            "sclk_hz": 20_000_000,
        }
        code = generate_code(project, settings)

        self.assertIn("LibXR::SPI::Prescaler::DIV_4", code)

    def test_i2c_address_mode_and_can_modes_are_emitted(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["I2C"]["i2c3"] = {"bus_hz": 400000, "address_mode": "10bit"}
        settings["FDCAN"]["mcan0"] = {
            "bitrate": 500000,
            "data_bitrate": 2000000,
            "loopback": True,
            "one_shot": True,
        }
        code = generate_code(project, settings)

        self.assertIn("HPMI2C::AddressMode::ADDR_10BIT", code)
        self.assertIn("mcan0_config.mode.loopback = true;", code)
        self.assertIn("mcan0_config.mode.one_shot = true;", code)

    def test_mcan_can_mode_only_emits_nominal_bitrate(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        project["Peripherals"]["MCAN"]["MCAN0"]["Kind"] = "CAN"
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_mcan0_pins"]
        settings["CAN"]["mcan0"] = {"bitrate": 250000}
        code = generate_code(project, settings)

        self.assertIn("LibXR::HPMCAN mcan0", code)
        self.assertIn("mcan0_config.bitrate = 250000U;", code)
        self.assertNotIn("mcan0_config.sample_point", code)
        self.assertNotIn("mcan0_config.data_bitrate", code)
        self.assertNotIn("mcan0_config.fd_mode", code)
        self.assertEqual(settings["CAN"]["mcan0"], {"bitrate": 250000})

    def test_mcan_can_mode_accepts_sample_point_and_queue_size(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        settings = load_settings("")
        settings["pinmux_functions"] = ["init_mcan0_pins"]
        settings["CAN"]["mcan0"] = {"bitrate": 250000, "sample_point": 0.8, "queue_size": 12}
        code = generate_code(project, settings)

        self.assertIn("LibXR::HPMCAN mcan0", code)
        self.assertIn("true, 12);", code)
        self.assertIn("mcan0_config.bitrate = 250000U;", code)
        self.assertIn("mcan0_config.sample_point = 0.8f;", code)
        self.assertNotIn("mcan0_config.data_bitrate", code)
        self.assertNotIn("mcan0_config.fd_mode", code)

    def test_generator_preserves_user_blocks(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        existing = (
            "/* User Code Begin 2 */\n"
            "\n"
            "  KeepThisCall();\n"
            "  \n"
            "  /* User Code End 2 */\n"
        )
        code = generate_code(project, load_settings(""), existing=existing)
        self.assertIn(
            "  /* User Code Begin 2 */\n"
            "  KeepThisCall();\n"
            "  /* User Code End 2 */",
            code,
        )

    def test_generator_drops_whitespace_only_user_blocks(self):
        project = parse_hpmpc_file(str(self.hpmpc))
        existing = (
            "  /* User Code Begin 2 */\n"
            "  \n"
            "  \n"
            "  \n"
            "  /* User Code End 2 */\n"
            "  /* User Code Begin 3 */\n"
            "  \t\n"
            "  \n"
            "  /* User Code End 3 */\n"
        )
        code = generate_code(project, load_settings(""), existing=existing)
        self.assertIn("  /* User Code Begin 2 */\n  /* User Code End 2 */", code)
        self.assertIn(
            "  /* User Code Begin 3 */\n"
            "  while (true)\n"
            "  {\n"
            "    LibXR::Thread::Sleep(UINT32_MAX);\n"
            "  }\n"
            "  /* User Code End 3 */",
            code,
        )

    def test_pinmux_without_board_header_stays_inactive(self):
        project = parse_hpmpc_file(str(self.hpmpc), board_header="missing-board.h")
        self.assertFalse(project["Peripherals"]["I2C"]["I2C3"]["Enabled"])
        self.assertFalse(project["Peripherals"]["UART"]["UART3"]["Enabled"])
        self.assertFalse(project["Peripherals"]["SPI"]["SPI1"]["Enabled"])
        self.assertFalse(project["Peripherals"]["MCAN"]["MCAN0"]["Enabled"])
        code = generate_code(project, load_settings(""))
        self.assertNotIn("LibXR::HPMI2C i2c3", code)
        self.assertNotIn("LibXR::HPMUART uart3", code)
        self.assertNotIn("LibXR::HPMSPI spi1", code)
        self.assertNotIn("LibXR::HPMCAN mcan0", code)

    def test_generic_generator_detects_hpm_yaml(self):
        yaml_path = Path(self.temp_dir.name) / "config.yaml"
        yaml_path.write_text("Mcu:\n  Platform: HPM\n", encoding="utf-8")
        self.assertEqual(detect_platform(str(yaml_path)), "HPM")


if __name__ == "__main__":
    unittest.main()
