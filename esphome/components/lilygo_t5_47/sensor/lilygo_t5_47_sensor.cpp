#include "lilygo_t5_47_sensor.h"
#include "esphome/core/log.h"

extern "C" {
#include "eink.h"
}

namespace esphome {
namespace lilygo_t5_47 {

static const char *const TAG = "lilygo_t5_47.sensor";

float LilygoT547Sensor::get_battery_voltage() {
  int raw = analogRead(36);

  adc_cali_handle_t cali_handle = NULL;
  adc_cali_line_fitting_config_t cali_config = {
      .unit_id = ADC_UNIT_1,
      .atten = ADC_ATTEN_DB_12,
      .bitwidth = ADC_BITWIDTH_12,
      .default_vref = 1100,
  };

  int voltage_mv = 0;
  if (adc_cali_create_scheme_line_fitting(&cali_config, &cali_handle) == ESP_OK) {
    adc_cali_raw_to_voltage(cali_handle, raw, &voltage_mv);
    adc_cali_delete_scheme_line_fitting(cali_handle);
    return voltage_mv / 1000.0f * 2.0f;
  }
  return ((float) raw / 4095.0f) * 2.0f * 3.3f;
}

void LilygoT547Sensor::update() {
  eink_power_on();
  delay(20);
  this->publish_state(this->get_battery_voltage());
  eink_power_off();
}

float LilygoT547Sensor::get_setup_priority() const { return esphome::setup_priority::LATE; }

void LilygoT547Sensor::dump_config() {
  LOG_SENSOR("", "Battery Voltage Sensor", this);
  LOG_UPDATE_INTERVAL(this);
}

}  // namespace lilygo_t5_47
}  // namespace esphome
