import copy
import tempfile
import unittest
from pathlib import Path

from hpm_test_utils import create_hpm_validation_project, valid_hpm_peripheral_config
from libxr.platforms.hpm.board_generator import (
    MissingBoardHelperError,
    MissingClockFunctionError,
    UnmanagedCanHelperError,
    render_board_files,
)
from libxr.platforms.hpm.config import normalize_config
from libxr.platforms.hpm.markers import MARKER_BEGIN, MARKER_END, MarkerConflictError
from libxr.platforms.hpm.project import discover_project


def crlf(value):
    return value.strip("\n").replace("\n", "\r\n") + "\r\n"


BOARD_C = """
uint32_t board_init_spi_clock(SPI_Type *ptr)
{
    if (ptr == HPM_SPI1) {
        clock_add_to_group(clock_spi1, 0);
        user_spi_clock_hook();
        return clock_get_frequency(clock_spi1);
    }
    return 0;
}

uint32_t board_init_i2c_clock(I2C_Type *ptr)
{
    return 0;
}

uint32_t board_init_uart_clock(UART_Type *ptr)
{
    return 0;
}

void board_write_spi_cs(uint32_t pin, uint8_t value)
{
    user_gpio_write(pin, value);
}

void user_before_marker(void)
{
    user_owned_before();
}

/* HPM Peripheral Config Begin */
old_generated_can_code();
/* HPM Peripheral Config End */

void init_gptmr_pins(void)
{
}

void user_after_marker(void)
{
    user_owned_after();
}
"""

BOARD_H = """
#pragma once
#define BOARD_SPI_CS_GPIO_CTRL HPM_GPIO0
#define BOARD_SPI_CS_PIN IOC_PAD_PA26
#define BOARD_SPI_CS_ACTIVE_LEVEL 0

#ifdef __cplusplus
}
#endif
"""

PINMUX_C = """
void init_uart3_pins(void) {}
void init_mcan0_pins(void)
{
    old_mcan_pinmux();
}
void init_spi1_pins(void) {}
void init_i2c3_pins(void) {}
"""

PINMUX_H = """
#pragma once
void init_uart3_pins(void);
void init_mcan0_pins(void);
void init_spi1_pins(void);
void init_i2c3_pins(void);
"""


class HpmBoardGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        fixture = create_hpm_validation_project(Path(self.temporary.name))
        self.root = fixture["root"]
        self.project = discover_project(
            str(self.root), "boards/hpm5361evklite/pinmux.hpmpc"
        )
        self._write_sources(BOARD_C, BOARD_H, PINMUX_C, PINMUX_H)
        candidate = valid_hpm_peripheral_config()
        candidate["spi"]["SPI1"]["cs_active_low"] = False
        self.config = normalize_config(self.project, candidate)

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, path, value):
        Path(path).write_bytes(crlf(value).encode("utf-8"))

    def _write_sources(self, board_c, board_h, pinmux_c, pinmux_h):
        self._write(self.project.board_c, board_c)
        self._write(self.project.board_h, board_h)
        self._write(self.project.pinmux_c, pinmux_c)
        self._write(self.project.pinmux_h, pinmux_h)

    def _snapshot(self):
        return {
            path: Path(path).read_bytes()
            for path in (
                self.project.board_c,
                self.project.board_h,
                self.project.pinmux_c,
                self.project.pinmux_h,
            )
        }

    def test_render_is_pure_preserves_user_code_crlf_and_is_idempotent(self):
        before = self._snapshot()

        rendered = render_board_files(self.project, self.config)

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(set(rendered), {str(Path(path).resolve()) for path in before})
        for content in rendered.values():
            self.assertNotIn("\n", content.replace("\r\n", ""))

        board_c = rendered[str(Path(self.project.board_c).resolve())]
        board_h = rendered[str(Path(self.project.board_h).resolve())]
        pinmux_c = rendered[str(Path(self.project.pinmux_c).resolve())]
        self.assertIn("user_owned_before();", board_c)
        self.assertIn("user_owned_after();", board_c)
        self.assertIn("user_spi_clock_hook();", board_c)
        self.assertNotIn("old_generated_can_code();", board_c)
        self.assertEqual(board_c.count(MARKER_BEGIN), 1)
        self.assertEqual(board_c.count(MARKER_END), 1)
        self.assertIn(
            "clock_set_source_divider(clock_spi1, clk_src_pll0_clk0, 48U);",
            board_c,
        )
        self.assertIn(
            "clock_set_source_divider(clock_i2c3, clk_src_osc24m, 1U);",
            board_c,
        )
        self.assertIn(
            "clock_set_source_divider(clock_uart3, clk_src_osc24m, 1U);",
            board_c,
        )
        self.assertIn("void board_init_can(MCAN_Type *ptr)", board_c)
        self.assertIn("init_mcan0_pins();", board_c)
        self.assertIn("#define BOARD_SPI_CS_ACTIVE_LEVEL       (1U)", board_h)
        self.assertIn("uint32_t board_init_can_clock(MCAN_Type *ptr);", board_h)
        self.assertIn(
            "IOC_PA00_FUNC_CTL_MCAN0_TXD",
            pinmux_c,
        )
        self.assertNotIn("old_mcan_pinmux();", pinmux_c)

        for path, content in rendered.items():
            Path(path).write_bytes(content.encode("utf-8"))
        self.assertEqual(render_board_files(self.project, self.config), rendered)

    def test_mixed_newlines_in_clock_functions_converge_in_one_render(self):
        board_c = Path(self.project.board_c)
        source = board_c.read_text(encoding="utf-8")
        source = source.replace("\n", "\r\n")
        source = source.replace(
            "clock_add_to_group(clock_spi1, 0);\r\n",
            "clock_add_to_group(clock_spi1, 0);\n",
        )
        board_c.write_bytes(source.encode("utf-8"))

        first = render_board_files(self.project, self.config)
        board_c.write_bytes(first[str(board_c.resolve())].encode("utf-8"))
        second = render_board_files(self.project, self.config)

        self.assertEqual(second, first)

    def test_missing_gpio_cs_helper_has_a_dedicated_error(self):
        self._write(
            self.project.board_c, BOARD_C.replace("board_write_spi_cs", "other")
        )

        with self.assertRaises(MissingBoardHelperError) as raised:
            render_board_files(self.project, self.config)

        self.assertIn("board_write_spi_cs", raised.exception.helpers)

    def test_missing_clock_function_has_a_dedicated_error(self):
        source = BOARD_C.replace("board_init_i2c_clock", "other_i2c_clock")
        self._write(self.project.board_c, source)

        with self.assertRaises(MissingClockFunctionError) as raised:
            render_board_files(self.project, self.config)

        self.assertEqual(raised.exception.function_name, "board_init_i2c_clock")

    def test_existing_unmanaged_can_helpers_are_not_overwritten(self):
        unmanaged = BOARD_C.replace(
            "/* HPM Peripheral Config Begin */\nold_generated_can_code();\n"
            "/* HPM Peripheral Config End */",
            "void board_init_can(MCAN_Type *ptr)\n{\n    user_can_init(ptr);\n}",
        )
        self._write(self.project.board_c, unmanaged)

        with self.assertRaises(UnmanagedCanHelperError):
            render_board_files(self.project, self.config)

    def test_missing_marker_is_inserted_before_the_board_hint(self):
        without_marker = BOARD_C.replace(
            "/* HPM Peripheral Config Begin */\nold_generated_can_code();\n"
            "/* HPM Peripheral Config End */",
            "",
        )
        self._write(self.project.board_c, without_marker)

        rendered = render_board_files(self.project, self.config)
        board_c = rendered[str(Path(self.project.board_c).resolve())]

        self.assertLess(
            board_c.index(MARKER_BEGIN), board_c.index("void init_gptmr_pins")
        )
        self.assertIn("user_owned_before();", board_c)
        self.assertIn("user_owned_after();", board_c)

    def test_malformed_marker_pair_is_rejected(self):
        malformed = BOARD_C.replace(MARKER_END, "")
        self._write(self.project.board_c, malformed)

        with self.assertRaises(MarkerConflictError):
            render_board_files(self.project, self.config)

    def test_disabling_mcan_clears_only_the_generated_block(self):
        disabled = copy.deepcopy(self.config)
        disabled["mcan"]["MCAN0"]["enabled"] = False

        rendered = render_board_files(self.project, disabled)
        board_c = rendered[str(Path(self.project.board_c).resolve())]

        self.assertIn(MARKER_BEGIN + "\r\n" + MARKER_END, board_c)
        self.assertIn("user_owned_before();", board_c)
        self.assertIn("user_owned_after();", board_c)


if __name__ == "__main__":
    unittest.main()
