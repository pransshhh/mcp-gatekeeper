# MCP Gatekeeper

Ask before any MCP tool call runs.

Claude uses MCP servers to do real things like read files, send mail, query a
database. Normally Claude decides to call a tool and the call just happens.
This sits in the middle of that conversation, freezes each call, and asks you
to allow or block it before it reaches the server.

Blocking means the call is never executed, not that its result is hidden:

```
13:03:05  delete_file  allow  by=user  3947ms     -> server ran it
12:59:56  delete_file  block  by=user  4599ms     -> server never saw it
```

## Demo



## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Tk, for the popup. Already present in most Python installs; on Debian or
Ubuntu it is `sudo apt install python3-tk` if missing.
- Claude Desktop, for the real thing. Not needed to run the tests or the
built-in demo.

## Install

```bash
git clone https://github.com/pransshhh/mcp-gatekeeper && cd mcp-gatekeeper
uv sync
```

That gives you a `mcp-gatekeeper` command inside the project environment:

```bash
uv run mcp-gatekeeper --help
```

## Try it without Claude Desktop

There is a fake client and a fake server in `tests/fakes/`, so you can see the
whole thing work before touching your real config. The fake server offers
`read_file`, `delete_file`, `send_email` and a `slow_task` that sleeps, and it
logs `!! <tool> actually ran` to stderr when a call genuinely executes and
that marker is how you prove a block really blocked.

In one terminal, start the daemon:

```bash
uv run python -m mcp_gatekeeper.daemon --socket /tmp/gk.sock --timeout 60
```

In another, run the fake client through the interceptor:

```bash
uv run python tests/fakes/client.py -- \
  uv run python -m mcp_gatekeeper.interceptor --server-name demo --socket /tmp/gk.sock -- \
  uv run python tests/fakes/server.py
```

Four popups appear in turn. Click Allow on some and Block on others, and watch
the transcript: allowed calls come back with results, blocked ones come back as
`BLOCKED/ERROR`. The last two arrive together, so you can see two decisions
outstanding at once.

To watch it without clicking anything, add `--auto block` or `--auto allow` to
the daemon. That skips the popup entirely and answers everything the same way,
which is also what the test suite uses.

## Use it with Claude Desktop

### 1. Have an MCP server configured

Gatekeeper wraps servers that already exist in Claude Desktop's config, so
there has to be at least one. If you have none and want to try the demo server,
add it under `mcpServers` in `claude_desktop_config.json`:

```json
"demo": {
  "command": "/absolute/path/to/mcp-gatekeeper/.venv/bin/python",
  "args": ["/absolute/path/to/mcp-gatekeeper/tests/fakes/server.py"]
}
```

Run `uv run mcp-gatekeeper doctor` if you are not sure where that file is since it
prints every location it knows about and marks the one that exists. The
documented path is `%APPDATA%\Claude\`, but recent Windows builds keep it under
`%LOCALAPPDATA%\Claude-Data\<install-id>\` instead.

### 2. Install the interceptor

```bash
uv run mcp-gatekeeper install --socket /tmp/gk.sock
```

This rewrites each server's command so Desktop launches the interceptor with
the original command after a `--`. It writes a backup first, and it is
idempotent, so running it twice is harmless.

```bash
uv run mcp-gatekeeper status --socket /tmp/gk.sock
```

### 3. Start the daemon

```bash
uv run python -m mcp_gatekeeper.daemon --socket /tmp/gk.sock
```

Pass the same `--socket` you gave `install`. Start it **before** Claude
Desktop, or the first tool call fails closed with "could not reach the
approval daemon", which is correct behaviour, just not what you want on a
first run.

Keep this in a terminal that is not inside Claude Desktop. Quitting Desktop
would otherwise take the daemon with it.

### 4. Restart Claude Desktop

Fully quit it — tray icon, Quit. Closing the window is not enough.

Then ask Claude to use one of the tools. A popup should appear.

### Removing it

```bash
uv run mcp-gatekeeper uninstall
```

That restores every wrapped entry to exactly what it was. There is also a
`claude_desktop_config.json.gatekeeper-backup` next to the config, which is the
file as it stood before Gatekeeper first touched it.

### A note on WSL

Claude Desktop is a Windows process, so the command it launches has to be
something Windows can start. If your config is under `/mnt` and you are running
inside WSL, `install` detects that and writes
`wsl.exe -d <distro> -- <linux python> -m mcp_gatekeeper.interceptor ...`, which
keeps the interceptor and the real MCP server both on the Linux side.

## Configuration

Three files, all under `~/.mcp-gatekeeper/`:

| File | Who writes it |
|---|---|
| `policy.yaml` | you, by hand. Nothing in the app ever rewrites it, so comments survive |
| `remembered.json` | the app, when you choose "Always" in the popup |
| `audit.jsonl` | the app, one line per decision |

Write a starter rules file with:

```bash
uv run mcp-gatekeeper init
```

Rules let you skip the popup for things you have already decided about:

```yaml
default: ask

