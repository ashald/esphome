"""Show ESPHome device web UIs inside Home Assistant (proof of concept).

Devices running ESPHome's tcp_proxy component advertise HTTP targets in their
DeviceCapabilitiesResponse. This integration lists them in a panel and serves
each through an authenticated reverse proxy (proxy.py) whose upstream
connections are TCP streams relayed over the device's native API connection.

In Home Assistant core this would live in the esphome integration itself, and
the frontend would render the UIs as tabs on the device page. As a custom
integration it links to its panel from the device page instead, by pointing the
device's configuration_url ("Visit") at it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import secrets
import time
from typing import Any

from aioesphomeapi.model import TcpProxyTargetType
import aiohttp
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import (
    DOMAIN,
    MAX_STREAMS_PER_DEVICE,
    PANEL_URL_PATH,
    PROXY_URL_PREFIX,
    SESSION_TTL,
    STATIC_URL_PATH,
)
from .proxy import TunnelConnector, async_register_proxy

_LOGGER = logging.getLogger(__name__)

ESPHOME_DOMAIN = "esphome"
DATA_HTTP_REGISTERED = f"{DOMAIN}_http_registered"


@dataclass(frozen=True, slots=True)
class DeviceUi:
    """An HTTP target of a device's tcp_proxy."""

    entry_id: str
    device_id: str
    device_name: str
    target: int
    name: str
    path: str


@dataclass(slots=True)
class Session:
    """Grants the browser of one user access to one device UI."""

    token: str
    ui: DeviceUi
    user_id: str
    isolated: bool
    expires: float


class WebUiManager:
    """Tracks device UIs, sessions and the HTTP clients that reach them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.uis: dict[str, list[DeviceUi]] = {}  # by device registry id
        self.sessions: dict[str, Session] = {}
        self._probed: dict[str, object] = {}  # entry_id -> API connection probed
        self._http: dict[tuple[str, int], tuple[object, aiohttp.ClientSession]] = {}
        self._refreshing: dict[str, asyncio.Task[None]] = {}

    # ---- discovery -----------------------------------------------------------

    @callback
    def schedule_refresh(self, entry_id: str) -> None:
        if entry_id in self._refreshing:
            return
        task = self.hass.async_create_background_task(
            self.async_refresh(entry_id), f"{DOMAIN} refresh {entry_id}"
        )
        self._refreshing[entry_id] = task
        task.add_done_callback(lambda _: self._refreshing.pop(entry_id, None))

    async def async_refresh_all(self) -> None:
        await asyncio.gather(
            *(
                self.async_refresh(entry.entry_id)
                for entry in self.hass.config_entries.async_entries(ESPHOME_DOMAIN)
            )
        )

    async def async_refresh(self, entry_id: str) -> None:
        """Ask a connected device which UIs it relays, once per API connection."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != ESPHOME_DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            return
        data = entry.runtime_data
        connection = data.client._connection  # noqa: SLF001
        if not data.available or connection is None or data.device_info is None:
            return
        device = dr.async_get(self.hass).async_get_device_by_connection(
            (dr.CONNECTION_NETWORK_MAC, data.device_info.mac_address), entry_id
        )
        if device is None:
            return
        if self._probed.get(entry_id) is not connection:
            try:
                capabilities = await data.client.device_capabilities()
            except Exception as err:  # noqa: BLE001 - older firmware, disconnects
                _LOGGER.debug("No capabilities from %s: %s", data.name, err)
                return
            self._probed[entry_id] = connection
            name = data.friendly_name or data.name
            uis = [
                DeviceUi(
                    entry_id, device.id, name, index, target.name, target.path or "/"
                )
                for index, target in enumerate(capabilities.tcp_proxy_targets)
                if target.type is TcpProxyTargetType.HTTP
            ]
            if uis:
                self.uis[device.id] = uis
            else:
                self.uis.pop(device.id, None)
        self._link_device_page(device)

    @callback
    def _link_device_page(self, device: dr.DeviceEntry) -> None:
        """Point the device page's "Visit" button at our panel.

        The esphome integration resets configuration_url whenever the device
        reconnects, which fires a registry update that brings us back here.
        """
        if device.id not in self.uis:
            return
        url = f"homeassistant://{PANEL_URL_PATH}/{device.id}"
        if device.configuration_url != url:
            dr.async_get(self.hass).async_update_device(
                device.id, configuration_url=url
            )

    @callback
    def async_device_registry_updated(
        self, event: Event[dr.EventDeviceRegistryUpdatedData]
    ) -> None:
        if event.data["action"] == "remove":
            return
        device = dr.async_get(self.hass).async_get(event.data["device_id"])
        if device is None:
            return
        for entry_id in device.config_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == ESPHOME_DOMAIN:
                self.schedule_refresh(entry_id)

    def find_ui(self, device_id: str, target: int) -> DeviceUi | None:
        return next(
            (ui for ui in self.uis.get(device_id, ()) if ui.target == target), None
        )

    # ---- sessions --------------------------------------------------------------

    def create_session(self, ui: DeviceUi, user_id: str, isolated: bool) -> Session:
        self._expire_sessions()
        session = Session(
            secrets.token_urlsafe(32),
            ui,
            user_id,
            isolated,
            time.monotonic() + SESSION_TTL,
        )
        self.sessions[session.token] = session
        return session

    def touch_session(self, token: str) -> Session | None:
        """Return a live session and extend it; used for every proxied request."""
        session = self.sessions.get(token)
        now = time.monotonic()
        if session is None or session.expires < now:
            self.sessions.pop(token, None)
            return None
        session.expires = now + SESSION_TTL
        return session

    def _expire_sessions(self) -> None:
        now = time.monotonic()
        for token in [t for t, s in self.sessions.items() if s.expires < now]:
            del self.sessions[token]

    # ---- upstream HTTP ---------------------------------------------------------

    def http_session(self, ui: DeviceUi) -> aiohttp.ClientSession | None:
        """HTTP client for a device UI, bound to the device's current API connection."""
        entry = self.hass.config_entries.async_get_entry(ui.entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return None
        data = entry.runtime_data
        connection = data.client._connection  # noqa: SLF001
        if not data.available or connection is None:
            return None
        key = (ui.entry_id, ui.target)
        cached = self._http.get(key)
        if cached is not None and cached[0] is connection:
            return cached[1]
        if cached is not None:
            self.hass.async_create_task(cached[1].close())
        client_session = aiohttp.ClientSession(
            connector=TunnelConnector(
                lambda: data.client, ui.target, MAX_STREAMS_PER_DEVICE
            ),
            cookie_jar=aiohttp.DummyCookieJar(),  # Cookies belong to the browser
        )
        self._http[key] = (connection, client_session)
        return client_session

    async def async_close(self) -> None:
        for _, client_session in self._http.values():
            await client_session.close()
        self._http.clear()
        self.sessions.clear()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = hass.data[DOMAIN] = WebUiManager(hass)

    if not hass.data.get(DATA_HTTP_REGISTERED):
        # Routes and static paths cannot be unregistered; they look up the manager
        async_register_proxy(hass)
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    STATIC_URL_PATH, str(Path(__file__).parent / "www"), False
                )
            ]
        )
        hass.data[DATA_HTTP_REGISTERED] = True

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="esphome-web-ui-panel",
        sidebar_title="Device UIs",
        sidebar_icon="mdi:monitor-dashboard",
        module_url=f"{STATIC_URL_PATH}/esphome-web-ui-panel.js?v=1",
        require_admin=True,
    )
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_session)

    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED, manager.async_device_registry_updated
        )
    )
    for esphome_entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        manager.schedule_refresh(esphome_entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    manager: WebUiManager = hass.data.pop(DOMAIN)
    await manager.async_close()
    return True


def _manager(hass: HomeAssistant) -> WebUiManager | None:
    return hass.data.get(DOMAIN)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "esphome_web_ui/list"})
