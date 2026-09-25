"""Check the proxy end to end through a running Home Assistant.

Browser -> HA (/api/esphome_web_ui/<token>/...) -> native API tunnel -> device -> UI.
Uses the tokens written by ha_demo_setup.py.
"""

import asyncio
import hashlib
import json
import pathlib
import sys

import aiohttp

HA = "http://127.0.0.1:8123"


async def main(token_file: str) -> None:
    tokens = json.loads(pathlib.Path(token_file).read_text(encoding="utf-8"))
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{HA}/auth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens["clientId"],
            },
        ) as r:
            access = (await r.json())["access_token"]

        async with http.ws_connect(f"{HA}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": access})
            assert (await ws.receive_json())["type"] == "auth_ok"
            await ws.send_json({"id": 1, "type": "esphome_web_ui/list"})
            uis = (await ws.receive_json())["result"]
            print("device UIs:", uis)
            ui = uis[0]
            await ws.send_json(
                {
                    "id": 2,
                    "type": "esphome_web_ui/session",
                    "device_id": ui["device_id"],
                    "target": ui["target"],
                }
            )
            session = (await ws.receive_json())["result"]
            await ws.send_json({"id": 3, "type": "config/device_registry/list"})
            devices = (await ws.receive_json())["result"]
            device = next(d for d in devices if d["id"] == ui["device_id"])
            print("device page 'Visit' link:", device["configuration_url"])

        base = f"{HA}{session['url'].rstrip('/')}"
        print("session url:", session["url"][:40] + "…")
        # Note: no Authorization header from here on, like an iframe
        async with http.get(base + "/") as r:
            body = await r.text()
            print(
                "GET /:",
                r.status,
                r.headers.get("Content-Security-Policy"),
                "| title ok:",
                "Porch Light" in body,
            )
        async with http.get(base + "/big.bin") as r:
            data = await r.read()
            assert hashlib.sha256(data).hexdigest() == r.headers["X-SHA256"]
            print(f"GET /big.bin: {r.status}, {len(data)} bytes, sha256 ok")
        async with http.get(base + "/api/headers") as r:
            seen = await r.json()
            print(
                "device saw:",
                {
                    k: seen[k]
                    for k in ("Request", "Host", "X-Ingress-Path")
                    if k in seen
                },
                "Origin forwarded:",
                "Origin" in seen,
            )
        # Opaque-origin (isolated) POST with JSON needs a CORS preflight
        async with http.options(
            base + "/api/state",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        ) as r:
            print(
                "preflight:",
                r.status,
                r.headers.get("Access-Control-Allow-Origin"),
                r.headers.get("Access-Control-Allow-Headers"),
            )
        async with http.post(
            base + "/api/state", json={"light": True}, headers={"Origin": "null"}
        ) as r:
            print(
                "POST /api/state:",
                r.status,
                await r.json(),
                "ACAO:",
                r.headers.get("Access-Control-Allow-Origin"),
            )
        async with http.get(base + "/events") as r:
            events = []
            async for line in r.content:
                if line.startswith(b"event:"):
                    events.append(line.decode().split(":", 1)[1].strip())
                if len(events) >= 3:
                    break
            print("SSE events:", events, "| content-type:", r.headers["Content-Type"])
        async with http.ws_connect(base.replace("http", "ws") + "/ws") as ws:
            await ws.send_str("ping over the tunnel")
            print("websocket:", (await ws.receive()).data)
        async with http.get(f"{HA}/api/esphome_web_ui/not-a-session/") as r:
            print("unknown session:", r.status)
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
