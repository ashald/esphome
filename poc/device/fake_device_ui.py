"""Stand-in for an on-device web UI, served on the device's loopback interface.

ESPHome's web_server does not run on the host platform, so the demo firmware
relays to this server instead. It deliberately exercises everything a real
device UI needs from a proxy: relative asset URLs, a JSON API with POST,
Server-Sent Events (like web_server's /events), a WebSocket (like WLED's /ws)
and a large download to exercise flow control.

Usage: python fake_device_ui.py [port]
"""

import asyncio
import hashlib
import json
import sys

from aiohttp import WSMsgType, web

BIG = bytes((i * 7 + (i >> 8)) & 0xFF for i in range(1024 * 1024))
STATE = {"light": False, "brightness": 128}

INDEX = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Porch Light</title><link rel="stylesheet" href="style.css"></head>
<body>
<header><h1>Porch Light</h1><span id="uptime">connecting…</span></header>
<section class="card">
  <div class="row"><span>Light</span>
    <button id="toggle" class="switch" aria-pressed="false">OFF</button></div>
  <div class="row"><span>Brightness</span>
    <input id="brightness" type="range" min="0" max="255"></div>
</section>
<section class="card"><h2>WebSocket</h2>
  <div class="row"><input id="msg" value="hello device"><button id="send">Send</button></div>
  <pre id="echo">–</pre></section>
<section class="card"><h2>What the device sees</h2><pre id="headers">…</pre></section>
<script src="app.js"></script>
</body></html>
"""

CSS = """
:root { color-scheme: light dark; --accent: #03a9f4; }
body { font-family: system-ui, sans-serif; margin: 0; background: #1c1c1c; color: #eee; }
header { display: flex; justify-content: space-between; align-items: baseline;
         padding: 12px 16px; background: #263238; }
h1 { font-size: 20px; margin: 0; } h2 { font-size: 15px; margin: 0 0 8px; color: #aaa; }
#uptime { font-variant-numeric: tabular-nums; color: #9ccc65; }
.card { margin: 12px 16px; padding: 12px 16px; background: #2b2b2b; border-radius: 10px; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 6px 0; }
.switch { min-width: 64px; padding: 6px 12px; border: 0; border-radius: 16px; background: #555; color: #fff; }
.switch[aria-pressed=true] { background: var(--accent); }
pre { margin: 0; white-space: pre-wrap; font-size: 12px; color: #ccc; }
input[type=range] { flex: 1; }
"""

JS = """
const $ = (id) => document.getElementById(id);
function render(s) {
  $('toggle').textContent = s.light ? 'ON' : 'OFF';
  $('toggle').setAttribute('aria-pressed', s.light);
  $('brightness').value = s.brightness;
}
async function post(body) {
  const r = await fetch('api/state', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify(body)});
  render(await r.json());
}
$('toggle').onclick = () => post({light: $('toggle').getAttribute('aria-pressed') !== 'true'});
$('brightness').onchange = (e) => post({brightness: +e.target.value});
fetch('api/state').then(r => r.json()).then(render);
fetch('api/headers').then(r => r.json()).then(h => $('headers').textContent =
  Object.entries(h).map(([k, v]) => k + ': ' + v).join('\\n'));
const es = new EventSource('events');
es.addEventListener('tick', (e) => $('uptime').textContent = 'uptime ' + e.data + ' s');
es.addEventListener('state', (e) => render(JSON.parse(e.data)));
const ws = new WebSocket(new URL('ws', location.href).href.replace(/^http/, 'ws'));
ws.onmessage = (e) => $('echo').textContent = e.data;
ws.onopen = () => $('echo').textContent = 'websocket open';
$('send').onclick = () => ws.send($('msg').value);
"""

subscribers: set[asyncio.Queue] = set()


def publish(event: str, data: str) -> None:
    for queue in subscribers:
        queue.put_nowait((event, data))


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX, content_type="text/html")


async def style(request: web.Request) -> web.Response:
    return web.Response(text=CSS, content_type="text/css")


async def app_js(request: web.Request) -> web.Response:
    return web.Response(text=JS, content_type="application/javascript")


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def post_state(request: web.Request) -> web.Response:
    STATE.update({k: v for k, v in (await request.json()).items() if k in STATE})
    publish("state", json.dumps(STATE))
    return web.json_response(STATE)


async def headers(request: web.Request) -> web.Response:
    shown = {"Request": f"{request.method} {request.path_qs}"}
    shown.update({k: v for k, v in request.headers.items() if k.lower() != "cookie"})
    return web.json_response(shown)


async def events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
    )
    await resp.prepare(request)
    queue: asyncio.Queue = asyncio.Queue()
    subscribers.add(queue)
    try:
        await resp.write(f"event: state\ndata: {json.dumps(STATE)}\n\n".encode())
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), 1)
            except TimeoutError:
                event, data = "tick", str(int(asyncio.get_running_loop().time()))
            await resp.write(f"event: {event}\ndata: {data}\n\n".encode())
    finally:
        subscribers.discard(queue)


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            await ws.send_str(f"device echoes: {msg.data}")
    return ws


async def big(request: web.Request) -> web.Response:
    return web.Response(
        body=BIG,
        content_type="application/octet-stream",
        headers={"X-SHA256": hashlib.sha256(BIG).hexdigest()},
    )


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/", index),
            web.get("/style.css", style),
            web.get("/app.js", app_js),
            web.get("/api/state", get_state),
            web.post("/api/state", post_state),
            web.get("/api/headers", headers),
            web.get("/events", events),
            web.get("/ws", websocket),
            web.get("/big.bin", big),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(
        make_app(),
        host="127.0.0.1",
        port=int(sys.argv[1]) if len(sys.argv) > 1 else 8080,
    )
