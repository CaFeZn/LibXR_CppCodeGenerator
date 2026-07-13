import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpm_test_utils import valid_hpm_peripheral_config
from libxr.platforms.hpm.libxr_config import (
    LibxrConfigError,
    merge_libxr_config,
    read_libxr_config,
    serialize_libxr_config,
    write_libxr_config,
)


class HPMLibxrConfigMergeTest(unittest.TestCase):
    def setUp(self):
        self.peripheral_config = valid_hpm_peripheral_config()
        self.existing = {
            "SYSTEM": "FreeRTOS",
            "custom_top_level": {"keep": True},
            "pinmux_functions": ["stale_function"],
            "disabled_peripherals": ["spi1", "uart3", "adc0"],
            "SPI": {
                "spi1": {
                    "custom_option": 7,
                    "use_dma": True,
                    "tx_dma_channel": 4,
                    "rx_dma_channel": 5,
                },
                "spi9": {"untouched": True},
            },
            "I2C": {
                "i2c3": {
                    "custom_option": 8,
                    "use_dma": True,
                    "dma_channel": 6,
                },
                "i2c9": {"untouched": True},
            },
            "UART": {
                "uart3": {
                    "custom_option": 9,
                    "use_dma": True,
                    "rx_dma_channel": 30,
                    "tx_dma_channel": 31,
                },
                "uart9": {"untouched": True},
            },
            "CAN": {
                "mcan0": {"custom_option": 10, "bitrate": 125_000},
                "can9": {"untouched": True},
            },
            "FDCAN": {
                "mcan0": {"stale_mode_value": True},
                "fdcan9": {"untouched": True},
            },
            "PWM": {"pwm0": {"frequency_hz": 1_000}},
            "GPIO": {"led": {"direction": "OUTPUT_PUSH_PULL"}},
            "device_aliases": {"uart3": {"aliases": ["console"]}},
        }

    def test_merge_preserves_unknown_data_and_updates_managed_fields(self):
        existing_before = copy.deepcopy(self.existing)
        peripheral_before = copy.deepcopy(self.peripheral_config)

        merged = merge_libxr_config(self.existing, self.peripheral_config)

        self.assertEqual(self.existing, existing_before)
        self.assertEqual(self.peripheral_config, peripheral_before)
        self.assertEqual(merged["SYSTEM"], "FreeRTOS")
        self.assertEqual(merged["custom_top_level"], {"keep": True})
        self.assertEqual(
            merged["pinmux_functions"],
            self.peripheral_config["project"]["pinmux_functions"],
        )
        self.assertEqual(merged["disabled_peripherals"], ["adc0"])

        self.assertEqual(merged["SPI"]["spi1"]["custom_option"], 7)
        self.assertEqual(merged["SPI"]["spi1"]["prescaler"], "DIV_1")
        self.assertNotIn("use_dma", merged["SPI"]["spi1"])
        self.assertNotIn("tx_dma_channel", merged["SPI"]["spi1"])
        self.assertNotIn("rx_dma_channel", merged["SPI"]["spi1"])
        self.assertEqual(merged["SPI"]["spi9"], {"untouched": True})

        self.assertEqual(merged["I2C"]["i2c3"]["custom_option"], 8)
        self.assertNotIn("use_dma", merged["I2C"]["i2c3"])
        self.assertNotIn("dma_channel", merged["I2C"]["i2c3"])
        self.assertEqual(merged["I2C"]["i2c9"], {"untouched": True})

        self.assertEqual(merged["UART"]["uart3"]["custom_option"], 9)
        self.assertNotIn("use_dma", merged["UART"]["uart3"])
        self.assertEqual(merged["UART"]["uart3"]["rx_dma_channel"], 0)
        self.assertEqual(merged["UART"]["uart3"]["tx_dma_channel"], 1)
        self.assertEqual(merged["UART"]["uart9"], {"untouched": True})

        self.assertEqual(merged["CAN"]["mcan0"]["custom_option"], 10)
        self.assertIs(merged["CAN"]["mcan0"]["stale_mode_value"], True)
        self.assertEqual(merged["CAN"]["mcan0"]["bitrate"], 500_000)
        self.assertNotIn("mcan0", merged["FDCAN"])
        self.assertEqual(merged["CAN"]["can9"], {"untouched": True})
        self.assertEqual(merged["FDCAN"]["fdcan9"], {"untouched": True})
        self.assertEqual(merged["PWM"], self.existing["PWM"])
        self.assertEqual(merged["GPIO"], self.existing["GPIO"])
        self.assertEqual(merged["device_aliases"], self.existing["device_aliases"])

    def test_disabled_instances_keep_only_user_owned_fields(self):
        self.peripheral_config["spi"]["SPI1"]["enabled"] = False
        self.peripheral_config["i2c"]["I2C3"]["enabled"] = False
        self.peripheral_config["uart"]["UART3"]["enabled"] = False
        self.peripheral_config["mcan"]["MCAN0"]["enabled"] = False

        merged = merge_libxr_config(self.existing, self.peripheral_config)

        self.assertEqual(
            merged["disabled_peripherals"],
            ["adc0", "spi1", "i2c3", "uart3", "mcan0"],
        )
        self.assertEqual(merged["SPI"]["spi1"], {"custom_option": 7})
        self.assertEqual(merged["I2C"]["i2c3"], {"custom_option": 8})
        self.assertEqual(merged["UART"]["uart3"], {"custom_option": 9})
        self.assertEqual(
            merged["CAN"]["mcan0"],
            {"custom_option": 10, "stale_mode_value": True},
        )
        self.assertNotIn("mcan0", merged["FDCAN"])
        self.assertEqual(merged["SPI"]["spi9"], {"untouched": True})
        self.assertEqual(merged["UART"]["uart9"], {"untouched": True})

        reenabled_config = copy.deepcopy(self.peripheral_config)
        for group in ("spi", "i2c", "uart", "mcan"):
            for entry in reenabled_config[group].values():
                entry["enabled"] = True
        reenabled = merge_libxr_config(merged, reenabled_config)
        self.assertEqual(reenabled["SPI"]["spi1"]["custom_option"], 7)
        self.assertEqual(reenabled["I2C"]["i2c3"]["custom_option"], 8)
        self.assertEqual(reenabled["UART"]["uart3"]["custom_option"], 9)
        self.assertEqual(reenabled["CAN"]["mcan0"]["custom_option"], 10)
        self.assertEqual(
            merge_libxr_config(reenabled, reenabled_config),
            reenabled,
        )

    def test_instances_outside_selected_pinmux_functions_are_disabled(self):
        active = {"spi1", "uart3", "mcan0"}

        merged = merge_libxr_config(
            self.existing,
            self.peripheral_config,
            active_instances=active,
        )

        self.assertIn("i2c3", merged["disabled_peripherals"])
        self.assertEqual(merged["I2C"]["i2c3"], {"custom_option": 8})
        self.assertEqual(merged["SPI"]["spi1"]["prescaler"], "DIV_1")
        self.assertEqual(merged["UART"]["uart3"]["baudrate"], 115_200)
        self.assertEqual(merged["CAN"]["mcan0"]["bitrate"], 500_000)

    def test_fdcan_mode_moves_managed_instance_and_emits_fd_fields(self):
        mcan = self.peripheral_config["mcan"]["MCAN0"]
        mcan.update(
            {
                "mode": "fdcan",
                "data_bitrate": 2_500_000,
                "data_sample_point": 0.75,
                "brs": False,
                "esi": True,
            }
        )

        merged = merge_libxr_config(self.existing, self.peripheral_config)

        self.assertNotIn("mcan0", merged["CAN"])
        self.assertEqual(merged["FDCAN"]["mcan0"]["custom_option"], 10)
        self.assertIs(merged["FDCAN"]["mcan0"]["stale_mode_value"], True)
        self.assertEqual(merged["FDCAN"]["mcan0"]["data_bitrate"], 2_500_000)
        self.assertEqual(merged["FDCAN"]["mcan0"]["data_sample_point"], 0.75)
        self.assertIs(merged["FDCAN"]["mcan0"]["brs"], False)
        self.assertIs(merged["FDCAN"]["mcan0"]["esi"], True)


