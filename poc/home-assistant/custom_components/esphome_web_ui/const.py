"""Constants for the ESPHome device web UI integration."""

DOMAIN = "esphome_web_ui"

# Proxied device UIs live under this prefix; the next path segment is the session token
PROXY_URL_PREFIX = "/api/esphome_web_ui"
PANEL_URL_PATH = "esphome-web-ui"
STATIC_URL_PATH = "/esphome_web_ui_static"

# A session expires this long after its last proxied request or panel keepalive
SESSION_TTL = 300.0

# Streams to open per device at most. Should not exceed the device's tcp_proxy
# max_connections, which defaults to 4.
MAX_STREAMS_PER_DEVICE = 4
