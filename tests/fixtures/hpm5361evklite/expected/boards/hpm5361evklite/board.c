#include "board.h"
#include "hpm_clock_drv.h"

static int golden_user_board_value = 7;

uint32_t board_init_spi_clock(SPI_Type *ptr)
{
    if (ptr == HPM_SPI1) {
        clock_add_to_group(clock_spi1, 0);
        clock_set_source_divider(clock_spi1, clk_src_pll0_clk0, 48U); /* HPM Peripheral Config: SPI1 */
        return clock_get_frequency(clock_spi1);
    }
    return 0;
}

uint32_t board_init_i2c_clock(I2C_Type *ptr)
{
    if (ptr == HPM_I2C3) {
        clock_add_to_group(clock_i2c3, 0);
        clock_set_source_divider(clock_i2c3, clk_src_osc24m, 1U); /* HPM Peripheral Config: I2C3 */
        return clock_get_frequency(clock_i2c3);
    }
    return 0;
}

uint32_t board_init_uart_clock(UART_Type *ptr)
{
    if (ptr == HPM_UART3) {
        clock_add_to_group(clock_uart3, 0);
        clock_set_source_divider(clock_uart3, clk_src_osc24m, 1U); /* HPM Peripheral Config: UART3 */
        return clock_get_frequency(clock_uart3);
    }
    return 0;
}

void board_write_spi_cs(unsigned int pin, unsigned char state)
{
    golden_user_board_value = (int) pin + (int) state;
}

void board_init_spi_pins_with_gpio_as_cs(SPI_Type *ptr)
{
    (void) ptr;
}

/* Golden user board code before generated block. */
/* HPM Peripheral Config Begin */
void board_init_can(MCAN_Type *ptr)
{
    init_can_pins(ptr);
}

uint32_t board_init_can_clock(MCAN_Type *ptr)
{
    uint32_t freq = 0;
    if (ptr == HPM_MCAN0) {
        clock_add_to_group(clock_can0, 0);
        clock_set_source_divider(clock_can0, clk_src_pll0_clk0, 12U);
        freq = clock_get_frequency(clock_can0);
    }
    return freq;
}

void init_can_pins(MCAN_Type *ptr)
{
    if (ptr == HPM_MCAN0) {
        init_mcan0_pins();
    }
}
/* HPM Peripheral Config End */

void init_gptmr_pins(void)
{
}
