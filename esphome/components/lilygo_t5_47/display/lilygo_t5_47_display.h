#pragma once
#include <Arduino.h>

#include "esp_adc_cal.h"
#include "esphome/components/display/display_buffer.h"
#include "esphome/components/display/display_color_utils.h"
#include "esphome/core/component.h"
#include "esphome/core/hal.h"
#include "esphome/core/version.h"

namespace esphome {
namespace lilygo_t5_47 {

#if ESPHOME_VERSION_CODE >= VERSION_CODE(2023, 12, 0)
class LilygoT547Display : public display::DisplayBuffer {
#else 
class LilygoT547Display : public PollingComponent, public display::DisplayBuffer {
#endif
 public:
  float get_setup_priority() const override;

  void setup() override;
  void update() override;
  void dump_config() override;

  int get_width_internal() override;
  int get_height_internal() override;

  void fill(Color color) override;
  
  void on_shutdown() override;

  void set_full_update_every(uint32_t value) { this->full_update_every_ = value; }
  uint32_t get_full_update_every() { return this->full_update_every_; }

  void set_full_update_countdown(uint32_t value);
  uint32_t get_full_update_countdown();

  void set_cycles_render(uint32_t value) { this->cycles_render_ = value; }
  uint32_t get_cycles_render() { return this->cycles_render_; }

  void set_cycles_invert(uint32_t value) { this->cycles_invert_ = value; }
  uint32_t get_cycles_invert() { return this->cycles_invert_; }

  void flush_screen_changes();
  void power_on();
  void power_off();

#if ESPHOME_VERSION_CODE >= VERSION_CODE(2022,6,0)
  display::DisplayType get_display_type() override { return display::DisplayType::DISPLAY_TYPE_BINARY; }
#endif

 protected:
  void HOT draw_absolute_pixel_internal(int x, int y, Color color) override;

 private:
  uint32_t full_update_every_;
  uint32_t cycles_render_;
  uint32_t cycles_invert_;
};

}  // namespace lilygo_t5_47
}  // namespace esphome
