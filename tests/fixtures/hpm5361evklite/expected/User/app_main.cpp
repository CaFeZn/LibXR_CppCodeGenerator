#include "app_main.h"
#include "board.h"
#include "hpm_timebase.hpp"
#include "libxr.hpp"
#include "hpm_uart.hpp"
#include "hpm_i2c.hpp"
#include "hpm_spi.hpp"
#include "hpm_mcan.hpp"
#include "app_framework.hpp"

/* User Code Begin 1 */
// Golden user header area.
/* User Code End 1 */

ATTR_PLACE_AT_NONCACHEABLE static uint8_t uart3_rx_dma_buffer[128];
ATTR_PLACE_AT_NONCACHEABLE static uint8_t uart3_tx_dma_buffer[384];
static uint8_t spi1_rx_buffer[128];
static uint8_t spi1_tx_buffer[128];

extern "C" void app_main(void)
{
  /* User Code Begin 2 */
  // Golden user setup area.
  /* User Code End 2 */
  static LibXR::HPMTimebase timebase;
  UNUSED(timebase);
  static LibXR::HPMUART uart3(
      BOARD_APP_UART_BASE, BOARD_APP_UART_CLK_NAME, BOARD_APP_UART_IRQ,
      LibXR::RawData(uart3_rx_dma_buffer, sizeof(uart3_rx_dma_buffer)),
      LibXR::RawData(uart3_tx_dma_buffer, sizeof(uart3_tx_dma_buffer)),
      4, 5, 7,
      {921600U, LibXR::UART::Parity::EVEN, 7U, 2U});
  static LibXR::HPMI2C i2c3(BOARD_APP_I2C_BASE, BOARD_APP_I2C_CLK_NAME, true, {400000U});
  board_init_spi_pins_with_gpio_as_cs(BOARD_APP_SPI_BASE);
  static LibXR::HPMSPI spi1(
      BOARD_APP_SPI_BASE, BOARD_APP_SPI_CLK_NAME,
      LibXR::RawData(spi1_rx_buffer, sizeof(spi1_rx_buffer)),
      LibXR::RawData(spi1_tx_buffer, sizeof(spi1_tx_buffer)), false,
      {LibXR::SPI::ClockPolarity::LOW, LibXR::SPI::ClockPhase::EDGE_1, LibXR::SPI::Prescaler::DIV_1, false},
      +[](bool selected) { board_write_spi_cs(BOARD_SPI_CS_PIN, selected ? BOARD_SPI_CS_ACTIVE_LEVEL : !BOARD_SPI_CS_ACTIVE_LEVEL); });
  board_init_can(HPM_MCAN0);
  board_init_can_clock(HPM_MCAN0);
  static LibXR::HPMCAN mcan0(HPM_MCAN0, clock_can0, 0, IRQn_MCAN0, true, 12);
  LibXR::CAN::Configuration mcan0_config{};
  mcan0_config.bitrate = 500000U;
  mcan0_config.sample_point = 0.75f;
  ASSERT(mcan0.SetConfig(mcan0_config) == LibXR::ErrorCode::OK);
  static LibXR::HardwareContainer peripherals(
      LibXR::Entry<LibXR::UART>{uart3, {"console"}},
      LibXR::Entry<LibXR::I2C>{i2c3, {"i2c3"}},
      LibXR::Entry<LibXR::SPI>{spi1, {"spi1"}},
      LibXR::Entry<LibXR::CAN>{mcan0, {"mcan0"}});
  /* User Code Begin 3 */
  // Golden user loop area.
  while (true) {
  }
  /* User Code End 3 */
}
