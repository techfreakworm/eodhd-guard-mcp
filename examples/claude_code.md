# Using eodhd-guard-mcp with Claude Code

These steps register the server with [Claude Code](https://claude.com/claude-code) over stdio. You need your own EODHD subscription and API key. See the [README](../README.md) for the plan each tool needs.

## 1. Install

```bash
pipx install git+https://github.com/techfreakworm/eodhd-guard-mcp
eodhd-guard-mcp --version
```

The package is not on PyPI yet. `pip install git+https://github.com/techfreakworm/eodhd-guard-mcp` into a virtual environment works too. In that case, use the absolute path to the `eodhd-guard-mcp` script in step 3.

## 2. Keep the key in a file

Claude Code stores the environment variables you give it in its own configuration file, in plain text, and your shell keeps a history of what you type. Putting the key in a file and passing only the path avoids both.

```bash
mkdir -p ~/.config/eodhd-guard-mcp
(umask 077; touch ~/.config/eodhd-guard-mcp/api_key)
${EDITOR:-nano} ~/.config/eodhd-guard-mcp/api_key
```

Paste the key, save the file and close the editor. The file holds only the key. The server strips surrounding whitespace.

## 3. Register the server

```bash
claude mcp add eodhd-guard --scope user \
  --env EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" \
  -- eodhd-guard-mcp
```

`--scope user` makes the server available in every project. Leave it out to register it for the current project only. The `--` separates Claude Code's options from the server command.

To skip the install step, let `uvx` fetch and run it:

```bash
claude mcp add eodhd-guard --scope user \
  --env EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" \
  -- uvx --from git+https://github.com/techfreakworm/eodhd-guard-mcp eodhd-guard-mcp
```

On Windows, use an absolute path for the key file, for example `C:\Users\you\eodhd-guard-mcp\api_key`.

## 4. Check it

```bash
claude mcp list
```

`eodhd-guard` should show as connected. Inside a Claude Code session, `/mcp` shows the same status.

You can also check the configuration without Claude Code. This prints whether a key is set (never the key), the state directory and the budget:

```bash
EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" eodhd-guard-mcp --check
```

Add `--online` to make one free call to EODHD's account endpoint and print only the usage counters.

## 5. Try it

Ask in plain language:

- "Use eodhd-guard to check ROG.SW and tell me if the code is still active."
- "Get the last month of daily prices for VOD.LSE, and say which price basis you used."
- "How much of my EODHD budget have I used today?"

Every answer carries its cost in `meta.cost`, and any warnings come back next to the data. If the assistant leaves a warning out of its summary, ask it to list them.

## Limit what a session can spend

The server stops calling EODHD once the day's budget is used up. The default is 1,000 API calls per GMT day. Set it at or below your plan's daily limit:

```bash
claude mcp add eodhd-guard --scope user \
  --env EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" \
  --env EODHD_GUARD_DAILY_BUDGET=20 \
  -- eodhd-guard-mcp
```

The free EODHD plan allows 20 calls a day. A fundamentals request costs 10 and an intraday request costs 5, so a low budget goes quickly. Ask for `get_usage` to see the spend.

To change a setting later, remove the server and add it again:

```bash
claude mcp remove eodhd-guard
```

## Streamable HTTP instead of stdio

Run the server yourself and point Claude Code at it:

```bash
eodhd-guard-mcp --transport streamable-http --host 127.0.0.1 --port 8000
claude mcp add --transport http eodhd-guard http://127.0.0.1:8000/mcp
```

This transport has no authentication. Anyone who can reach the port can spend your EODHD calls, so keep the host on `127.0.0.1`.
