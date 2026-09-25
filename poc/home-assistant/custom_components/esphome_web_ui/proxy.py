"""Reverse proxy from Home Assistant's HTTP server to device web UIs.

Requests arrive at /api/esphome_web_ui/<session token>/<path>. The token names
an authenticated session (see __init__.py); the request is forwarded over a TCP
proxy stream on the device's native API connection, so the device's web server
never has to be reachable from the browser, or even from the LAN.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import logging
import re
import socket
from typing import TYPE_CHECKING, Any

from aioesphomeapi import APIClient
from aioesphomeapi.model import TcpProxyStatus
from aioesphomeapi.tcp_proxy import TcpProxyError, TcpProxyStream
import aiohttp
from aiohttp import ClientTimeout, hdrs, web
from aiohttp.client_proto import ResponseHandler
from aiohttp.helpers import must_be_empty_body
from homeassistant.components.http import KEY_HASS
from homeassistant.core import HomeAssistant, callback
from multidict import CIMultiDict
from yarl import URL

from .const import DOMAIN, PROXY_URL_PREFIX

if TYPE_CHECKING:
    from . import Session

_LOGGER = logging.getLogger(__name__)

# The device relays to an address fixed in its firmware; this name only fills the
# Host header, and web servers on the device do not care about it
UPSTREAM_HOST = "localhost"

HOP_BY_HOP = {
    hdrs.CONNECTION,
    hdrs.KEEP_ALIVE,
    hdrs.PROXY_AUTHENTICATE,
    hdrs.PROXY_AUTHORIZATION,
    hdrs.TE,
    hdrs.TRAILER,
    hdrs.TRANSFER_ENCODING,
    hdrs.UPGRADE,
}
REQUEST_HEADERS_FILTER = HOP_BY_HOP | {
    hdrs.HOST,
    # HA already authenticated the user. Browsers send the HA origin here, which a
    # device (ESPHome web_server among them) would reject as cross-origin
    hdrs.ORIGIN,
    # Carries the session token
    hdrs.REFERER,
    # Let aiohttp negotiate what it can decode; HA re-compresses towards the browser
    hdrs.ACCEPT_ENCODING,
    hdrs.CONTENT_LENGTH,
    hdrs.SEC_WEBSOCKET_EXTENSIONS,
    hdrs.SEC_WEBSOCKET_PROTOCOL,
    hdrs.SEC_WEBSOCKET_VERSION,
    hdrs.SEC_WEBSOCKET_KEY,
}
RESPONSE_HEADERS_FILTER = HOP_BY_HOP | {
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_TYPE,
    hdrs.CONTENT_ENCODING,
    # The UI is meant to be framed by HA, and HA sets its own policy
    "X-Frame-Options",
    "Content-Security-Policy",
    # Device CORS answers name the device's origin, not HA's
    hdrs.ACCESS_CONTROL_ALLOW_ORIGIN,
    hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS,
    # Rewritten below so device cookies stay under the session prefix
    hdrs.SET_COOKIE,
    hdrs.LOCATION,
}

# Applied to every response of an isolated session. The page gets an opaque
# origin, so it cannot read Home Assistant's storage (and access token) even
# though it is served from Home Assistant's origin. This also holds when the
# user opens the UI in a new tab, where the iframe sandbox attribute is gone.
ISOLATION_CSP = (
    "sandbox allow-scripts allow-forms allow-popups "
    "allow-popups-to-escape-sandbox allow-modals allow-downloads"
)

MAX_SIMPLE_RESPONSE_SIZE = 4 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_SIZE = 16 * 1024 * 1024
NO_RESOURCES_RETRIES = 25
NO_RESOURCES_DELAY = 0.2

# Root-relative references in HTML ("/0.js") would escape the session prefix
ROOT_RELATIVE_ATTR = re.compile(
    rb"""(\s(?:src|href|action)\s*=\s*["'])/(?!/)""", re.IGNORECASE
)


class TunnelConnector(aiohttp.BaseConnector):
    """aiohttp connector whose connections are TCP proxy streams on a device.

    aiohttp drives a normal asyncio transport over one end of a socketpair; the
    other end is pumped to and from the stream. That keeps HTTP parsing, keep-alive,
    chunked bodies and WebSocket upgrades entirely in aiohttp.
    """

    def __init__(
        self, get_client: Callable[[], APIClient], target: int, limit: int
    ) -> None:
        super().__init__(limit=limit, limit_per_host=limit, keepalive_timeout=5.0)
        self._get_client = get_client
        self._target = target
        self._pumps: set[asyncio.Task[None]] = set()

    async def _create_connection(
        self, req: aiohttp.ClientRequest, traces: list[Any], timeout: ClientTimeout
    ) -> ResponseHandler:
        stream = await self._open_stream()
        ours, theirs = socket.socketpair()
        try:
            ours.setblocking(False)
            theirs.setblocking(False)
            _, proto = await self._loop.create_connection(self._factory, sock=ours)
        except OSError as err:
            ours.close()
            theirs.close()
            stream.close()
            raise aiohttp.ClientConnectionError(str(err)) from err
        task = self._loop.create_task(_pump(theirs, stream))
        self._pumps.add(task)
        task.add_done_callback(self._pumps.discard)
        return proto

    async def _open_stream(self) -> TcpProxyStream:
        for attempt in range(NO_RESOURCES_RETRIES + 1):
            try:
                return await self._get_client().tcp_proxy_open(self._target)
            except TcpProxyError as err:
                # Slots free up as idle keep-alive streams are closed
                if (
                    err.status is TcpProxyStatus.NO_RESOURCES
                    and attempt < NO_RESOURCES_RETRIES
                ):
                    await asyncio.sleep(NO_RESOURCES_DELAY)
                    continue
                raise aiohttp.ClientConnectionError(str(err)) from err
            except Exception as err:  # Not connected, API errors
                raise aiohttp.ClientConnectionError(str(err)) from err
        raise AssertionError("unreachable")

    async def close(self, *, abort_ssl: bool = False) -> None:
        for task in self._pumps:
            task.cancel()
        await super().close()


async def _pump(sock: socket.socket, stream: TcpProxyStream) -> None:
    """Copy bytes between a socketpair end and a stream until either side closes."""
    reader, writer = await asyncio.open_connection(sock=sock)

    async def to_device() -> None:
        try:
            while data := await reader.read(65536):
                await stream.send(data)
        finally:
            stream.close()

    async def from_device() -> None:
        try:
            while data := await stream.read():
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    results = await asyncio.gather(to_device(), from_device(), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception) and not isinstance(
            result, (TcpProxyError, ConnectionError)
        ):
            _LOGGER.debug("Stream %s ended with %r", stream.stream_id, result)


@callback
def async_register_proxy(hass: HomeAssistant) -> None:
    """Route every method under the proxy prefix to the proxy.

    Not a HomeAssistantView: those get HA's CORS handling attached, which claims
    OPTIONS for itself (only /api/hassio_ingress/ is exempt). Isolated UIs have an
    opaque origin, so their preflights must reach us.
    """
    resource = hass.http.app.router.add_resource(
        PROXY_URL_PREFIX + "/{token}/{path:.*}", name="esphome_web_ui:proxy"
    )
    resource.add_route(hdrs.METH_ANY, _handle)


async def _handle(request: web.Request) -> web.StreamResponse:
    """Proxy a request for a session to the device UI it names."""
    # The browser cannot attach HA's bearer token to iframe requests; the session
    # token in the path authenticates instead (compare hassio ingress)
    token = request.match_info["token"]
    hass = request.app[KEY_HASS]
    manager = hass.data.get(DOMAIN)
    if manager is None or (session := manager.touch_session(token)) is None:
        raise web.HTTPNotFound

    prefix = f"{PROXY_URL_PREFIX}/{token}"
    # Isolated pages have an opaque origin, so their requests to us are cross-origin
    cross_origin = request.headers.get(hdrs.ORIGIN) == "null"
    if (
        cross_origin
        and request.method == hdrs.METH_OPTIONS
        and (hdrs.ACCESS_CONTROL_REQUEST_METHOD in request.headers)
    ):
        return _preflight_response(request)

    client_session = manager.http_session(session.ui)
    if client_session is None:
        raise web.HTTPServiceUnavailable(text="Device is not connected")
    url = URL.build(
        scheme="http",
        host=UPSTREAM_HOST,
        path=request.rel_url.raw_path[len(prefix) :] or "/",
        query_string=request.rel_url.raw_query_string,
        encoded=True,
    )
    headers = _request_headers(request, prefix)
    try:
        if _is_websocket(request):
            return await _proxy_websocket(request, client_session, url, headers)
        return await _proxy_request(
            request, client_session, url, headers, prefix, session, cross_origin
        )
    except aiohttp.ClientError as err:
        _LOGGER.debug("Proxying %s to %s failed: %s", url.path, session.ui.name, err)
        raise web.HTTPBadGateway(text="Device UI is not reachable") from None


def _preflight_response(request: web.Request) -> web.Response:
    headers = {
        hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "*",
        hdrs.ACCESS_CONTROL_ALLOW_METHODS: "GET, HEAD, POST, PUT, PATCH, DELETE",
        hdrs.ACCESS_CONTROL_MAX_AGE: "600",
    }
    if requested := request.headers.get(hdrs.ACCESS_CONTROL_REQUEST_HEADERS):
        headers[hdrs.ACCESS_CONTROL_ALLOW_HEADERS] = requested
    return web.Response(status=204, headers=headers)


def _request_headers(request: web.Request, prefix: str) -> CIMultiDict[str]:
    headers = CIMultiDict(
        (name, value)
        for name, value in request.headers.items()
        if name not in REQUEST_HEADERS_FILTER
    )
    headers[hdrs.HOST] = UPSTREAM_HOST
    # Same header Supervisor ingress uses, so UIs built for ingress can adapt links
    headers["X-Ingress-Path"] = prefix
    if peername := request.transport and request.transport.get_extra_info("peername"):
        forwarded = request.headers.get(hdrs.X_FORWARDED_FOR)
        headers[hdrs.X_FORWARDED_FOR] = (
            f"{forwarded}, {peername[0]}" if forwarded else str(peername[0])
        )
    headers[hdrs.X_FORWARDED_HOST] = request.headers.get(
        hdrs.X_FORWARDED_HOST, request.host
    )
    headers[hdrs.X_FORWARDED_PROTO] = request.headers.get(
        hdrs.X_FORWARDED_PROTO, request.scheme
    )
    return headers


def _response_headers(
    result: aiohttp.ClientResponse, prefix: str, session: Session, cross_origin: bool
) -> CIMultiDict[str]:
    headers = CIMultiDict(
        (name, value)
        for name, value in result.headers.items()
        if name not in RESPONSE_HEADERS_FILTER
    )
    for cookie in result.headers.getall(hdrs.SET_COOKIE, ()):
        headers.add(hdrs.SET_COOKIE, _scope_cookie(cookie, prefix))
    if (location := result.headers.get(hdrs.LOCATION)) is not None:
        headers[hdrs.LOCATION] = _rewrite_location(location, prefix)
    if session.isolated:
        headers["Content-Security-Policy"] = ISOLATION_CSP
    if cross_origin:
        headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = "*"
        headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS] = "*"
    return headers


