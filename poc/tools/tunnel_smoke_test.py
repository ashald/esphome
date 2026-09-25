"""Exercise the tcp_proxy tunnel directly with the patched aioesphomeapi.

Needs the host demo firmware (poc/device/tunnel-demo.yaml) listening on
127.0.0.1:6053 and poc/device/fake_device_ui.py on 127.0.0.1:8080.
"""

import asyncio
import hashlib
import sys
import time

from aioesphomeapi import APIClient
from aioesphomeapi.model import TcpProxyStatus
from aioesphomeapi.tcp_proxy import TcpProxyError


async def http_get(client: APIClient, path: str) -> tuple[bytes, bytes]:
    stream = await client.tcp_proxy_open(0)
    await stream.send(
        f"GET {path} HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n".encode()
    )
    chunks = []
    while chunk := await stream.read():
        chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    return head, body


async def expect_status(coro, status: TcpProxyStatus) -> None:
    try:
        stream = await coro
    except TcpProxyError as err:
        assert err.status == status, err
        print(f"  refused as expected: {status.name}")
        return
    stream.close()
    raise AssertionError(f"expected {status.name}")


async def main() -> None:
    client = APIClient("127.0.0.1", 6053, None)
    await client.connect(login=True)
    caps = await client.device_capabilities()
    print("targets:", [(t.name, t.type.name, t.path) for t in caps.tcp_proxy_targets])

    head, body = await http_get(client, "/")
    print("GET /:", head.split(b"\r\n")[0].decode(), f"{len(body)} bytes")
    assert b"Porch Light" in body

    start = time.monotonic()
    head, body = await http_get(client, "/big.bin")
    elapsed = time.monotonic() - start
    expected = next(
        line.split(b": ")[1]
        for line in head.split(b"\r\n")
        if line.lower().startswith(b"x-sha256")
    )
    assert hashlib.sha256(body).hexdigest().encode() == expected, "checksum mismatch"
    print(
        f"GET /big.bin: {len(body)} bytes, sha256 ok, {len(body) / elapsed / 1024:.0f} KiB/s"
    )

    # Four slots on the device: fill them, then a fifth must be refused
    streams = [await client.tcp_proxy_open(0) for _ in range(4)]
    print("opened 4 concurrent streams")
    await expect_status(client.tcp_proxy_open(0), TcpProxyStatus.NO_RESOURCES)
    for stream in streams:
        stream.close()
    await asyncio.sleep(0.2)
    head, _ = await http_get(client, "/api/state")
    print("slots reusable after close:", head.split(b"\r\n")[0].decode())

    await expect_status(client.tcp_proxy_open(7), TcpProxyStatus.INVALID_ARGUMENT)
    await expect_status(client.tcp_proxy_open(1), TcpProxyStatus.CONNECT_FAILED)

    # Parallel downloads share the connection and must not corrupt each other
    results = await asyncio.gather(*(http_get(client, "/big.bin") for _ in range(3)))
    assert all(
        hashlib.sha256(body).hexdigest().encode() in head for head, body in results
    )
    print("3 parallel 1 MiB downloads: all checksums ok")

    await client.disconnect()
    print("ALL OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
