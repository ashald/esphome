import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import display
from esphome.const import (
    CONF_FULL_UPDATE_EVERY,
    CONF_ID,
    CONF_LAMBDA,
    CONF_PAGES,
)
from esphome.const import __version__ as ESPHOME_VERSION

from .. import lilygo_t5_47_ns

CONF_CYCLES_RENDER = "cycles_render"
CONF_CYCLES_INVERT = "cycles_invert"

Display = lilygo_t5_47_ns.class_(
    "LilygoT547Display", cg.PollingComponent, display.DisplayBuffer
)

CONFIG_SCHEMA = cv.All(
    display.FULL_DISPLAY_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(Display),
            cv.Optional(CONF_FULL_UPDATE_EVERY, default=1): cv.uint32_t,
            cv.Optional(CONF_CYCLES_RENDER, default=20): cv.uint32_t,
            cv.Optional(CONF_CYCLES_INVERT, default=20): cv.uint32_t,
        }
    ).extend(cv.polling_component_schema("60s")),
    cv.has_at_most_one_key(CONF_PAGES, CONF_LAMBDA),
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])

    if cv.Version.parse(ESPHOME_VERSION) < cv.Version.parse("2023.12.0"):
        await cg.register_component(var, config)
        
    await display.register_display(var, config)

    cg.add(var.set_full_update_every(config[CONF_FULL_UPDATE_EVERY]))
    cg.add(var.set_cycles_render(config[CONF_CYCLES_RENDER]))
    cg.add(var.set_cycles_invert(config[CONF_CYCLES_INVERT]))

    if CONF_LAMBDA in config:
        lambda_ = await cg.process_lambda(
            config[CONF_LAMBDA], [(display.DisplayRef, "it")], return_type=cg.void
        )
        cg.add(var.set_writer(lambda_))

    cg.add_library("https://github.com/ashald/platformio-epdiy-monochrome.git", None)

    cg.add_build_flag("-DCONFIG_EPD_DISPLAY_TYPE_ED047TC1")
    cg.add_build_flag("-DCONFIG_EPD_BOARD_REVISION_LILYGO_T5_47")