def _scope_cookie(cookie: str, prefix: str) -> str:
    """Keep a device cookie under the session prefix, never on HA's own paths."""
    parts = [
        part
        for part in cookie.split(";")
        if part.strip().split("=", 1)[0].strip().lower() not in ("path", "domain")
    ]
    parts.append(f" Path={prefix}/")
    return ";".join(parts)


def _rewrite_location(location: str, prefix: str) -> str:
    url = URL(location)
    if url.is_absolute():
        if url.host not in (UPSTREAM_HOST, None):
            return location  # Redirect somewhere else entirely
        url = url.relative()
    if url.path.startswith("/"):
        return f"{prefix}{url.raw_path}" + (
            f"?{url.raw_query_string}" if url.raw_query_string else ""
        )
    return location


def _is_websocket(request: web.Request) -> bool:
    return (
        "upgrade" in request.headers.get(hdrs.CONNECTION, "").lower()
        and request.headers.get(hdrs.UPGRADE, "").lower() == "websocket"
    )


def _should_compress(content_type: str) -> bool:
    if content_type == "text/event-stream":
        return False  # Compression buffers, which would stall live events
    return content_type.startswith("text/") or content_type in (
        "application/javascript",
        "application/json",
        "application/xml",
        "image/svg+xml",
    )


