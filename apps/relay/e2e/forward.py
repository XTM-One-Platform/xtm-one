"""Loopback to the platform for the relay e2e.

The relay sends its credential over ``https://``, or over plain ``http://`` to
a loopback host only. The e2e platform serves plain HTTP, so the relay runs in
this container's network namespace and reaches the platform at
``http://127.0.0.1:4000``, which this forwards to ``platform:4000`` - the
relay's own transport rule stays as shipped. Stdlib only.
"""

from __future__ import annotations

import asyncio


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def _serve(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection("platform", 4000)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        _pipe(client_reader, upstream_writer),
        _pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def main() -> None:
    server = await asyncio.start_server(_serve, "127.0.0.1", 4000)
    print("forwarding 127.0.0.1:4000 -> platform:4000", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
