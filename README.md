# MCP Gatekeeper

Ask before any MCP tool call runs.

Claude uses MCP servers to do real things — read files, send mail, query a
database. Normally a tool call just happens. This sits in between, holds each
call, and asks you to allow or block it before it reaches the server.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the interception works and what
it can and can't cover.

## Development

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
```