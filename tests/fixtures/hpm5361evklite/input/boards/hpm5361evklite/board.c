#include "board.h"
#include "hpm_clock_drv.h"

static int golden_user_board_value = 7;

uint32_t board_init_spi_clock(SPI_Type *ptr)
{
    if (ptr == HPM_SPI1) {
        clock_add_to_group(clock_spi1, 0);
        return clock_get_frequency(clock_spi1);
    }
    return 0;
}

uint32_t board_init_i2c_clock(I2C_Type *ptr)
{
    if (ptr == HPM_I2C3) {
        clock_add_to_group(clock_i2c3, 0);
        return clock_get_frequency(clock_i2c3);
    }
    return 0;
}

uint32_t board_init_uart_clock(UART_Type *ptr)
{
    if (ptr == HPM_UART3) {
        clock_add_to_group(clock_uart3, 0);
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
void init_gptmr_pins(void)
{
}
