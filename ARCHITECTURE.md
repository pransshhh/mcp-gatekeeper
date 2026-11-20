# Architecture

## Approach

Claude Desktop and Claude Web reach MCP servers in different ways, so there are two entry points that share one decision point.

- **Claude Desktop** runs each MCP server as a child process over stdio. `mcp-gatekeeper install` patches `claude_desktop_config.json` (with a backup) so Desktop starts an **interceptor**, which starts the real server and sits in between.
- **Claude Web** calls connectors from Anthropic's servers, so nothing on your machine sees that traffic. Instead a **gateway** (streamable HTTP) sits in front of your MCP server, exposed through a tunnel and added as a custom connector in claude.ai.

Both ask one background **daemon** for a verdict. The daemon owns the queue, the rules, the audit log and the popup.

```
 Claude Desktop                         Claude Web
      │ stdio                               │ https → tunnel
      ▼                                     ▼
 interceptor (one per server)            gateway
      │                                     │
      ▼                                     ▼
 real MCP server                     remote MCP server

      └──────────── "may I run this?" ──────┘
                          ▼
          daemon: queue · rules · audit log · popup
```

I started with each interceptor opening its own popup, but Desktop runs one process per server, so that means N windows, N logs and no shared rules. Keeping interceptors dumb and putting all state and UI in one daemon fixed that.

## What gets gated

- **Gated:** `tools/call`, `resources/read`, and server → client requests (`sampling/createMessage`, `elicitation/create`). A server asking Claude to run an inference for it is easy to miss if you only watch one direction.
- **Not gated:** the handshake, `ping`, and the `*/list` discovery calls. Gating these breaks startup or floods the user with popups.

## The popup

Always on top. It shows the server, the tool and the arguments, with **Allow** and **Block**. Arguments are shown as plain text and truncated, since they can contain prompt injection or huge padding meant to push Block off screen. Several pending requests are shown one at a time ("2 of 3") instead of stacked windows.

There are also optional "remember" choices (this session / always), so the user isn't asked the same thing over and over.

## Allow and block

- **Allow:** the original bytes are forwarded unchanged, so what the user saw is exactly what runs.
- **Block:** the server never receives the call. Claude gets back a result with `isError: true` and a "Blocked by MCP Gatekeeper" message. The spec reserves JSON-RPC errors for protocol faults, and with this the model can just tell the user what happened.

## Concurrency

If the interceptor waited for a verdict inside its read loop, one open popup would freeze that server, pings included. So it uses asyncio: one reader per direction, and each gated request runs in its own task. Pending requests are keyed by `(direction, server, id)`, because two servers can both use `id: 1`.

Tk needs the main thread, so the daemon's asyncio loop runs in a worker thread and talks to the UI through thread-safe queues.

## Failures and edge cases

The rule is **fail closed**.

| Situation | What happens |
|---|---|
| No answer within 45s | Block (kept below the client's ~60s timeout so Claude doesn't give up first) |
| Client cancels the request | Close its popup, forward nothing |
| Daemon unreachable | Block and log |
| Real server crashes | Error out every pending request, exit cleanly |
| Desktop quits | Detect stdin EOF, stop the child server, no orphans |
| Junk or huge lines on stdout | Drop and log to stderr; line size is capped |

The interceptor never writes to stdout, because a stray `print` corrupts the JSON-RPC stream. A test checks this.

## Security

- The daemon socket/pipe is accessible only to the current user, since anything that can connect can fake verdicts.
- The gateway requires a bearer token and checks `Origin`, because the tunnel URL is public.
- The audit log (`~/.mcp-gatekeeper/audit.jsonl`, mode 0600) stores a hash and short preview of params, with secrets redacted.

## Limitations

- On Claude Web, only custom connectors pointed at the gateway can be intercepted. First-party connectors can't.
- This is a consent layer, not a sandbox. Anything that can edit Desktop's config can remove it.
- "Always allow" is not argument-aware yet, so always allowing `read_file` allows any path.