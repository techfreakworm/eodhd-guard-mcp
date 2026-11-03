# eodhd-guard-mcp

<!-- mcp-name: io.github.techfreakworm/eodhd-guard-mcp -->

A guard-first [MCP](https://modelcontextprotocol.io) server for the [EODHD](https://eodhd.com) market-data API: it catches silent failures, says what each price is, and counts what every call costs.

[![CI](https://github.com/techfreakworm/eodhd-guard-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/techfreakworm/eodhd-guard-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

**Status: alpha (0.1.x).** Tool names, parameters and output fields can still change before 1.0.

**Unofficial.** This project is not affiliated with or endorsed by EODHD or Unicorn Data Services. You need your own EODHD subscription, and EODHD's data licence applies to everything you fetch. See the [data licence notice](#data-licence-notice).

## Why

EODHD is a broad, inexpensive market-data API. An evaluation in October 2026 (about 930 calls) found its data sound for the US, London and Swiss markets, and good as a cross-check for India. It also found failures that an AI assistant cannot see, because the API reports them as success:

- **Frozen codes.** A delisted or renamed code keeps answering HTTP 200 with frozen data. Roche's Swiss line changed from `ROG.SW` to `ROP.SW` in March 2026, and the old code kept answering.
- **Empty and placeholder answers.** A real-time quote for an unknown code is HTTP 200 with every field set to `"NA"`. Tokyo and the UAE have no prices and answer with `[]` or a bare 404.
- **Ignored parameters.** A bad `from` date is ignored and the whole history comes back. An unknown `period` falls back to daily. A symbol without an exchange suffix silently means the US.
- **Prices that are not what they look like.** `close` is not adjusted for splits or dividends, yet it is back-adjusted for some spin-offs and demergers. `adjusted_close` is the column to use for returns.
- **Lateness.** Quotes are 15 to 20 minutes delayed. Intraday bars for the current session are not available. End-of-day bars appear anywhere from 10 minutes (US) to 4.5 hours (India) after the close.
- **Cost.** Every endpoint has a price in API calls (end-of-day 1, intraday 5, fundamentals 10), so an unattended agent can spend a day's allowance in minutes.

This server has 14 tools, and every one has guards. It validates arguments before sending anything, turns empty and placeholder answers into errors or explicit warnings, says whether each price is raw or adjusted and how old it is, states the cost of every response, counts every call in a local ledger, and stops at a daily budget. Each caveat is explained in [docs/caveats.md](docs/caveats.md).

## Requirements

- Python 3.10 or later.
- Your own EODHD subscription and API key. This project does not include or resell data.
- An MCP client that can start a stdio server or connect to a streamable-HTTP one, such as Claude Code or Claude Desktop.

### EODHD plan for each tool

EODHD refuses requests that your plan does not include (HTTP 403). The server reports that as `PLAN_ERROR`, and the message names the plans that include the endpoint.

| Tools | EODHD plan needed |
|---|---|
| `get_eod_prices`, `get_dividends`, `get_splits`, `search_symbols`, `list_exchanges`, `list_exchange_symbols`, `check_symbol` | "EOD Historical Data" or higher |
| `get_intraday_prices` | "EOD+Intraday" or "All-in-One" |
| `get_fundamentals`, `get_earnings_calendar`, `get_dividend_calendar`, `get_split_calendar` | "Fundamentals Data Feed" or "All-in-One" |
| `get_quote` | Live (delayed) prices: check EODHD's plan table for your plan |
| `get_usage` | Any plan; the account endpoint is free |

`check_symbol` also needs the plan that covers `get_quote` when you pass `include_quote`. The free EODHD plan allows 20 calls a day and one year of end-of-day history; if you use it, set `EODHD_GUARD_DAILY_BUDGET=20`. EODHD changes its plans from time to time, so check eodhd.com for the current table.

## Data licence notice

EODHD's data is licensed for personal, non-commercial use, and EODHD's terms do not allow redistributing it. That was the position at the time of the October 2026 evaluation; read the current terms on eodhd.com before you use output for anything beyond your own analysis.

- This project ships no EODHD data. Its tests use synthetic fixtures.
- You are responsible for complying with EODHD's terms for everything you fetch, store and share.
- The local cache holds EODHD responses. Do not share it, sync it to a shared folder or commit it. See [Cache and state files](#cache-and-state-files).
- Nothing here is legal advice.

## Unofficial

eodhd-guard-mcp is an independent project. It is not affiliated with, endorsed by or supported by EODHD or Unicorn Data Services. "EODHD" and "EOD Historical Data" are names of their respective owners, used here only to say which API the server talks to.

## Install

The package is not on PyPI yet. Install it from source:

```bash
pip install git+https://github.com/techfreakworm/eodhd-guard-mcp
```

Or in an isolated environment, with pipx or uv:

```bash
pipx install git+https://github.com/techfreakworm/eodhd-guard-mcp
uvx --from git+https://github.com/techfreakworm/eodhd-guard-mcp eodhd-guard-mcp --version
```

Or from a clone:

```bash
git clone https://github.com/techfreakworm/eodhd-guard-mcp.git
cd eodhd-guard-mcp
pip install -e .
```

Check the install and your configuration:

```bash
eodhd-guard-mcp --version
EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" eodhd-guard-mcp --check
```

`--check` prints the configuration with the key shown only as set or not set, the state directory and the budget. Add `--online` to make one free call to EODHD's account endpoint and print only its usage counters. The exit code is non-zero when something is wrong.

## Configure

The server needs your API key. Pass it as `EODHD_API_KEY_FILE`, the path of a file that holds only the key, or as `EODHD_API_KEY`. Prefer the file. An inline key lands in your shell history and in your MCP client's configuration, in plain text.

To create the key file without putting the key in your shell history:

```bash
mkdir -p ~/.config/eodhd-guard-mcp
(umask 077; touch ~/.config/eodhd-guard-mcp/api_key)
${EDITOR:-nano} ~/.config/eodhd-guard-mcp/api_key
```

### Claude Code

```bash
claude mcp add eodhd-guard --env EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" -- eodhd-guard-mcp
```

With the key inline, which is easier but leaves it in your shell history and in Claude Code's configuration file:

```bash
claude mcp add eodhd-guard --env EODHD_API_KEY=... -- eodhd-guard-mcp
```

[examples/claude_code.md](examples/claude_code.md) has the full walk-through, including `uvx`, user scope and a lower budget.

### Claude Desktop

Add the server to `claude_desktop_config.json` and restart Claude Desktop. The file is at `~/Library/Application Support/Claude/` on macOS and `%APPDATA%\Claude\` on Windows.

```json
{
  "mcpServers": {
    "eodhd-guard": {
      "command": "eodhd-guard-mcp",
      "args": [],
      "env": {
        "EODHD_API_KEY_FILE": "/Users/you/.config/eodhd-guard-mcp/api_key"
      }
    }
  }
}
```

Claude Desktop does not always see the `PATH` of your shell. If it cannot find the command, put the absolute path from `which eodhd-guard-mcp` (macOS and Linux) or `where eodhd-guard-mcp` (Windows) in `command`. On Windows, write backslashes twice inside the JSON. Or let `uvx` fetch and run it:

```json
{
  "mcpServers": {
    "eodhd-guard": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/techfreakworm/eodhd-guard-mcp", "eodhd-guard-mcp"],
      "env": {
        "EODHD_API_KEY_FILE": "/Users/you/.config/eodhd-guard-mcp/api_key"
      }
    }
  }
}
```

The same file is in [examples/claude_desktop_config.json](examples/claude_desktop_config.json).

### Any other stdio client

Start `eodhd-guard-mcp` with no arguments and set `EODHD_API_KEY_FILE` in its environment. `python -m eodhd_guard_mcp` does the same. Logs go to stderr, because stdout carries the protocol.

### Streamable HTTP

```bash
eodhd-guard-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The server answers at `http://127.0.0.1:8000/mcp`. **This transport has no authentication.** Anyone who can reach the port can spend your EODHD calls, so keep the host on `127.0.0.1`. The server prints a warning to stderr to remind you.

### Command line

| Option | Meaning |
|---|---|
| `--transport stdio\|streamable-http` | How to serve MCP. The default is stdio. |
| `--host`, `--port` | For streamable HTTP. Defaults: `127.0.0.1` and `8000`. |
| `--check` | Print the configuration, the state directory and the budget, and exit. |
| `--check --online` | Also make the free account call and print its counters. |
| `--version` | Print the version. |

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `EODHD_API_KEY` | API key | none |
| `EODHD_API_KEY_FILE` | File holding the key (used when `EODHD_API_KEY` is unset) | none |
| `EODHD_GUARD_DAILY_BUDGET` | Cost units per GMT day, 0 = unlimited | `1000` |
| `EODHD_GUARD_STATE_DIR` | Ledger and cache folder | `$XDG_STATE_HOME/eodhd-guard-mcp`, else `~/.local/state/eodhd-guard-mcp`; `%LOCALAPPDATA%\eodhd-guard-mcp` on Windows |
| `EODHD_GUARD_CACHE` | `on` or `off` (also 1/0, true/false, yes/no) | `on` |
| `EODHD_GUARD_STALE_SESSIONS` | Sessions behind before `STALE_DATA`; an integer, 1 or more | `3` |
| `EODHD_GUARD_TIMEOUT` | Request timeout in seconds, above 0 | `30` |
| `EODHD_GUARD_MAX_RETRIES` | Retries for 429, 5xx and network errors; an integer, 0 to 5 | `2` |
| `EODHD_GUARD_ALLOW_UNCOVERED` | Skip the coverage pre-check for Tokyo and the UAE | `off` |
| `EODHD_GUARD_BASE_URL` | API base, for tests and proxies | `https://eodhd.com/api` |
| `EODHD_GUARD_LOG_LEVEL` | `DEBUG` to `CRITICAL` | `WARNING` |

An invalid value stops the server at start-up with a `CONFIG_ERROR` message that names the variable and the values it accepts. A key file that cannot be read, or is empty, does the same. A key that is simply not set does not stop the server: listing tools works, and each tool call fails with `CONFIG_ERROR`. The key and the content of a key file are stripped of surrounding whitespace. `EODHD_GUARD_BASE_URL` must be an `https` URL (plain `http` is allowed only for localhost), with no credentials, query or fragment.

## What a response looks like

Every tool returns the same envelope:

- `data`: the result.
- `warnings`: a list of `{code, message, details}`. Show every warning to the user. They are the point of this server.
- `meta`: `source`, `endpoints`, `cost` (API calls charged by this response), `cached`, `fetched_at` (when EODHD produced the data; the oldest time for a result built from several calls) and `notes`.

The server also gives the model short instructions when it connects: symbols are `CODE.EXCHANGE`, surface every warning, check `meta.cost` and call `get_usage` before bulk work, use `adjusted_close` for returns, quotes are delayed, Tokyo and the UAE are not covered, and call `check_symbol` when data looks stale.

This is the shape of an end-of-day answer. The company is fictional and the numbers are invented.

```json
{
  "data": {
    "symbol": "ACME.US",
    "period": "d",
    "price_basis": "both",
    "start": "2026-10-01",
    "end": "2026-10-02",
    "first_date": "2026-10-01",
    "last_date": "2026-10-02",
    "count": 2,
    "bars": [
      {"date": "2026-10-01", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "adjusted_close": 10.4, "volume": 1000},
      {"date": "2026-10-02", "open": 10.5, "high": 11.5, "low": 10.0, "close": 11.0, "adjusted_close": 10.9, "volume": 1200}
    ]
  },
  "warnings": [
    {"code": "SPINOFF_ADJUSTED_CLOSE", "message": "<what happened and what to do about it>", "details": null}
  ],
  "meta": {
    "source": "EODHD",
    "endpoints": ["eod"],
    "cost": 1,
    "cached": false,
    "fetched_at": "2026-10-03T21:00:00Z",
    "notes": ["open/high/low/close are as EODHD reports them: not adjusted for splits or dividends, but sometimes back-adjusted for spin-offs and demergers, so they may differ from the traded price. adjusted_close is adjusted for splits and dividends; use it for returns."]
  }
}
```

A failed call comes back as an MCP error result whose text starts with the error code, for example `NOT_COVERED: ...`, followed by a hint where one helps.

## Tools

All tools are read-only. Costs are in EODHD API calls. A cached answer costs 0.

| Tool | Returns | Cost | Key guard |
|---|---|---|---|
| [`get_eod_prices`](#get_eod_prices) | Daily, weekly or monthly bars, raw and adjusted | 1 | Flags frozen codes, drops holiday filler bars, labels the price basis, warns about spin-off back-adjustment |
| [`get_intraday_prices`](#get_intraday_prices) | 1-minute, 5-minute or 1-hour bars | 5 | Says when same-day bars cannot exist yet |
| [`get_quote`](#get_quote) | Delayed quotes for up to 20 symbols | 1 per symbol | Turns `"NA"` answers into `no_quote`; always flags the delay |
| [`get_fundamentals`](#get_fundamentals) | Trimmed sections and statements | 10 for the first read, then free from cache for 24 hours | Currency, filing-date, dividend-yield and EPS-basis warnings; output capped at about 150 KB |
| [`get_earnings_calendar`](#get_earnings_calendar) | Earnings dates in a window | 1 | One whole-market request, filtered locally; timing flags marked unreliable |
| [`get_dividends`](#get_dividends) | Dividend history | 1 | Currency check; split-adjusted and as-declared values labelled |
| [`get_dividend_calendar`](#get_dividend_calendar) | Symbols with a dividend dated on one day | 1 | Notes that the calendar has no amounts |
| [`get_splits`](#get_splits) | Split history | 1 | Flags ratios that look like spin-offs or rights adjustments |
| [`get_split_calendar`](#get_split_calendar) | Splits in a window | 1 | Filtered locally by symbols or exchange |
| [`list_exchanges`](#list_exchanges) | Exchanges with codes, countries and currencies | 1 | Flags exchanges with no data |
| [`list_exchange_symbols`](#list_exchange_symbols) | Symbols on one exchange, active or delisted | 1 | Filters and pages locally; an empty list is an error |
| [`search_symbols`](#search_symbols) | Symbols matching a name or code | 1 | Marks lines that may be inactive |
| [`check_symbol`](#check_symbol) | A verdict on one symbol, with likely successors | 1 to 4 (0 for an uncovered market) | Looks for a successor when a code looks dead |
| [`get_usage`](#get_usage) | Local ledger, budget and account counters | 0 | Never returns account holder details |

Each section below gives the signature with its defaults and an example of the arguments of a call. Symbols are always `CODE.EXCHANGE`, for example `AAPL.US`, `VOD.LSE`, `NESN.SW` or `RELIANCE.NSE`.

### get_eod_prices

`get_eod_prices(symbol, start=None, end=None, period="d", price_basis="both", include_filler_bars=False, max_rows=500)`

End-of-day bars for one symbol. Cost: 1. Cached for 6 hours when `end` is within a day of today, otherwise 24 hours.

- `end` defaults to today (UTC) and `start` to 30 days before `end`. `period` is `d`, `w` or `m`. `max_rows` is 1 to 5000, and the most recent rows are kept.
- `price_basis` is `both` (`close` and `adjusted_close`), `raw` or `adjusted`. Every answer says in `meta.notes` which is which.
- Guards: frozen or renamed codes (`STALE_DATA`), empty answers for a range that has trading days (`NO_DATA`), holiday filler bars (`FILLER_BARS_REMOVED`), rows outside the range (`RANGE_TRIMMED`), known spin-offs (`SPINOFF_ADJUSTED_CLOSE`) and pence quoting on London lines (`CURRENCY_NOTE`).
- Data: `{symbol, period, price_basis, start, end, first_date, last_date, count, bars}`.

```json
{"symbol": "AAPL.US", "start": "2026-10-01", "end": "2026-10-30", "price_basis": "adjusted"}
```

### get_intraday_prices

`get_intraday_prices(symbol, interval="5m", start=None, end=None, max_rows=500)`

Intraday bars. Cost: 5. Cached for 15 minutes when the window includes today, otherwise 24 hours.

- `interval` is `1m`, `5m` or `1h`. `start` and `end` take `YYYY-MM-DD` (00:00 UTC) or an ISO 8601 timestamp; a timestamp without an offset is UTC.
- `end` defaults to now. `start` defaults to one day before for `1m`, three days for `5m` and 14 days for `1h`. The longest span is 120, 600 and 7200 days respectively.
- Bars: `{datetime_utc, open, high, low, close, volume}`.
- Guards: `INTRADAY_NOT_SAME_DAY` when the window includes today and EODHD has no bar for it yet. A window with no bars at all is not an error: it returns an empty list and `NO_INTRADAY_DATA`.
- Needs a plan with intraday data.

```json
{"symbol": "AAPL.US", "interval": "5m", "start": "2026-10-28T13:30:00Z", "end": "2026-10-28T20:00:00Z"}
```

### get_quote

`get_quote(symbols)`

Delayed quotes. Cost: 1 per symbol, at most 20 symbols per call. Cached for 60 seconds.

- `symbols` is a list or a comma-separated string.
- Each symbol gets an entry: `{symbol, status: "ok", price, open, high, low, previous_close, change, change_pct, volume, as_of, age_seconds, delayed: true}`, or `{symbol, status: "no_quote" | "missing" | "not_covered", message}`.
- Guards: `"NA"` fields become `no_quote` with `NO_QUOTE`; `STALE_QUOTE` when a quote's date is several sessions behind; uncovered symbols are never sent; if no symbol has a quote, the call fails with `NO_DATA`. `DELAYED_QUOTE` is always present.

```json
{"symbols": ["AAPL.US", "VOD.LSE", "RELIANCE.NSE"]}
```

### get_fundamentals

`get_fundamentals(symbol, sections=None, statements=None, period="quarterly", periods=4)`

Company fundamentals, trimmed. Cost: 10 for the first request for a symbol. One request fetches the whole document, which is cached for 24 hours, so every later section or statement read for that symbol is free in that time.

- Default sections: for stocks General, Highlights, Valuation, SharesStats, SplitsDividends and Earnings; for ETFs General, Technicals and ETF_Data; for funds General and MutualFund_Data.
- `statements` is any of `income`, `balance` and `cashflow`, and is empty by default. `period` is `quarterly` or `yearly`. `periods` is 1 to 40.
- Output is trimmed (officers, addresses and logos dropped, long text cut, holders and insider transactions limited, statements cut to the newest `periods`) and capped at about 150 KB. `TRUNCATED` says when something was cut.
- Guards: `CURRENCY_MISMATCH`, `FILING_DATE_UNRELIABLE`, `DIVIDEND_YIELD_INCONSISTENT`, `EPS_BASIS`, and a note that new quarters usually arrive 1 to 4 days after the release.
- Needs "Fundamentals Data Feed" or "All-in-One".

```json
{"symbol": "NESN.SW", "sections": ["General", "Highlights"], "statements": ["income"], "period": "yearly", "periods": 3}
```

### get_earnings_calendar

`get_earnings_calendar(start=None, end=None, symbols=None, exchange=None, max_rows=200, offset=0)`

Earnings dates. Cost: 1 for any window. The tool makes one whole-market request (cached for 6 hours) and filters by `symbols` and `exchange` itself, because a request per symbol would cost 1 each.

- `start` defaults to today and `end` to `start` plus 14 days. The window is at most 90 days, and `symbols` holds at most 100.
- Rows are sorted by `report_date`, then symbol: `{symbol, report_date, period_end, timing, currency, eps_actual, eps_estimate, surprise, surprise_pct}`.
- Guards: `CALENDAR_TIMING_UNRELIABLE` (always present), `NO_CALENDAR_ROWS` for requested symbols with no row, `PARTIAL_COVERAGE` for Tokyo rows, which have dates only, and `TRUNCATED` with the total.

```json
{"start": "2026-11-03", "end": "2026-11-10", "symbols": ["AAPL.US", "MSFT.US"]}
```

### get_dividends

`get_dividends(symbol, start=None, end=None, max_rows=100)`

Dividend history. Cost: 1. Cached for 24 hours.

- Without `start` you get the full history, and the newest `max_rows` rows are kept.
- Rows: `{ex_date, declaration_date, record_date, payment_date, period, value, unadjusted_value, currency, upcoming}`. `value` is split-adjusted and `unadjusted_value` is as declared.
- Guards: `CURRENCY_MISMATCH` when the dividend currency differs from the quote currency (for example US dollar dividends on a pence-quoted London line). No dividends is `EMPTY_RESULT`, not an error.

```json
{"symbol": "VOD.LSE", "start": "2025-01-01"}
```

### get_dividend_calendar

`get_dividend_calendar(date, max_rows=200, offset=0)`

Symbols with a dividend dated on one day. Cost: 1. Cached for 6 hours.

- Rows: `{symbol, date}`. The calendar has no amounts, and a note points to `get_dividends`.

```json
{"date": "2026-11-05"}
```

### get_splits

`get_splits(symbol, start=None, end=None)`

Split history. Cost: 1. Cached for 24 hours.

- Rows: `{date, ratio, new_shares, old_shares, factor}`, parsed from EODHD's `"new/old"` text.
- Guard: `ODD_SPLIT_RATIO` when either side of the ratio is not a whole number, or when the factor lies between 0.8 and 1.25 and is not 1. These are usually spin-off or rights adjustments, not splits.

```json
{"symbol": "NVDA.US", "start": "2020-01-01"}
```

### get_split_calendar

`get_split_calendar(start=None, end=None, symbols=None, exchange=None, max_rows=200, offset=0)`

Splits in a window. Cost: 1. Cached for 6 hours.

- `start` defaults to today and `end` to `start` plus 30 days. The window is at most 90 days.
- Rows: `{symbol, split_date, old_shares, new_shares, factor, optionable}`, filtered here by `symbols` and `exchange`.

```json
{"start": "2026-11-03", "end": "2026-12-03", "exchange": "US"}
```

### list_exchanges

`list_exchanges(query=None)`

The exchanges EODHD lists. Cost: 1. Cached for 7 days.

- Rows: `{code, name, country, currency, operating_mic, country_iso2, country_iso3, coverage_note}`. `query` filters by code, name or country.
- Guard: `PARTIAL_COVERAGE` notes that Tokyo, DFM and ADX have no data and that `AE` is listed but empty.

```json
{"query": "india"}
```

### list_exchange_symbols

`list_exchange_symbols(exchange, query=None, type=None, delisted=False, max_rows=100, offset=0)`

Symbols on one exchange. Cost: 1. One list is cached for 24 hours per exchange and `delisted` flag, and filtering and paging happen locally, so a large list never goes into the model's context.

- Rows: `{symbol, code, name, type, currency, country, isin, venue}`, with `symbol` as `CODE.<requested exchange>`.
- `type` is matched case-insensitively against EODHD's type, with spaces as underscores: `common_stock`, `etf`, `fund`, `preferred_stock` and so on.
- `delisted=true` returns delisted lines, which search never finds. The US delisted list has about 60,000 rows.
- Guards: `NOT_COVERED` for Tokyo and the UAE, `NOT_FOUND` for an unknown exchange, `NO_DATA` for a list that is listed but empty (as `AE` is), and `TRUNCATED` with the total.

```json
{"exchange": "SW", "query": "nestle", "type": "common_stock", "max_rows": 10}
```

### search_symbols

`search_symbols(query, exchange=None, type="all", max_results=15)`

Look a company up by name or code. Cost: 1. Cached for 24 hours.

- `max_results` is 1 to 50. `type` is one of `all`, `stock`, `etf`, `fund`, `bonds`, `index` or `crypto`.
- Rows: `{symbol, code, exchange, name, type, country, currency, isin, is_primary, previous_close, previous_close_date, possibly_inactive}`.
- Guards: `POSSIBLY_INACTIVE` when a line's previous close is old, `PARTIAL_COVERAGE` for rows on exchanges with gaps, and `EMPTY_RESULT` for no match. A note says delisted lines are not searchable and points to `list_exchange_symbols`.

```json
{"query": "Roche", "exchange": "SW", "type": "stock"}
```

### check_symbol

`check_symbol(symbol, include_quote=False, find_successor=True)`

Is this code alive? Cost: 1 to 4 calls, and 0 for an uncovered market.

1. Coverage check. A gap returns `not_covered` and costs nothing.
2. The last 14 days of end-of-day bars. A 404 means `not_found`; an empty answer or stale data means `stale`.
3. With `include_quote`, a quote. `"NA"` means `no_quote`.
4. If the status is not ok and `find_successor` is true, two searches (by code, then by company name) list other codes on the same exchange with a recent previous close.

Data: `{symbol, status, last_bar_date, expected_latest_session, sessions_behind, quote_status, name, isin, successor_candidates, advice}`, where `status` is `ok`, `stale`, `not_found`, `no_quote` or `not_covered`. Candidates are a lead to confirm, not a verdict.

```json
{"symbol": "ROG.SW", "include_quote": true}
```

### get_usage

`get_usage(days=7, include_account=True)`

What this server has spent. Cost: 0.

- Data: `ledger` (calls, cost and cache hits per day and per endpoint, plus this session), `budget` (limit, spent today, remaining, reset time), `account` (EODHD's own counters) and `cost_table`.
- The account part comes from EODHD's free account endpoint and is limited to `api_requests_today`, `requests_date`, `daily_rate_limit`, `extra_limit`, `subscription_type` and `subscription_mode`. It never includes a name, email address, payment method or invite token.
- EODHD counts per GMT day. The account counter also includes other apps that share your key, while the ledger counts only this server.

```json
{"days": 7}
```

## Warnings and errors

### Warning codes

Warnings travel next to the data in `warnings`. They never replace an error, and they are not optional reading.

| Code | Raised by | Meaning |
|---|---|---|
| `STALE_DATA` | EOD, `check_symbol` | The last bar is `EODHD_GUARD_STALE_SESSIONS` or more sessions behind the expected latest session |
| `FILLER_BARS_REMOVED` | EOD | Zero-volume bars with open = high = low = close were dropped (holiday filler) |
| `RANGE_TRIMMED` | EOD, intraday | EODHD returned rows outside the requested range; they were dropped |
| `RANGE_CLAMPED` | EOD, intraday, calendars | An end date in the future was clamped to today |
| `TRUNCATED` | any list | More rows than `max_rows`; the most recent or first rows were kept |
| `SPINOFF_ADJUSTED_CLOSE` | EOD | The range starts before a known spin-off or demerger that EODHD folds into `close` |
| `CURRENCY_NOTE` | EOD, quote, dividends | London lines are usually quoted in GBX (pence) |
| `CURRENCY_MISMATCH` | fundamentals, dividends | Statements or dividends use another currency than the quote |
| `DELAYED_QUOTE` | quote | Quotes are 15 to 20 minutes delayed (always present) |
| `STALE_QUOTE` | quote | The quote's timestamp is far behind the expected latest session |
| `NO_QUOTE` | quote | EODHD answered with `"NA"` fields for a symbol |
| `INTRADAY_NOT_SAME_DAY` | intraday | The session is open or just closed, and EODHD has no bars for it yet |
| `NO_INTRADAY_DATA` | intraday | No bars in the window |
| `FILING_DATE_UNRELIABLE` | fundamentals | `filing_date` equals the period end, so it is not the publication date |
| `DIVIDEND_YIELD_INCONSISTENT` | fundamentals | `Highlights.DividendYield` is 0 while a forward dividend rate is set |
| `EPS_BASIS` | fundamentals | EPS actuals follow the analyst (adjusted) basis |
| `CALENDAR_TIMING_UNRELIABLE` | earnings | Before and after-market flags were often wrong in checks |
| `NO_CALENDAR_ROWS` | earnings | Requested symbols with no row in the window |
| `PARTIAL_COVERAGE` | earnings, search, exchanges | A market with dates only, or a listed-but-empty exchange |
| `POSSIBLY_INACTIVE` | search | A line's previous close is old: it may be delisted or renamed |
| `ODD_SPLIT_RATIO` | splits | A ratio that looks like a spin-off or rights adjustment, not a split |
| `EMPTY_RESULT` | lists | A legitimately empty answer, for example no dividends |
| `BUDGET_LOW` | any tool | 80% or more of the daily budget is spent |

### Error codes

A failed call is an MCP error result. Its text begins with the code, then a message that names the field or the cause, and a hint where one helps. Error messages never contain the API key.

| Code | When | Billed |
|---|---|---|
| `CONFIG_ERROR` | A setting is invalid, or no API key is set | No call made |
| `INVALID_PARAMS` | An argument is invalid. The message names the field, the value received and what is accepted | No call made |
| `NOT_COVERED` | The market is known to have no data (Tokyo, the UAE) | No call made |
| `NOT_FOUND` | EODHD answered 404, for example an unknown symbol or exchange | No |
| `NO_DATA` | HTTP 200, but nothing usable: an empty list for a range with trading days, only `"NA"` quotes, an empty document or symbol list | Yes, it was a 200 |
| `AUTH_ERROR` | 401: the key is missing, wrong or expired | No |
| `PLAN_ERROR` | 403: your subscription does not include this endpoint. The message names the plans that do | No |
| `QUOTA_EXCEEDED` | 402: your account's daily API limit is used up. It resets at 00:00 GMT | No |
| `RATE_LIMITED` | 429, after retries | No |
| `BUDGET_EXCEEDED` | This server's daily budget would be exceeded. Nothing was sent | No call made |
| `UPSTREAM_ERROR` | 5xx after retries, or any other 4xx status | No |
| `NETWORK_ERROR` | A network failure or timeout after retries. Only the exception class name is reported | No |
| `BAD_RESPONSE` | HTTP 200 with a body that is not JSON, or has the wrong shape | Yes, it was a 200 |

## Costs, budget and usage

EODHD charges in API calls, and each endpoint has its own price. The server knows the prices, reports the cost of every response in `meta.cost`, and counts every call in a local ledger.

| Endpoint | API calls |
|---|---|
| End-of-day prices | 1 |
| Intraday bars | 5 |
| Real-time quotes | 1 per symbol |
| Fundamentals | 10 (one request returns the whole document) |
| Earnings calendar | 1 for any window |
| Split calendar | 1 |
| Dividend calendar | 1 (assumed from EODHD's published price, not measured) |
| Dividend and split history | 1 each |
| Exchange list, exchange symbol list, search | 1 each |
| Account counters | 0 |

These prices were measured in October 2026. Only HTTP 200 answers are billed: 401, 402, 403 and 404 were measured as free. A cached answer costs 0.

**Daily budget.** `EODHD_GUARD_DAILY_BUDGET` sets a cap in API calls per GMT day. The default is 1000, and 0 means no cap. Before each call that is not served from the cache, the server checks that today's spend plus the call's cost stays within the cap. If it would not, the call is not made and the tool returns `BUDGET_EXCEEDED`, which names the budget, today's spend, the variable that raises it and the reset time. From 80% of the cap, every response also carries `BUDGET_LOW`. The budget is a guard of this server's own, separate from your EODHD plan's limit, so set it at or below that limit.

**The day.** EODHD's daily counter resets at 00:00 GMT, and the ledger uses GMT days too.

**`get_usage`.** Ask for it before bulk work. It shows today's spend, the remaining budget, the last days per endpoint, and EODHD's own counters. See [get_usage](#get_usage).

## Cache and state files

The server keeps two SQLite files in its state directory. The directory is `EODHD_GUARD_STATE_DIR` if set, else `$XDG_STATE_HOME/eodhd-guard-mcp`, else `~/.local/state/eodhd-guard-mcp`, and `%LOCALAPPDATA%\eodhd-guard-mcp` on Windows.

| File | Holds | Kept |
|---|---|---|
| `usage.sqlite` | One row per call: time, GMT day, endpoint, cost, HTTP status, cached or not, duration, size and symbol. Never the API key, URLs or response bodies | 90 days |
| `cache.sqlite` | EODHD responses, zlib-compressed. Only 200 and 404 answers | Per endpoint, see below |

| Data | Cached for |
|---|---|
| End-of-day prices | 6 hours when `end` is within a day of today, else 24 hours |
| Intraday bars | 15 minutes when the window includes today, else 24 hours |
| Quotes | 60 seconds |
| Fundamentals | 24 hours |
| Earnings, split and dividend calendars | 6 hours |
| Dividend and split history | 24 hours |
| Exchange list | 7 days |
| Exchange symbol lists and search | 24 hours |
| 404 answers | 24 hours |
| Account counters | Never |

`meta.cached` tells you whether an answer came from the cache, and `meta.fetched_at` carries the time EODHD produced it.

- **Turn the cache off** with `EODHD_GUARD_CACHE=off`.
- **Clear the cache** by stopping the server and deleting `cache.sqlite` (and any `cache.sqlite-wal` and `cache.sqlite-shm` files next to it).
- **Deleting `usage.sqlite` resets the local budget counter,** because the budget is computed from the ledger. EODHD's own counter is not affected.

**The cache holds licensed EODHD data.** Keep the state directory out of git repositories, shared folders and backups that others can read, and do not hand the files to anyone else. See the [data licence notice](#data-licence-notice).

## Security

- The key comes from `EODHD_API_KEY_FILE` or `EODHD_API_KEY`. It is never a tool argument, so the model cannot read it or set it.
- The key is sent only to the EODHD API, as the `api_token` query parameter EODHD expects. Only `EODHD_GUARD_BASE_URL`, which you set yourself, can change where it goes.
- The key is never logged. Log records pass through a redaction filter, and the HTTP client's request logging, which would print the URL, is turned off.
- The key is never cached, never stored in the ledger, and never returned in a tool result or an error message. Network errors report the exception class name only.
- `get_usage` returns counters, never the account holder's name, email address, payment method or invite token.
- The streamable-HTTP transport has no authentication. It binds to `127.0.0.1` by default.
- Names and descriptions in results come from EODHD. Treat them as data, not as instructions.

To report a vulnerability, follow [SECURITY.md](SECURITY.md). Do not open a public issue for it.

## Data caveats

[docs/caveats.md](docs/caveats.md) lists the quirks of EODHD's data that this server guards against, with what was observed in October and November 2026 and what the server does about each: coverage gaps, frozen codes, `"NA"` quotes, ignored parameters, raw and adjusted prices, spin-offs, NSE closes, filler bars, publication lag, intraday and quote delays, fundamentals, calendars, delisted names and holiday handling.

## Compared with EODHD's official MCP server

EODHD maintains its own MCP server, [EODHD-MCP-Server](https://github.com/EodHistoricalData/EODHD-MCP-Server), which is also hosted at mcp.eodhd.com. It is the broader tool, and the right choice when you need endpoints this project does not wrap. This project has no bulk downloads, screener, options, news or technical indicators, for example. It is a narrow complement for the cases where correctness and cost matter, and the two can run side by side.

The right-hand column comes from a check in November 2026. EODHD may have changed things since, so test the version you use.

| | eodhd-guard-mcp | EODHD's official MCP server |
|---|---|---|
| Maintained by | An independent project | EODHD |
| Scope | 14 tools | About 100 tools |
| Where it runs | On your machine, over stdio or localhost HTTP | Hosted at mcp.eodhd.com |
| Authentication | Your key, read from your environment | v1: the key in the URL or a header. v2: OAuth |
| Empty and `"NA"` answers | Become errors or explicit warnings | Passed through as successful results |
| Fundamentals | 10 API calls on the first read, output capped at about 150 KB | A call cost 80 API calls and returned about 1 MB |
| Account details | `get_usage` returns counters only | The account tool returned the account holder's details to the model |

Use the official server for breadth. Use this one for prices, quotes, fundamentals and calendars, where an empty or placeholder answer, a mislabelled price or a surprise bill would hurt. Neither replaces checking the numbers that matter against a second source.

## Development

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, running the tests, the fixture policy (synthetic data only, never real EODHD responses) and how to add a tool. The design of every module, tool and warning code is in [docs/DESIGN.md](docs/DESIGN.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md). Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy
```

## Licence

[MIT](LICENSE). The licence covers this code only. It does not cover EODHD's data, which has its own terms.
