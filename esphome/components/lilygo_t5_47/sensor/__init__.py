import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome.const import (
    CONF_ID,
    STATE_CLASS_NONE,
    ENTITY_CATEGORY_DIAGNOSTIC,
)
from .. import lilygo_t5_47_ns

LilygoT547Sensor = lilygo_t5_47_ns.class_(
    "LilygoT547Sensor", sensor.Sensor, cg.PollingComponent
)

CONFIG_SCHEMA = (
    sensor.sensor_schema(
        accuracy_decimals=2,
        state_class=STATE_CLASS_NONE,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    )
    .extend(
        {
            cv.GenerateID(): cv.declare_id(LilygoT547Sensor),
        }
    )
    .extend(cv.polling_component_schema("60s"))
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await sensor.register_sensor(var, config)

    cg.add_library("https://github.com/daernsinstantfortress/platformio-epdiy-monochrome.git", None)

    cg.add_build_flag("-DCONFIG_EPD_DISPLAY_TYPE_ED047TC1")
    cg.add_build_flag("-DCONFIG_EPD_BOARD_REVISION_LILYGO_T5_47")