class HPMLibxrConfigIoTest(unittest.TestCase):
    def test_read_serialize_and_atomic_write_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "User" / "libxr_config.yaml"
            config = {"SYSTEM": "None", "custom": {"label": "中文"}}
            real_replace = os.replace

            with mock.patch(
                "libxr.platforms.hpm.libxr_config.os.replace",
                wraps=real_replace,
            ) as replace:
                write_libxr_config(str(target), config)

            self.assertEqual(replace.call_count, 1)
            temporary, destination = replace.call_args.args
            self.assertEqual(destination, str(target.resolve()))
            self.assertNotEqual(temporary, destination)
            self.assertFalse(Path(temporary).exists())
            self.assertEqual(read_libxr_config(str(target)), config)
            self.assertEqual(
                read_libxr_config(str(Path(temp_dir) / "missing.yaml")),
                {},
            )
            self.assertEqual(
                read_libxr_config(str(target)),
                read_libxr_config_from_text(serialize_libxr_config(config)),
            )

    def test_invalid_yaml_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "libxr_config.yaml"
            target.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
            with self.assertRaises(LibxrConfigError):
                read_libxr_config(str(target))


def read_libxr_config_from_text(content):
    with tempfile.TemporaryDirectory() as temp_dir:
        target = Path(temp_dir) / "config.yaml"
        target.write_text(content, encoding="utf-8")
        return read_libxr_config(str(target))


if __name__ == "__main__":
    unittest.main()
