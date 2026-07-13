import json
from pathlib import Path

import yaml

CLIENT_KEY = "TEST_CLIENT_KEY_MUST_NOT_LEAK"
SECRET_KEY = "TEST_SECRET_KEY_MUST_NOT_LEAK"


def write_hpmpc(path: Path, project_name: str = "pinmux.hpmpc") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "clientKey": CLIENT_KEY,
        "secretKey": SECRET_KEY,
        "content": {
            "info": {
                "projectName": project_name,
                "sdkName": "1.11.0",
                "socName": "HPM5361",
                "packageName": "BGA289",
            },
            "pinmux": {
                "functions": {
                    "init_uart3_pins": {
                        "annotation": "console",
                        "selectPins": {
                            "PB15": {"signal": "UART3.A.TXD"},
                            "PB14": {"signal": "UART3.A.RXD"},
                        },
                    },
                    "init_mcan0_pins": {
                        "annotation": "CAN FD telemetry",
                        "selectPins": {
                            "PA00": {"signal": "MCAN0.A.TXD"},
                            "PA01": {"signal": "MCAN0.A.RXD"},
                        },
                    },
                }
            },
            "clock": {"functions": {"init_clocks": {}}},
        },
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def create_hpm_project(root: Path):
    board_dir = root / "boards" / "test_board"
    board_dir.mkdir(parents=True, exist_ok=True)
    hpmpc = board_dir / "pinmux.hpmpc"
    write_hpmpc(hpmpc)

    (root / "CMakeLists.txt").write_text(
        "set(APP_NAME hpm5361_inspect_test)\n", encoding="utf-8"
    )
    (board_dir / "board.c").write_text("void board_init(void) {}\n", encoding="utf-8")
    (board_dir / "board.h").write_text(
        """#pragma once
#define BOARD_APP_UART_BASE HPM_UART3
#define BOARD_APP_UART_CLK_NAME clock_uart3
#define BOARD_APP_UART_IRQ IRQn_UART3
""",
        encoding="utf-8",
    )
    (board_dir / "pinmux.c").write_text(
        """void init_uart3_pins(void) {}
void init_mcan0_pins(void) {}
""",
        encoding="utf-8",
    )
    (board_dir / "pinmux.h").write_text(
        """#pragma once
void init_uart3_pins(void);
void init_mcan0_pins(void);
""",
        encoding="utf-8",
    )
    return {"root": root, "board_dir": board_dir, "hpmpc": hpmpc}


def create_hpm_validation_project(root: Path):
    project = create_hpm_project(root)
    board_dir = root / "boards" / "hpm5361evklite"
    project["board_dir"].rename(board_dir)
    hpmpc = board_dir / "pinmux.hpmpc"

    document = json.loads(hpmpc.read_text(encoding="utf-8"))
    functions = document["content"]["pinmux"]["functions"]
    functions["init_spi1_pins"] = {
        "annotation": "SPI flash",
        "selectPins": {
            "PA26": {"signal": "SPI1.A.CS[0]"},
            "PA27": {"signal": "SPI1.A.SCLK"},
            "PA28": {"signal": "SPI1.A.MISO"},
            "PA29": {"signal": "SPI1.A.MOSI"},
        },
    }
    functions["init_i2c3_pins"] = {
        "annotation": "sensor bus",
        "selectPins": {
            "PB13": {"signal": "I2C3.C.SCL"},
            "PB12": {"signal": "I2C3.C.SDA"},
        },
    }
    hpmpc.write_text(json.dumps(document, indent=2), encoding="utf-8")

    (board_dir / "board.h").write_text(
        """#pragma once
#define BOARD_APP_UART_BASE HPM_UART3
#define BOARD_APP_UART_CLK_NAME clock_uart3
#define BOARD_APP_UART_IRQ IRQn_UART3
#define BOARD_APP_SPI_BASE HPM_SPI1
#define BOARD_APP_SPI_CLK_NAME clock_spi1
#define BOARD_APP_SPI_IRQ IRQn_SPI1
#define BOARD_SPI_CS_GPIO_CTRL HPM_GPIO0
#define BOARD_SPI_CS_PIN IOC_PAD_PA26
#define BOARD_SPI_CS_ACTIVE_LEVEL 0
#define BOARD_APP_I2C_BASE HPM_I2C3
#define BOARD_APP_I2C_CLK_NAME clock_i2c3
#define BOARD_APP_I2C_IRQ IRQn_I2C3
""",
        encoding="utf-8",
    )
    (board_dir / "pinmux.c").write_text(
        """void init_uart3_pins(void) {}
void init_mcan0_pins(void) {}
void init_spi1_pins(void) {}
void init_i2c3_pins(void) {}
""",
        encoding="utf-8",
    )
    (board_dir / "pinmux.h").write_text(
        """#pragma once
void init_uart3_pins(void);
void init_mcan0_pins(void);
void init_spi1_pins(void);
void init_i2c3_pins(void);
""",
        encoding="utf-8",
    )
    project.update({"board_dir": board_dir, "hpmpc": hpmpc})
    return project


