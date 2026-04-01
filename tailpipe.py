#!/usr/bin/env python3
"""
TCP Reverse Tunnel (Tailscale Funnel edition)
=============================================

Forwards an internal service (e.g. MSSQL) from a remote environment to
your MacBook, using Tailscale Funnel as the public ingress point.
No third server needed.

Architecture:

    Environment (agent) ──TLS──► Tailscale Funnel ──► MacBook (server)
         │                                                │
    connects to                                     local port you
    internal MSSQL                                  connect to

Two modes, one script:

    server  — runs on your MacBook behind Tailscale Funnel
    agent   — runs in the environment, connects out to the Funnel URL

Setup:

    # 1. On your MacBook, expose port 9000 via Funnel
    tailscale funnel 9000

    # 2. Start the server on your MacBook
    python3 tunnel.py server --port 9000 --listen 1433 --token mysecret

    # 3. In the environment
    python3 tunnel.py agent --server your-machine.tail1234.ts.net --target localhost:1433 --token mysecret

    # 4. Connect your SQL client to localhost:1433

Protocol:

    The agent opens a persistent control connection (header 0x01) over
    TLS to the Funnel endpoint. When a local client connects to the
    server's listen port, the server signals the agent (1 byte). The
    agent opens a new TLS data connection (header 0x02) and bridges it
    to the target service. The server bridges the data connection to
    the local client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import signal
import ssl
import struct
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tunnel")

# Protocol constants
CTRL_CHANNEL = b"\x01"
DATA_CHANNEL = b"\x02"
SIGNAL_NEW_CLIENT = b"\x01"
SIGNAL_PING = b"\x02"
SIGNAL_PONG = b"\x03"
TOKEN_MAX_LEN = 256

HEARTBEAT_INTERVAL = 15   # seconds between pings
HEARTBEAT_TIMEOUT = 10    # seconds to wait for pong


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def bridge(
    r_a: asyncio.StreamReader, w_a: asyncio.StreamWriter,
    r_b: asyncio.StreamReader, w_b: asyncio.StreamWriter,
    label: str = "",
) -> None:
    """Bidirectionally pipe two stream pairs until either side closes."""

    async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try:
                dst.close()
                await dst.wait_closed()
            except OSError:
                pass

    log.debug("Bridge started (%s)", label)
    await asyncio.gather(copy(r_a, w_b), copy(r_b, w_a))
    log.debug("Bridge closed (%s)", label)


async def read_token(reader: asyncio.StreamReader) -> str:
    raw_len = await reader.readexactly(2)
    length = struct.unpack("!H", raw_len)[0]
    if length > TOKEN_MAX_LEN:
        raise ValueError("Token too long")
    return (await reader.readexactly(length)).decode()


def write_token(writer: asyncio.StreamWriter, token: str) -> None:
    encoded = token.encode()
    writer.write(struct.pack("!H", len(encoded)) + encoded)


# ---------------------------------------------------------------------------
# Server (runs on your MacBook)
# ---------------------------------------------------------------------------

async def run_server(
    funnel_port: int,
    listen_host: str,
    listen_port: int,
    token: str,
) -> None:
    ctrl_state: dict = {"reader": None, "writer": None}
    pending_clients: asyncio.Queue[tuple] = asyncio.Queue()

    async def handle_tunnel_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle inbound connections from the agent (arriving via Funnel)."""
        peer = writer.get_extra_info("peername")
        try:
            header = await asyncio.wait_for(reader.readexactly(1), timeout=10)
            incoming_token = await asyncio.wait_for(read_token(reader), timeout=10)
        except Exception:
            writer.close()
            return

        if incoming_token != token:
            log.warning("Auth failed from %s", peer)
            writer.close()
            return

        if header == CTRL_CHANNEL:
            if ctrl_state["writer"] is not None:
                log.info("Replacing previous agent control connection")
                try:
                    ctrl_state["writer"].close()
                except OSError:
                    pass

            ctrl_state["reader"] = reader
            ctrl_state["writer"] = writer
            log.info("Agent connected (control) from %s", peer)

            # Signal for any clients already waiting
            for _ in range(pending_clients.qsize()):
                writer.write(SIGNAL_NEW_CLIENT)
            await writer.drain()

            # Heartbeat: ping the agent periodically, expect pong back
            async def _server_ping_loop():
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    try:
                        writer.write(SIGNAL_PING)
                        await writer.drain()
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return

            async def _server_read_loop():
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return
                    # Agent sends pongs (and nothing else on control channel)
                    for byte in data:
                        if bytes([byte]) == SIGNAL_PONG:
                            log.debug("Pong from agent")

            ping_task = asyncio.create_task(_server_ping_loop())
            try:
                await _server_read_loop()
            except (ConnectionResetError, OSError):
                pass
            finally:
                ping_task.cancel()
                log.info("Agent control disconnected")
                ctrl_state["reader"] = None
                ctrl_state["writer"] = None

        elif header == DATA_CHANNEL:
            log.debug("Agent data channel from %s", peer)
            try:
                client = await asyncio.wait_for(pending_clients.get(), timeout=30)
            except asyncio.TimeoutError:
                log.warning("No pending client for this data channel")
                writer.close()
                return

            c_reader, c_writer = client
            log.info("Bridging local client ↔ agent")
            await bridge(reader, writer, c_reader, c_writer, label="server")

    async def handle_local_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle connections from local tools (e.g. SSMS on localhost:1433)."""
        peer = writer.get_extra_info("peername")
        log.info("Local client connected from %s", peer)

        if ctrl_state["writer"] is None:
            log.warning("No agent connected, rejecting local client")
            writer.close()
            return

        await pending_clients.put((reader, writer))
        try:
            ctrl_state["writer"].write(SIGNAL_NEW_CLIENT)
            await ctrl_state["writer"].drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            log.error("Failed to signal agent")
            writer.close()

    # Start the tunnel listener (Funnel sends traffic here)
    tunnel_server = await asyncio.start_server(handle_tunnel_conn, "127.0.0.1", funnel_port)
    log.info("Tunnel listener on 127.0.0.1:%d", funnel_port)

    # Start the local service listener
    local_server = await asyncio.start_server(handle_local_client, listen_host, listen_port)
    log.info("Local port open on %s:%d (connect your SQL client here)", listen_host, listen_port)

    await asyncio.gather(
        tunnel_server.serve_forever(),
        local_server.serve_forever(),
    )


# ---------------------------------------------------------------------------
# Agent (runs in the environment)
# ---------------------------------------------------------------------------

def make_tls_context(no_verify: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if no_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def agent_connect(
    host: str, port: int, tls_ctx: ssl.SSLContext | None, server_hostname: str | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if tls_ctx:
        return await asyncio.open_connection(host, port, ssl=tls_ctx, server_hostname=server_hostname)
    return await asyncio.open_connection(host, port)


async def run_agent(
    server_host: str,
    server_port: int,
    target_host: str,
    target_port: int,
    token: str,
    use_tls: bool = True,
    no_verify: bool = False,
) -> None:
    tls_ctx = make_tls_context(no_verify) if use_tls else None
    hostname = server_host if use_tls else None

    while True:
        try:
            log.info("Connecting to server %s:%d (tls=%s) ...", server_host, server_port, use_tls)
            ctrl_r, ctrl_w = await agent_connect(server_host, server_port, tls_ctx, hostname)

            ctrl_w.write(CTRL_CHANNEL)
            write_token(ctrl_w, token)
            await ctrl_w.drain()
            log.info("Control channel established")

            # Heartbeat: ping the server periodically to detect dead connections
            async def _agent_ping_loop():
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    try:
                        ctrl_w.write(SIGNAL_PONG)  # unsolicited pong as keepalive
                        await ctrl_w.drain()
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return

            ping_task = asyncio.create_task(_agent_ping_loop())
            try:
                while True:
                    sig = await ctrl_r.readexactly(1)
                    if sig == SIGNAL_NEW_CLIENT:
                        log.info("New client signal received")
                        asyncio.create_task(
                            _agent_data_channel(
                                server_host, server_port,
                                target_host, target_port,
                                token, tls_ctx, hostname,
                            )
                        )
                    elif sig == SIGNAL_PING:
                        ctrl_w.write(SIGNAL_PONG)
                        await ctrl_w.drain()
                        log.debug("Pong sent")
            finally:
                ping_task.cancel()
        except (ConnectionRefusedError, OSError, asyncio.IncompleteReadError) as exc:
            log.warning("Control connection lost (%s), reconnecting in 3s ...", exc)
        finally:
            try:
                ctrl_w.close()
                await ctrl_w.wait_closed()
            except Exception:
                pass
        await asyncio.sleep(3)


async def _agent_data_channel(
    server_host: str, server_port: int,
    target_host: str, target_port: int,
    token: str,
    tls_ctx: ssl.SSLContext | None,
    hostname: str | None,
) -> None:
    try:
        relay_r, relay_w = await agent_connect(server_host, server_port, tls_ctx, hostname)
        relay_w.write(DATA_CHANNEL)
        write_token(relay_w, token)
        await relay_w.drain()

        target_r, target_w = await asyncio.open_connection(target_host, target_port)
        log.info("Bridging server ↔ %s:%d", target_host, target_port)
        await bridge(relay_r, relay_w, target_r, target_w, label="agent")
    except (ConnectionRefusedError, OSError) as exc:
        log.error("Data channel failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_host_port(value: str, default_port: int | None = None) -> tuple[str, int]:
    if ":" in value:
        host, port_str = value.rsplit(":", 1)
        return host, int(port_str)
    if default_port is not None:
        return value, default_port
    raise ValueError(f"Expected host:port, got {value!r}")


# ---------------------------------------------------------------------------
# Tailscale Funnel helpers
# ---------------------------------------------------------------------------

def get_tailscale_hostname() -> str:
    """Get this machine's Tailscale DNS name."""
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tailscale status failed: {result.stderr.strip()}")
    status = json.loads(result.stdout)
    dns_name = status.get("Self", {}).get("DNSName", "")
    return dns_name.rstrip(".")


