#!/usr/bin/env python3
"""tailpipe — TCP reverse tunnel over Tailscale Funnel. See README.md."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import logging
import os
import secrets
import signal
import ssl
import struct
import subprocess
import sys

log = logging.getLogger("tunnel")

# Protocol constants
CTRL_CHANNEL = b"\x01"
DATA_CHANNEL = b"\x02"
SIGNAL_NEW_CLIENT = b"\x01"
SIGNAL_PING = b"\x02"
SIGNAL_PONG = b"\x03"
TOKEN_MAX_LEN = 256

HEARTBEAT_INTERVAL = 15
RAW_URL = "https://raw.githubusercontent.com/jqwn/tailpipe/main/tailpipe.py"


# ---------------------------------------------------------------------------
# TUI — sticky header + scrolling log
# ---------------------------------------------------------------------------

class TuiDisplay:
    def __init__(self, max_log_lines: int = 20):
        self._header: list[str] = []
        self._logs: deque[str] = deque(maxlen=max_log_lines)

    def set_header(self, lines: list[str]) -> None:
        self._header = lines
        self._redraw()

    def add_log(self, msg: str) -> None:
        self._logs.append(msg)
        self._redraw()

    def _redraw(self) -> None:
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80
        sep = "─" * min(cols, 60)
        out = "\033[H"  # cursor home
        for line in self._header:
            out += f"\033[2K{line}\n"
        out += f"\033[2K{sep}\n\033[2K\n"
        for line in self._logs:
            out += f"\033[2K{line}\n"
        out += "\033[J"  # clear rest of screen
        sys.stdout.write(out)
        sys.stdout.flush()


class TuiLogHandler(logging.Handler):
    def __init__(self, display: TuiDisplay):
        super().__init__()
        self.display = display

    def emit(self, record: logging.LogRecord) -> None:
        self.display.add_log(self.format(record))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def bridge(r_a, w_a, r_b, w_b, label=""):
    async def copy(src, dst):
        try:
            while data := await src.read(65536):
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
    length = struct.unpack("!H", await reader.readexactly(2))[0]
    if length > TOKEN_MAX_LEN:
        raise ValueError("Token too long")
    return (await reader.readexactly(length)).decode()


def write_token(writer: asyncio.StreamWriter, token: str) -> None:
    encoded = token.encode()
    writer.write(struct.pack("!H", len(encoded)) + encoded)


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    return await reader.readexactly(length)


def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(struct.pack("!I", len(data)) + data)


def parse_target(value: str) -> dict:
    """Parse 'name=host:port' or 'host:port' (name defaults to port number)."""
    if "=" in value:
        name, hostport = value.split("=", 1)
    else:
        name, hostport = None, value
    host, port_str = hostport.rsplit(":", 1)
    return {"name": name or port_str, "host": host, "port": int(port_str)}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

async def run_server(funnel_port: int, token: str, hostname: str,
                     bind_host: str = "127.0.0.1", tui: TuiDisplay | None = None):
    ctrl_state: dict = {"reader": None, "writer": None}
    # name -> {server, listen_port, queue, target_host, target_port}
    target_state: dict[str, dict] = {}
    targets: list[dict] = []  # ordered, matches agent indices

    def build_header() -> list[str]:
        lines = [""]
        if not targets:
            lines += [
                "  Waiting for agent...",
                "",
                "  Run on remote:",
                f"    python tailpipe.py agent --server {hostname} --token {token} \\",
                "      --target name=host:port [--target ...]",
                "",
                "  Or with uv:",
                f"    uv run {RAW_URL} agent \\",
                f"      --server {hostname} --token {token} \\",
                "      --target name=host:port [--target ...]",
            ]
        else:
            target_args = [f"--target {t['name']}={t['host']}:{t['port']}" for t in targets]
            lines += [
                "  Run on remote:",
                f"    python tailpipe.py agent --server {hostname} --token {token} \\",
                "      --target name=host:port [--target ...]",
                "",
                "  Or with uv:",
                f"    uv run {RAW_URL} agent \\",
                f"      --server {hostname} --token {token} \\",
                "      --target name=host:port [--target ...]",
                "",
                "  Connected targets:",
            ]
            for i, ta in enumerate(target_args):
                lines.append(f"    {ta}")
            lines += ["", "  Mappings:"]
            name_w = max(len(t["name"]) for t in targets)
            for t in targets:
                st = target_state[t["name"]]
                remote = f"{t['host']}:{t['port']}"
                local = f"localhost:{st['listen_port']}"
                note = f"  ({t['port']} in use)" if st["listen_port"] != t["port"] else ""
                lines.append(f"    {t['name']:<{name_w}}  {remote}  ->  {local}{note}")
        lines.append("")
        return lines

    async def update_targets(new_targets: list[dict]) -> None:
        nonlocal targets
        new_by_name = {t["name"]: t for t in new_targets}
        old_names = set(target_state.keys())
        new_names = set(new_by_name.keys())

        # Remove
        for name in old_names - new_names:
            st = target_state.pop(name)
            st["server"].close()
            await st["server"].wait_closed()
            while not st["queue"].empty():
                _, w = st["queue"].get_nowait()
                w.close()
            log.info("Removed: %s (was :%d)", name, st["listen_port"])

        # Add
        for name in new_names - old_names:
            t = new_by_name[name]
            queue: asyncio.Queue[tuple] = asyncio.Queue()

            async def _handle(r, w, _name=name):
                await handle_local_client(r, w, _name)

            listen_port = t["port"]
            try:
                srv = await asyncio.start_server(_handle, bind_host, listen_port)
            except OSError:
                srv = await asyncio.start_server(_handle, bind_host, 0)
                listen_port = srv.sockets[0].getsockname()[1]

            target_state[name] = {
                "server": srv, "listen_port": listen_port, "queue": queue,
                "target_host": t["host"], "target_port": t["port"],
            }
            log.info("Listening: %s on :%d -> %s:%d", name, listen_port, t["host"], t["port"])

        targets = new_targets
        if tui:
            tui.set_header(build_header())

    async def handle_local_client(reader, writer, target_name):
        peer = writer.get_extra_info("peername")
        log.info("Client connected (%s) from %s", target_name, peer)

        if ctrl_state["writer"] is None:
            log.warning("No agent, rejecting client (%s)", target_name)
            writer.close()
            return

        st = target_state.get(target_name)
        if not st:
            writer.close()
            return

        await st["queue"].put((reader, writer))
        idx = next(i for i, t in enumerate(targets) if t["name"] == target_name)
        try:
            ctrl_state["writer"].write(SIGNAL_NEW_CLIENT + struct.pack("B", idx))
            await ctrl_state["writer"].drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            log.error("Failed to signal agent (%s)", target_name)
            writer.close()

    async def handle_tunnel_conn(reader, writer):
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
            try:
                reg_data = await asyncio.wait_for(read_frame(reader), timeout=10)
                new_targets = json.loads(reg_data)
            except Exception as exc:
                log.error("Bad registration: %s", exc)
                writer.close()
                return

            # Validate: no duplicate names
            names = [t["name"] for t in new_targets]
            if len(names) != len(set(names)):
                log.error("Duplicate target names in registration")
                writer.close()
                return

            if ctrl_state["writer"] is not None:
                log.info("Replacing previous agent")
                try:
                    ctrl_state["writer"].close()
                except OSError:
                    pass

            ctrl_state["reader"] = reader
            ctrl_state["writer"] = writer
            log.info("Agent connected from %s", peer)

            await update_targets(new_targets)

            # Signal for any already-queued clients
            for idx, t in enumerate(targets):
                st = target_state.get(t["name"])
                if st:
                    for _ in range(st["queue"].qsize()):
                        writer.write(SIGNAL_NEW_CLIENT + struct.pack("B", idx))
                    await writer.drain()

            # Heartbeat
            async def _ping_loop():
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    try:
                        writer.write(SIGNAL_PING)
                        await writer.drain()
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return

            async def _read_loop():
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return

            ping_task = asyncio.create_task(_ping_loop())
            try:
                await _read_loop()
            except (ConnectionResetError, OSError):
                pass
            finally:
                ping_task.cancel()
                log.info("Agent disconnected")
                ctrl_state["reader"] = None
                ctrl_state["writer"] = None

        elif header == DATA_CHANNEL:
            try:
                idx = struct.unpack("B", await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            except Exception:
                writer.close()
                return

            if idx >= len(targets):
                log.warning("Invalid target index %d", idx)
                writer.close()
                return

            name = targets[idx]["name"]
            st = target_state.get(name)
            if not st:
                writer.close()
                return

            try:
                c_reader, c_writer = await asyncio.wait_for(st["queue"].get(), timeout=30)
            except asyncio.TimeoutError:
                log.warning("No pending client for %s", name)
                writer.close()
                return

            log.info("Bridging (%s)", name)
            await bridge(reader, writer, c_reader, c_writer, label=name)

    tunnel_server = await asyncio.start_server(handle_tunnel_conn, "127.0.0.1", funnel_port)
    log.info("Tunnel listener on 127.0.0.1:%d", funnel_port)

    if tui:
        tui.set_header(build_header())

    await tunnel_server.serve_forever()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def make_tls_context(no_verify: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if no_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def agent_connect(host, port, tls_ctx, server_hostname):
    if tls_ctx:
        return await asyncio.open_connection(host, port, ssl=tls_ctx, server_hostname=server_hostname)
    return await asyncio.open_connection(host, port)


async def run_agent(server_host: str, server_port: int, targets: list[dict],
                    token: str, use_tls: bool = True, no_verify: bool = False):
    tls_ctx = make_tls_context(no_verify) if use_tls else None
    hostname = server_host if use_tls else None
    reg_json = json.dumps(targets).encode()

    while True:
        try:
            log.info("Connecting to %s:%d ...", server_host, server_port)
            ctrl_r, ctrl_w = await agent_connect(server_host, server_port, tls_ctx, hostname)

            ctrl_w.write(CTRL_CHANNEL)
            write_token(ctrl_w, token)
            write_frame(ctrl_w, reg_json)
            await ctrl_w.drain()
            log.info("Registered %d target(s): %s",
                     len(targets), ", ".join(t["name"] for t in targets))

            async def _ping_loop():
                while True:
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    try:
                        ctrl_w.write(SIGNAL_PONG)
                        await ctrl_w.drain()
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return

            ping_task = asyncio.create_task(_ping_loop())
            try:
                while True:
                    sig = await ctrl_r.readexactly(1)
                    if sig == SIGNAL_NEW_CLIENT:
                        idx = struct.unpack("B", await ctrl_r.readexactly(1))[0]
                        if idx >= len(targets):
                            log.warning("Unknown target index %d", idx)
                            continue
                        t = targets[idx]
                        log.info("New client for %s (%s:%d)", t["name"], t["host"], t["port"])
                        asyncio.create_task(_agent_data_channel(
                            server_host, server_port, t["host"], t["port"], idx,
                            token, tls_ctx, hostname,
                        ))
                    elif sig == SIGNAL_PING:
                        ctrl_w.write(SIGNAL_PONG)
                        await ctrl_w.drain()
            finally:
                ping_task.cancel()
        except (ConnectionRefusedError, OSError, asyncio.IncompleteReadError) as exc:
            log.warning("Connection lost (%s), reconnecting in 3s ...", exc)
        finally:
            try:
                ctrl_w.close()
                await ctrl_w.wait_closed()
            except Exception:
                pass
        await asyncio.sleep(3)


async def _agent_data_channel(server_host, server_port, target_host, target_port,
                               target_idx, token, tls_ctx, hostname):
    try:
        relay_r, relay_w = await agent_connect(server_host, server_port, tls_ctx, hostname)
        relay_w.write(DATA_CHANNEL)
        write_token(relay_w, token)
        relay_w.write(struct.pack("B", target_idx))
        await relay_w.drain()

        target_r, target_w = await asyncio.open_connection(target_host, target_port)
        log.info("Bridging %s:%d", target_host, target_port)
        await bridge(relay_r, relay_w, target_r, target_w, label=f"{target_host}:{target_port}")
    except (ConnectionRefusedError, OSError) as exc:
        log.error("Data channel failed (%s:%d): %s", target_host, target_port, exc)


# ---------------------------------------------------------------------------
# Tailscale Funnel helpers
# ---------------------------------------------------------------------------

def get_tailscale_hostname() -> str:
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tailscale status failed: {result.stderr.strip()}")
    return json.loads(result.stdout).get("Self", {}).get("DNSName", "").rstrip(".")


def start_funnel(funnel_port: int) -> None:
    result = subprocess.run(
        ["tailscale", "funnel", "--bg", "--tls-terminated-tcp", "443", str(funnel_port)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to start Tailscale Funnel: {result.stderr.strip() or result.stdout.strip()}"
        )
    log.info("Tailscale Funnel active (:443 -> 127.0.0.1:%d)", funnel_port)


def stop_funnel() -> None:
    try:
        subprocess.run(
            ["tailscale", "funnel", "--tls-terminated-tcp=443", "off"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="TCP reverse tunnel over Tailscale Funnel")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_srv = sub.add_parser("server", help="Run on your machine")
    p_srv.add_argument("--port", type=int, default=9000, help="Funnel delivery port (default 9000)")
    p_srv.add_argument("--token", default=None, help="Auth token (auto-generated if omitted)")
    p_srv.add_argument("--bind", default="127.0.0.1", help="Bind address for local listeners (default 127.0.0.1)")

    p_ag = sub.add_parser("agent", help="Run in the remote environment")
    p_ag.add_argument("--server", required=True, help="Funnel hostname")
    p_ag.add_argument("--port", type=int, default=443, help="Funnel port (default 443)")
    p_ag.add_argument("--target", action="append", required=True, dest="targets",
                       help="name=host:port (repeatable)")
    p_ag.add_argument("--token", required=True, help="Auth token")
    p_ag.add_argument("--no-tls", action="store_true")
    p_ag.add_argument("--no-verify", action="store_true")

    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    if sys.platform != "win32":
        for sig_name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig_name, lambda: sys.exit(0))

    if args.mode == "server":
        token = args.token or secrets.token_hex(16)
        hostname = get_tailscale_hostname()
        start_funnel(args.port)

        tui = TuiDisplay()
        handler = TuiLogHandler(tui)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.propagate = False
        log.addHandler(handler)
        log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

        try:
            loop.run_until_complete(run_server(args.port, token, hostname, args.bind, tui))
        finally:
            stop_funnel()

    elif args.mode == "agent":
        targets = [parse_target(t) for t in args.targets]
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
        )
        loop.run_until_complete(
            run_agent(args.server, args.port, targets, args.token,
                      use_tls=not args.no_tls, no_verify=args.no_verify)
        )


if __name__ == "__main__":
    main()