def valid_hpm_peripheral_config():
    return {
        "version": 1,
        "project": {
            "board": "hpm5361evklite",
            "soc": "HPM5361",
            "hpmpc": "boards/hpm5361evklite/pinmux.hpmpc",
            "pinmux_functions": [
                "init_uart3_pins",
                "init_mcan0_pins",
                "init_spi1_pins",
                "init_i2c3_pins",
            ],
        },
        "spi": {
            "SPI1": {
                "auto_clock": True,
                "clock_source": "pll0_clk0",
                "clock_divider": 48,
                "peripheral_clock_hz": 20_000_000,
                "prescaler": "DIV_1",
                "actual_sclk_hz": 20_000_000,
                "enabled": True,
                "buffer_size": 256,
                "sclk_hz": 20_000_000,
                "clock_polarity": "LOW",
                "clock_phase": "EDGE_1",
                "cs_active_low": True,
                "double_buffer": False,
                "use_dma": False,
                "use_gpio_cs": True,
                "pins": {"CS0": "STALE", "SCLK": "STALE"},
            }
        },
        "i2c": {
            "I2C3": {
                "auto_clock": True,
                "clock_source": "osc24m",
                "clock_divider": 1,
                "peripheral_clock_hz": 24_000_000,
                "enabled": True,
                "bus_hz": 100_000,
                "address_mode": "7bit",
                "use_dma": False,
                "pins": {"SCL": "STALE", "SDA": "STALE"},
            }
        },
        "uart": {
            "UART3": {
                "auto_clock": True,
                "clock_source": "osc24m",
                "clock_divider": 1,
                "peripheral_clock_hz": 24_000_000,
                "enabled": True,
                "baudrate": 115_200,
                "rx_buffer_size": 256,
                "tx_buffer_size": 256,
                "tx_queue_size": 5,
                "parity": "NO_PARITY",
                "data_bits": 8,
                "stop_bits": 1,
                "use_dma": True,
                "rx_dma_channel": 0,
                "tx_dma_channel": 1,
                "pins": {"TXD": "STALE", "RXD": "STALE"},
            }
        },
        "mcan": {
            "MCAN0": {
                "auto_clock": True,
                "clock_source": "pll0_clk0",
                "clock_divider": 12,
                "peripheral_clock_hz": 80_000_000,
                "enabled": True,
                "mode": "can",
                "bitrate": 500_000,
                "queue_size": 8,
                "sample_point": 0.75,
                "loopback": False,
                "listen_only": False,
                "one_shot": False,
                "pins": {"TXD": "STALE", "RXD": "STALE"},
            }
        },
    }


def write_hpm_peripheral_config(path: Path, config=None) -> None:
    value = valid_hpm_peripheral_config() if config is None else config
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