async def _proxy_request(
    request: web.Request,
    client_session: aiohttp.ClientSession,
    url: URL,
    headers: CIMultiDict[str],
    prefix: str,
    session: Session,
    cross_origin: bool,
) -> web.StreamResponse:
    async with client_session.request(
        request.method,
        url,
        headers=headers,
        data=request.content if request.body_exists else None,
        allow_redirects=False,
        timeout=ClientTimeout(total=None, sock_connect=30),
        skip_auto_headers={hdrs.CONTENT_TYPE},
    ) as result:
        response_headers = _response_headers(result, prefix, session, cross_origin)
        content_type = (
            result.headers.get(hdrs.CONTENT_TYPE, "application/octet-stream")
            .partition(";")[0]
            .strip()
        )
        if must_be_empty_body(request.method, result.status):
            return web.Response(status=result.status, headers=response_headers)

        length = result.headers.get(hdrs.CONTENT_LENGTH)
        if length is not None and int(length) <= MAX_SIMPLE_RESPONSE_SIZE:
            body = await result.read()
            if content_type == "text/html":
                body = ROOT_RELATIVE_ATTR.sub(rb"\1" + prefix.encode() + b"/", body)
            response = web.Response(
                status=result.status,
                headers=response_headers,
                body=body,
                content_type=content_type,
            )
            if _should_compress(content_type):
                response.enable_compression()
            return response

        # Streamed: chunked bodies, large downloads, and server-sent events
        response = web.StreamResponse(status=result.status, headers=response_headers)
        response.content_type = content_type
        if _should_compress(content_type):
            response.enable_compression()
        await response.prepare(request)
        try:
            async for data, _ in result.content.iter_chunks():
                await response.write(data)
        except (aiohttp.ClientError, ConnectionError) as err:
            _LOGGER.debug("Stream from %s ended: %s", url, err)
        return response


