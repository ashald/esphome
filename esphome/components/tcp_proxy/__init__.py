"""
TCP Proxy component for ESPHome.

WARNING: This component is a PROOF OF CONCEPT. The API (both Python configuration
and C++ interfaces, and the native API messages) may change at any time.

Relays TCP streams between a native API client (Home Assistant) and endpoints the
device can reach, most usefully the device's own web server on 127.0.0.1. The
client never learns or chooses the endpoint address: it opens a stream to a
target by index, and the targets are fixed in the firmware.
"""

import esphome.codegen as cg
from esphome.components import socket
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADDRESS,
    CONF_BUFFER_SIZE,
    CONF_ID,
    CONF_MAX_CONNECTIONS,
    CONF_NAME,
    CONF_PATH,
    CONF_PORT,
    CONF_TIMEOUT,
    CONF_TYPE,
    CONF_WEB_SERVER,
)
from esphome.core import CORE
import esphome.final_validate as fv
from esphome.types import ConfigType

DEPENDENCIES = ["api"]
AUTO_LOAD = ["socket"]

CONF_TARGETS = "targets"

tcp_proxy_ns = cg.esphome_ns.namespace("tcp_proxy")
TCPProxy = tcp_proxy_ns.class_("TCPProxy", cg.Component)

api_enums_ns = cg.esphome_ns.namespace("api").namespace("enums")
TcpProxyTargetType = api_enums_ns.enum("TcpProxyTargetType")
TARGET_TYPES = {
    "raw": TcpProxyTargetType.TCP_PROXY_TARGET_TYPE_RAW,
    "http": TcpProxyTargetType.TCP_PROXY_TARGET_TYPE_HTTP,
}

DEFAULT_WEB_SERVER_TARGET_NAME = "Web UI"
LOOPBACK = "127.0.0.1"


def _validate_path(value):
    value = cv.string_strict(value)
    if not value.startswith("/"):
        raise cv.Invalid("path must start with '/'")
    return value


def _consume_sockets(config: ConfigType) -> ConfigType:
    socket.consume_sockets(config[CONF_MAX_CONNECTIONS], "tcp_proxy")(config)
    return config


TARGET_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_NAME): cv.All(cv.string_strict, cv.Length(max=120)),
        cv.Optional(CONF_ADDRESS, default=LOOPBACK): cv.ipv4address,
        cv.Required(CONF_PORT): cv.port,
        cv.Optional(CONF_TYPE, default="http"): cv.enum(TARGET_TYPES, lower=True),
        cv.Optional(CONF_PATH, default="/"): cv.All(_validate_path, cv.Length(max=127)),
    }
)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(TCPProxy),
            # Defaults to the device's own web_server when omitted
            cv.Optional(CONF_TARGETS): cv.All(
                cv.ensure_list(TARGET_SCHEMA), cv.Length(min=1, max=8)
            ),
            cv.Optional(CONF_MAX_CONNECTIONS, default=4): cv.int_range(min=1, max=8),
            # Per-stream receive window (and read chunk size), in bytes
            cv.Optional(CONF_BUFFER_SIZE, default=2048): cv.int_range(
                min=256, max=16384
            ),
            cv.Optional(
                CONF_TIMEOUT, default="5s"
            ): cv.positive_time_period_milliseconds,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on(["esp32", "host", "bk72xx", "ln882x", "rtl87xx"]),
    _consume_sockets,
)


def _final_validate(config: ConfigType) -> ConfigType:
    full_config = fv.full_config.get()
    impl = full_config.get("socket", {}).get("implementation")
    if impl == socket.IMPLEMENTATION_LWIP_TCP:
        raise cv.Invalid(
            "tcp_proxy needs outgoing TCP connections, which the 'lwip_tcp' "
            "socket implementation does not support"
        )
    if CONF_TARGETS not in config and CONF_WEB_SERVER not in full_config:
        raise cv.Invalid(
            f"'{CONF_TARGETS}' is required unless '{CONF_WEB_SERVER}' is configured"
        )
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


def _targets(config: ConfigType) -> list[ConfigType]:
    if CONF_TARGETS in config:
        return config[CONF_TARGETS]
    web_server = CORE.config[CONF_WEB_SERVER]
    return [
        {
            CONF_NAME: DEFAULT_WEB_SERVER_TARGET_NAME,
            CONF_ADDRESS: LOOPBACK,
            CONF_PORT: web_server[CONF_PORT],
            CONF_TYPE: TARGET_TYPES["http"],
            CONF_PATH: "/",
        }
    ]


async def to_code(config: ConfigType) -> None:
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    targets = _targets(config)
    for target in targets:
        cg.add(
            var.add_target(
                target[CONF_NAME],
                str(target[CONF_ADDRESS]),
                target[CONF_PORT],
                target[CONF_TYPE],
                target[CONF_PATH],
            )
        )
    cg.add(var.set_max_connections(config[CONF_MAX_CONNECTIONS]))
    cg.add(var.set_buffer_size(config[CONF_BUFFER_SIZE]))
    cg.add(var.set_connect_timeout(config[CONF_TIMEOUT]))

    cg.add_define("USE_TCP_PROXY")
    cg.add_define("TCP_PROXY_TARGET_COUNT", len(targets))
