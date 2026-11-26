"""Logged, public-internet-only HTTP/HTTPS proxy for tenant containers.

Tenant containers have no direct route to the internet. They set HTTP_PROXY /
HTTPS_PROXY to this service. This proxy:

- allows only public IP addresses (blocks loopback, private, link-local,
  multicast, reserved and metadata addresses -- SSRF guard);
- allows only HTTP/HTTPS ports (default 80,443);
- logs the destination host, port and byte counts, never paths or bodies;
- supports CONNECT tunnelling for HTTPS and simple HTTP GET/HEAD requests.

Set ``WEB_EGRESS_ALLOW_ALL=0`` and ``WEB_EGRESS_ALLOW_HOSTS=host1,*.host2`` to
switch from logged-open to an allowlist without changing the network topology.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

logging.basicConfig(level=os.environ.get("WEB_EGRESS_LOG_LEVEL", "INFO"))
logger = logging.getLogger("hermes.web_egress")

HOST = os.environ.get("WEB_EGRESS_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEB_EGRESS_PORT", "8081"))
ALLOW_ALL = os.environ.get("WEB_EGRESS_ALLOW_ALL", "1").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_HOSTS = {
    item.strip().lower().lstrip(".")
    for item in os.environ.get("WEB_EGRESS_ALLOW_HOSTS", "").split(",")
    if item.strip()
}
ALLOW_PORTS = {
    int(item) for item in os.environ.get("WEB_EGRESS_ALLOW_PORTS", "80,443").split(",") if item.strip()
}
ALLOW_CIDRS = []
for item in os.environ.get("WEB_EGRESS_ALLOW_CIDRS", "").split(","):
    item = item.strip()
    if not item:
        continue
    try:
        ALLOW_CIDRS.append(ipaddress.ip_network(item, strict=False))
    except ValueError:
        pass
MAX_HEADER_BYTES = 32768


def _host_allowed(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized in ALLOW_HOSTS:
        return True
    if ALLOW_ALL:
        return True
    for entry in ALLOW_HOSTS:
        if normalized == entry or normalized.endswith("." + entry):
            return True
    return False


def _public_addr(host: str, port: int) -> tuple[int, str] | None:
    """Resolve and return the first globally routable address, or None."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for family, _type, _proto, _canon, address in infos:
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            continue
        if any(ip in network for network in ALLOW_CIDRS):
            return family, address[0]
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            continue
        return family, address[0]
    return None


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            total += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass
    return total


def _split_target(target: str) -> tuple[str, int] | None:
    if target.startswith("["):
        # [ipv6]:port
        host, sep, port_text = target[1:].partition("]:")
        if not sep:
            return None
    else:
        host, sep, port_text = target.rpartition(":")
        if not sep:
            host, port_text = target, "443"
    try:
        port = int(port_text)
    except ValueError:
        return None
    return host.strip(), port


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    host = "-"
    port = 0
    sent = received = 0
    try:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        if len(header) > MAX_HEADER_BYTES:
            writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        text = header.decode("latin-1", "replace")
        lines = text.split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        if method.upper() == "CONNECT":
            parsed = _split_target(target)
            if parsed is None:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            host, port = parsed
            if not _host_allowed(host) or port not in ALLOW_PORTS:
                logger.info("web-egress DENY CONNECT host=%s port=%s", host, port)
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            address = await asyncio.to_thread(_public_addr, host, port)
            if address is None:
                logger.info("web-egress DENY private-or-unresolved host=%s port=%s", host, port)
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            try:
                remote_reader, remote_writer = await asyncio.open_connection(address[1], port)
            except OSError:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            sent_task = asyncio.create_task(_relay(reader, remote_writer))
            recv_task = asyncio.create_task(_relay(remote_reader, writer))
            results = await asyncio.gather(sent_task, recv_task)
            sent, received = results[0], results[1]
            return

        if method.upper() not in {"GET", "HEAD"}:
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        parsed_url = urlsplit(target)
        if parsed_url.scheme.lower() != "http" or not parsed_url.hostname:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host = parsed_url.hostname
        port = parsed_url.port or 80
        if not _host_allowed(host) or port not in ALLOW_PORTS:
            logger.info("web-egress DENY http host=%s port=%s", host, port)
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        address = await asyncio.to_thread(_public_addr, host, port)
        if address is None:
            logger.info("web-egress DENY private-or-unresolved host=%s port=%s", host, port)
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            remote_reader, remote_writer = await asyncio.open_connection(address[1], port)
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        path = parsed_url.path or "/"
        if parsed_url.query:
            path += "?" + parsed_url.query
        forwarded = [f"{method.upper()} {path} {version}"]
        for line in lines[1:]:
            lower = line.lower()
            if not line or lower.startswith(("proxy-connection:", "connection:", "keep-alive:")):
                continue
            forwarded.append(line)
        forwarded.append("Connection: close")
        remote_writer.write(("\r\n".join(forwarded) + "\r\n\r\n").encode("latin-1"))
        await remote_writer.drain()
        sent = len(header)
        received = await _relay(remote_reader, writer)
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("web-egress handler failed")
    finally:
        logger.info("web-egress host=%s port=%s sent=%s received=%s", host, port, sent, received)
        try:
            writer.close()
        except OSError:
            pass


async def main() -> None:
    server = await asyncio.start_server(_handle, HOST, PORT)
    logger.info(
        "web-egress listening on %s:%s allow_all=%s allow_hosts=%s ports=%s",
        HOST,
        PORT,
        ALLOW_ALL,
        sorted(ALLOW_HOSTS),
        sorted(ALLOW_PORTS),
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