rules:
  - server: "*"
    tool: "delete_*"
    action: ask

  - server: "demo"
    tool: "read_file"
    action: allow
```

`action` is `allow`, `block` or `ask`. Quote every wildcard — a bare `*` starts
an alias in YAML and will not parse.

An `allow` or `block` written here is final: it overrides anything you later
click "Always" on, so tightening this file afterwards does what you expect.

Recent decisions:

```bash
uv run mcp-gatekeeper log -n 20
```

### Flags worth knowing

On the daemon (`python -m mcp_gatekeeper.daemon`):

| Flag | Default | What it does |
|---|---|---|
| `--timeout` | 45 | Seconds to wait for a click. Kept below the client's own ~60s timeout so Claude does not give up first |
| `--on-timeout` | `block` | What an unanswered request becomes |
| `--auto allow\|block` | off | Answer everything without a popup. For tests and CI |
| `--policy`, `--remembered`, `--audit` | under `~/.mcp-gatekeeper/` | Override where each file lives |

On the interceptor, which you normally set through `install` rather than by
hand:

| Flag | Default | What it does |
|---|---|---|
| `--server-name` | `server` | Label for this server in the popup and the log |
| `--on-daemon-unavailable` | `block` | What to do when the daemon is down. `allow` exists and is a bad idea |
| `--denial-mode` | `tool_error` | `tool_error` returns a result with `isError`, which the model reads and explains. `jsonrpc_error` returns a hard error instead |

`--socket` applies to both, and to `install`. All three must agree.

## How it works

Claude Desktop launches whatever command each `mcpServers` entry names, and
talks JSON-RPC to it over stdio. Installing rewrites that command to point at
the interceptor, which launches the real server and passes every frame through.
Frames needing a decision are held while one background daemon is asked; the
daemon owns the queue, the rules, the audit log and the single popup.

`ARCHITECTURE.md` has the reasoning, including why a proxy rather than packet
sniffing, and what the design deliberately does not cover.

## Claude Web

Not implemented yet.

Claude Web has no local-server mechanism at all — its only option is a remote
connector, and those are fetched by Anthropic's servers rather than by your
machine. Claude says so explicitly if you try:

> Claude connects from Anthropic's servers, so it can't reach a local address
> like this one. Enter a public https:// URL.

So covering Claude Web means running an HTTP gateway locally and exposing it
through a tunnel (`cloudflared`, `ngrok`) as a custom connector. The same is
true of Desktop's URL connectors — only its local stdio servers can be
intercepted without a tunnel, and those are what this currently handles.

Anthropic's own first-party connectors (Drive, Gmail and so on) cannot be
intercepted by anything running on your machine, with or without a tunnel.

## Limitations

- Claude Web is not covered yet, as above.
- The daemon runs in a terminal. There is no tray icon or service mode, so
closing that terminal stops the gatekeeper and the next tool call fails
closed.
- "Always allow" is scoped to a server and tool, not to arguments. Always
allowing `read_file` therefore permits reading any path, which is why "just
this once" is the default choice in the popup.
- This is a consent layer, not a sandbox. Its trust anchor is Claude Desktop's
config file, so anything able to edit that file can remove the interceptor.
It does not defend against local malware and does not claim to.
- Batches containing a gated request are refused as a whole. Batching existed
only in the `2025-03-26` revision, so this is a safety net rather than a
feature.

## Development

```bash
uv run ruff format . && uv run ruff check . && uv run basedpyright && uv run pytest
```

Those four are the gate; everything should be clean before a commit. The Tk
test skips itself when `$DISPLAY` is unset, so the suite runs headless.