async def _proxy_websocket(
    request: web.Request,
    client_session: aiohttp.ClientSession,
    url: URL,
    headers: CIMultiDict[str],
) -> web.WebSocketResponse:
    protocols: Iterable[str] = [
        proto.strip()
        for proto in request.headers.get(hdrs.SEC_WEBSOCKET_PROTOCOL, "").split(",")
        if proto.strip()
    ]
    ws_server = web.WebSocketResponse(
        protocols=protocols,
        autoclose=False,
        autoping=False,
        max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
    )
    await ws_server.prepare(request)
    async with client_session.ws_connect(
        url,
        headers=headers,
        protocols=protocols,
        autoclose=False,
        autoping=False,
        max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
    ) as ws_client:
        await asyncio.wait(
            [
                asyncio.create_task(_websocket_forward(ws_server, ws_client)),
                asyncio.create_task(_websocket_forward(ws_client, ws_server)),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
    await ws_server.close()
    return ws_server


async def _websocket_forward(
    ws_from: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    ws_to: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
) -> None:
    try:
        async for msg in ws_from:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await ws_to.send_str(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await ws_to.send_bytes(msg.data)
            elif msg.type is aiohttp.WSMsgType.PING:
                await ws_to.ping(msg.data)
            elif msg.type is aiohttp.WSMsgType.PONG:
                await ws_to.pong(msg.data)
            elif msg.type is aiohttp.WSMsgType.CLOSE:
                await ws_to.close(code=ws_from.close_code or 1000)
                return
    except (RuntimeError, ConnectionError):
        pass
