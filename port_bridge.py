"""Listen on many ports and forward to the main uvicorn port.

After a redeploy the proxy route sometimes points at a port the container
was not listening on. This bridge makes every plausible port reachable so
the route matches regardless of what the panel has configured.
"""

import asyncio
import os

MAIN_PORT = int(os.environ.get("PORT", "8000"))
EXTRA_PORTS = [int(p) for p in os.environ.get(
    "EXTRA_PORTS",
    "80 81 3000 3001 5000 7000 8001 8080 8081 8088 8880 8888 9000 9090 8443 9443 10443".split(),
)]

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def handle(client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", MAIN_PORT), timeout=5
        )
    except Exception:
        client_writer.close()
        return
    asyncio.ensure_future(pipe(client_reader, upstream_writer))
    await pipe(upstream_reader, client_writer)

async def serve(port):
    server = await asyncio.start_server(handle, "0.0.0.0", port)
    async with server:
        await server.serve_forever()

async def serve_safe(port):
    try:
        await serve(port)
    except Exception as exc:
        print(f"port bridge: skip {port}: {exc}", flush=True)

async def main():
    ports = [p for p in EXTRA_PORTS if p != MAIN_PORT]
    print(f"port bridge: {MAIN_PORT} (main) + extra {ports}", flush=True)
    await asyncio.gather(*(serve_safe(p) for p in ports))

if __name__ == "__main__":
    asyncio.run(main())
