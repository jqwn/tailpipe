# tailpipe

TCP reverse tunnel over Tailscale Funnel. Expose remote services (e.g. MSSQL, Postgres, Redis) to your local machine through a single outbound TLS connection. No port forwarding, no VPN, no third server.

## How it works

```
Remote environment                       Your machine
┌────────────────┐                       ┌────────────────┐
│                │  outbound TLS (443)   │                │
│  agent ────────┼──────────────────────►│  server        │
│    │           │  via Tailscale Funnel │    │           │
│    ├─► MSSQL   │                       │    ├─► :1433   │
│    ├─► Postgres│                       │    ├─► :5432   │
│    └─► Redis   │                       │    └─► :6379   │
│                │                       │                │
└────────────────┘                       └────────────────┘
                                              ▲
                                              │
                                         SQL client,
                                         SSMS, etc.
```

The **agent** runs in the remote environment (behind firewalls, NAT, etc.) and makes a single outbound TLS connection to your Tailscale Funnel URL. It registers the services it can reach. The **server** runs on your machine and dynamically opens local ports for each registered service.

All connections are outbound from the agent — nothing needs to be opened on the remote firewall.

## Prerequisites

- [Tailscale](https://tailscale.com/) installed on your machine with [Funnel](https://tailscale.com/kb/1223/funnel) enabled
- Python 3.10+ on both machines (no dependencies — stdlib only)

## Quick start

**On your machine** (server):

```bash
python tailpipe.py server
```

This automatically:
1. Starts Tailscale Funnel (TLS-terminated TCP on port 443)
2. Generates an auth token
3. Shows the agent command to run on the remote machine

**On the remote machine** (agent) — copy the printed command and add your targets:

```bash
python tailpipe.py agent --server your-machine.tailXXXX.ts.net --token <token> \
  --target mssql=localhost:1433
```

Or with `uv` (no install needed):

```bash
uv run https://raw.githubusercontent.com/jqwn/tailpipe/main/tailpipe.py agent \
  --server your-machine.tailXXXX.ts.net --token <token> \
  --target mssql=localhost:1433
```

**Connect** your SQL client to `localhost:1433` as usual.

### Multiple services

Tunnel as many services as you need through the same connection:

```bash
python tailpipe.py agent --server your-machine.tailXXXX.ts.net --token <token> \
  --target mssql=localhost:1433 \
  --target postgres=dbserver:5432 \
  --target redis=localhost:6379
```

The server dynamically opens a local port for each target. Need to add another service later? Just restart the agent with the extra `--target` — the server stays running.

### Target names are optional

If you don't provide a name, the port number is used:

```bash
--target localhost:1433          # name: "1433"
--target mssql=localhost:1433    # name: "mssql"
```

## Usage

### Server

```
python tailpipe.py server [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `9000` | Internal port Funnel delivers traffic to |
| `--token` | auto | Shared auth token (auto-generated if omitted) |
| `--bind` | `127.0.0.1` | Bind address for local listeners |
| `-v` | | Verbose logging |

The server has no port configuration — local ports are allocated automatically when the agent registers targets. It tries to match the target's port number (e.g. remote `:1433` → local `:1433`). If that port is in use, the OS picks an available one.

The terminal shows a sticky display with the current port mappings and agent command, plus a scrolling log of recent activity.

### Agent

```
python tailpipe.py agent [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--server` | required | Tailscale Funnel hostname |
| `--token` | required | Shared auth token (from server output) |
| `--target` | required | `[name=]host:port` — repeatable |
| `--port` | `443` | Funnel port |
| `--no-tls` | | Disable TLS (local testing only) |
| `--no-verify` | | Skip TLS certificate verification |
| `-v` | | Verbose logging |

## Features

- **Single file, zero dependencies** — stdlib Python only, runs anywhere
- **Multi-port** — tunnel multiple services through one connection
- **Agent-driven** — agent declares targets, server allocates ports dynamically
- **Auto Funnel management** — server starts/stops Tailscale Funnel automatically
- **Auto port allocation** — server picks the best local port for each target
- **Sticky TUI** — server terminal always shows current mappings + scrolling log
- **Heartbeat** — detects stale connections within ~15s and auto-reconnects
- **Auto-reconnect** — agent reconnects automatically if the connection drops
- **Token auth** — shared secret prevents unauthorized tunnel use
- **Multi-client** — each client connection gets its own data channel

## Protocol

1. Agent opens a persistent **control channel** (TLS) to the server via Funnel
2. Agent sends a **registration frame** listing its available targets
3. Server opens local listeners for each registered target
4. When a local client connects, the server sends a **new-client signal** (with target index) over the control channel
5. Agent opens a new **data channel** (TLS), connects it to the correct target service
6. Server bridges the local client to the data channel
7. Both sides exchange **heartbeat** pings every 15s to detect dead connections