@websocket_api.async_response
async def ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List the device UIs of connected devices."""
    if (manager := _manager(hass)) is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not loaded")
        return
    await manager.async_refresh_all()
    connection.send_result(
        msg["id"],
        [
            {
                "device_id": ui.device_id,
                "device_name": ui.device_name,
                "target": ui.target,
                "name": ui.name,
            }
            for uis in manager.uis.values()
            for ui in uis
        ],
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "esphome_web_ui/session",
        vol.Required("device_id"): str,
        vol.Required("target"): int,
        vol.Optional("isolated", default=True): bool,
        # Extend this session instead of creating a new one, when still valid
        vol.Optional("token"): str,
    }
)
@websocket_api.async_response
async def ws_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Create (or keep alive) a session that the panel's iframe loads through."""
    if (manager := _manager(hass)) is None:
        connection.send_error(msg["id"], "not_loaded", "Integration is not loaded")
        return
    user_id = connection.user.id
    session = None
    if (token := msg.get("token")) is not None:
        session = manager.touch_session(token)
        if session is not None and (
            session.user_id != user_id
            or session.ui.device_id != msg["device_id"]
            or session.ui.target != msg["target"]
            or session.isolated != msg["isolated"]
        ):
            session = None
    if session is None:
        ui = manager.find_ui(msg["device_id"], msg["target"])
        if ui is None and (device := dr.async_get(hass).async_get(msg["device_id"])):
            # Deep link to a device we have not probed yet (e.g. right after a restart)
            for entry_id in device.config_entries:
                await manager.async_refresh(entry_id)
            ui = manager.find_ui(msg["device_id"], msg["target"])
        if ui is None:
            connection.send_error(
                msg["id"], "not_found", "No such device UI, or the device is offline"
            )
            return
        session = manager.create_session(ui, user_id, msg["isolated"])
    connection.send_result(
        msg["id"],
        {
            "token": session.token,
            "url": f"{PROXY_URL_PREFIX}/{session.token}{session.ui.path}",
            "device_name": session.ui.device_name,
            "name": session.ui.name,
            "expires_in": SESSION_TTL,
        },
    )
