"""Config flow for the ESPHome device web UI integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class EsphomeWebUiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance setup, nothing to configure."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="ESPHome device web UIs", data={})
        return self.async_show_form(step_id="user")
