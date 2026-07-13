#pragma once

#define BOARD_APP_UART_BASE HPM_UART3
#define BOARD_APP_UART_CLK_NAME clock_uart3
#define BOARD_APP_UART_IRQ IRQn_UART3
#define BOARD_APP_I2C_BASE HPM_I2C3
#define BOARD_APP_I2C_CLK_NAME clock_i2c3
#define BOARD_APP_I2C_IRQ IRQn_I2C3
#define BOARD_APP_SPI_BASE HPM_SPI1
#define BOARD_APP_SPI_CLK_NAME clock_spi1
#define BOARD_APP_SPI_IRQ IRQn_SPI1
#define BOARD_SPI_CS_GPIO_CTRL HPM_GPIO0
#define BOARD_SPI_CS_PIN IOC_PAD_PA26
#define BOARD_SPI_CS_ACTIVE_LEVEL       (0U)

#ifdef __cplusplus
extern "C" {
#endif

void board_write_spi_cs(unsigned int pin, unsigned char state);
void board_init_spi_pins_with_gpio_as_cs(SPI_Type *ptr);

void board_init_can(MCAN_Type *ptr);
uint32_t board_init_can_clock(MCAN_Type *ptr);
void init_can_pins(MCAN_Type *ptr);
#ifdef __cplusplus
}
#endif

/* Golden user board header tail. */
