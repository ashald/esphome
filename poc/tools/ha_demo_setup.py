"""Onboard a fresh Home Assistant, add the demo ESPHome device and this integration.

Writes the resulting tokens to the file given as the first argument (for the
browser demo). Assumes HA on 127.0.0.1:8123 and the demo device on 127.0.0.1:6053.
"""

import asyncio
import json
import pathlib
import sys

import aiohttp

HA = "http://127.0.0.1:8123"
CLIENT_ID = f"{HA}/"


async def flow(
    session: aiohttp.ClientSession, headers: dict, handler: str, *steps: dict
) -> dict:
    async with session.post(
        f"{HA}/api/config/config_entries/flow",
        headers=headers,
        json={"handler": handler, "show_advanced_options": False},
    ) as r:
        result = await r.json()
    for step in steps:
        async with session.post(
            f"{HA}/api/config/config_entries/flow/{result['flow_id']}",
            headers=headers,
            json=step,
        ) as r:
            result = await r.json()
    print(
        handler,
        "->",
        result.get("type"),
        result.get("title") or result.get("reason") or result,
    )
    return result


async def main(token_file: str) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA}/api/onboarding/users",
            json={
                "client_id": CLIENT_ID,
                "name": "Demo",
                "username": "demo",
                "password": "demo-password-123",
                "language": "en",
            },
        ) as r:
            code = (await r.json())["auth_code"]
        async with session.post(
            f"{HA}/auth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CLIENT_ID,
            },
        ) as r:
            tokens = await r.json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        for step in ("core_config", "analytics"):
            async with session.post(
                f"{HA}/api/onboarding/{step}", headers=headers, json={}
            ) as r:
                r.raise_for_status()
        async with session.post(
            f"{HA}/api/onboarding/integration",
            headers=headers,
            json={"client_id": CLIENT_ID, "redirect_uri": f"{HA}/?auth_callback=1"},
        ) as r:
            r.raise_for_status()

        await flow(session, headers, "esphome", {"host": "127.0.0.1", "port": 6053})
        await flow(session, headers, "esphome_web_ui", {})

    tokens.update({"hassUrl": HA, "clientId": CLIENT_ID})
    with pathlib.Path(token_file).open("w", encoding="utf-8") as f:
        json.dump(tokens, f)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
