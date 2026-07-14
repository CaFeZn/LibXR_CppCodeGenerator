#include "pinmux.h"

void init_uart3_pins(void)
{
}

void init_i2c3_pins(void)
{
}

void init_spi1_pins(void)
{
}

void init_mcan0_pins(void)
{
    HPM_IOC->PAD[IOC_PAD_PA00].FUNC_CTL = IOC_PA00_FUNC_CTL_MCAN0_TXD;

    HPM_IOC->PAD[IOC_PAD_PA01].FUNC_CTL = IOC_PA01_FUNC_CTL_MCAN0_RXD;
}

static void golden_user_pinmux_helper(void)
{
}