def start_funnel(funnel_port: int) -> None:
    """Configure Tailscale Funnel with TLS-terminated TCP on port 443.

    Errors out if Funnel can't be started (e.g. port 443 already in use).
    """
    result = subprocess.run(
        ["tailscale", "funnel", "--bg", "--tls-terminated-tcp", "443", str(funnel_port)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to start Tailscale Funnel: {result.stderr.strip() or result.stdout.strip()}"
        )
    log.info("Tailscale Funnel active (TLS-terminated TCP :443 → 127.0.0.1:%d)", funnel_port)


def stop_funnel() -> None:
    """Tear down the Funnel."""
    try:
        subprocess.run(
            ["tailscale", "funnel", "--tls-terminated-tcp=443", "off"],
            capture_output=True, text=True, timeout=10,
        )
        log.info("Tailscale Funnel stopped")
    except Exception as exc:
        log.warning("Failed to stop Funnel: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCP Reverse Tunnel (Tailscale Funnel edition)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)

    # server
    p_srv = sub.add_parser("server", help="Run on your MacBook (behind Tailscale Funnel)")
    p_srv.add_argument("--port", type=int, default=9000, help="Port Funnel delivers traffic to (default 9000)")
    p_srv.add_argument("--listen", default="1433", help="Local port for your SQL client (default 1433)")
    p_srv.add_argument("--token", default=None, help="Shared auth token (auto-generated if omitted)")

    # agent
    p_ag = sub.add_parser("agent", help="Run in the environment")
    p_ag.add_argument("--server", required=True, help="Funnel hostname (e.g. my-mac.tail1234.ts.net)")
    p_ag.add_argument("--port", type=int, default=443, help="Funnel port (default 443)")
    p_ag.add_argument("--target", default="localhost:1433", help="Internal service (default localhost:1433)")
    p_ag.add_argument("--token", required=True, help="Shared auth token")
    p_ag.add_argument("--no-tls", action="store_true", help="Disable TLS (for local testing without Funnel)")
    p_ag.add_argument("--no-verify", action="store_true", help="Skip TLS certificate verification")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    loop = asyncio.new_event_loop()
    if sys.platform != "win32":
        for sig_name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig_name, lambda: sys.exit(0))

    if args.mode == "server":
        if ":" in args.listen:
            lh, lp = parse_host_port(args.listen)
        else:
            lh, lp = "127.0.0.1", int(args.listen)

        token = args.token or secrets.token_hex(16)

        # Auto-start Tailscale Funnel
        hostname = get_tailscale_hostname()
        start_funnel(args.port)

        print()
        print(f"  Run on remote:")
        print(f"    python tailpipe.py agent --server {hostname} --token {token} --target localhost:1433")
        print()

        try:
            loop.run_until_complete(run_server(args.port, lh, lp, token))
        finally:
            stop_funnel()

    elif args.mode == "agent":
        th, tp = parse_host_port(args.target, 1433)
        loop.run_until_complete(
            run_agent(args.server, args.port, th, tp, args.token,
                      use_tls=not args.no_tls, no_verify=args.no_verify)
        )


if __name__ == "__main__":
    main()
