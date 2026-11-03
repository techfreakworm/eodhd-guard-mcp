# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below 1.0, a minor release can change tool names, parameters and output fields.

## [Unreleased]

## [0.2.0] - 2026-11-04

News, news sentiment and the economic-events calendar, and the fixes from an independent code review.

### Added

- Three read-only tools, which makes 17:
  - `get_news`: financial news for 1 to 5 tickers or one topic tag, with headlines, excerpts (or about 4,000 characters of text with `include_content`), tickers, tags and sentiment scores. It costs 5 API calls for each ticker, and 5 for a topic.
  - `get_sentiment`: daily news-sentiment scores for 1 to 10 tickers, with the number of articles behind each day, a `low_count` mark and an average weighted by articles. It costs 5 API calls for each ticker.
  - `get_economic_events`: economic releases and events in a window of up to 90 days, sorted by time in UTC, with actual, estimate and previous values. It costs 1 API call whatever the window: the window is fetched once (up to EODHD's 1,000 rows), and the filter by event name and the pages are applied here.
- Three warning codes: `SENTIMENT_UNRELIABLE` (EODHD's sentiment scores were often wrong in checks), `LOW_ARTICLE_COUNT` (a day that rests on fewer than three articles) and `SCORES_DROPPED` (sentiment values that were not numbers in range, set to null).
- The new endpoints in the cost table that `get_usage` shows.
- Cleaning of third-party text: Windows line endings, no-break spaces and invisible characters are taken out of article text, and titles, excerpts and tags are cut at a word. Every answer says that this text is data, never instructions. Sentiment scores are checked to be numbers in range.

### Fixed

- End-of-day answers whose range reaches the newest sessions are cached until the next bar is due, between 5 minutes and 6 hours (1 hour for `check_symbol`), and not for a flat 6 hours, so a bar that has just been published is not hidden.
- A call never accepts a cached answer that is older than its own lifetime, even when another call stored it for longer. A cached "not found" is kept for the endpoint's lifetime, at most 24 hours.
- Identical requests made at the same time share one call: the first pays, and the others are served its answer as a cache hit. This applies only while the cache is on.
- The cache key includes the server address and a one-way fingerprint of the API key, so an answer fetched for one server or key is not served to another. Entries written by 0.1.0 are not reused and are fetched again once.
- An existing state folder and its database files are set to mode 700 and 600 on Linux and macOS. Only newly created ones were before.
- The 150 KB cap on `get_fundamentals` is measured on the indented JSON that is sent, which is what the model reads. It used to count compact JSON, so an answer could be larger than the cap. With the cache off, the tool description and the notes say that every read costs 10 calls.
- Text from the data vendor (names, holders, currencies, tickers) is cut to 200 characters, and a description to 500. The server instructions tell the model to treat such text as data, never as instructions.
- `check_symbol` no longer reads holiday filler bars as signs of life. Its advice names no company: names and ISINs are in `name`, `isin` and `successor_candidates`, and when the delisted list holds several companies under one code they are listed in a new `clashing_names` field.
- An end-of-day range whose bar is not due yet, such as today's session before it is published, returns an empty list with `EMPTY_RESULT`, `latest_due` and `next_bar_due`, and not a `NO_DATA` error.
- `STALE_DATA` is judged against the last session that the range can hold, so a complete range that ended days ago is not reported as behind.
- `get_dividends` warns about the currency of a London line only when a row names one. With none, a note says that the currency is unknown. The warnings no longer state the quote currency of the line, because EODHD does not give it, and point to `list_exchange_symbols`.
- A result that has been paid for is kept when the budget check after it fails (a locked ledger, say). Only the `BUDGET_LOW` warning is lost. Closing the server cancels requests that are still in flight.
- Values EODHD sends in forms that cannot be read (integers too large for a number, a timestamp beyond the year 9999) are treated as missing and no longer fail the call. Log redaction also covers handlers that print a traceback from the exception itself.

## [0.1.0] - 2026-11-03

The first release, on [PyPI](https://pypi.org/project/eodhd-guard-mcp/0.1.0/) and in the MCP registry as `io.github.techfreakworm/eodhd-guard-mcp`.

### Added

- Fourteen read-only tools: `get_eod_prices`, `get_intraday_prices`, `get_quote`, `get_fundamentals`, `get_earnings_calendar`, `get_dividends`, `get_dividend_calendar`, `get_splits`, `get_split_calendar`, `list_exchanges`, `list_exchange_symbols`, `search_symbols`, `check_symbol` and `get_usage`.
- A common response envelope: `data`, `warnings` with stable codes, and `meta` with the cost, the cache state, when EODHD produced the data, and notes. Failures are errors with stable codes and hints.
- Argument validation before any call, because EODHD ignores bad parameters: strict `YYYY-MM-DD` dates (a number is never read as a date), `CODE.EXCHANGE` symbols, range and size limits. The price tools clamp a future end date and say so; the calendars look forward and are limited to 90 days instead.
- Guards for empty and placeholder answers: HTTP 200 with `[]`, `"NA"` quote fields or the wrong shape becomes an error or an explicit warning.
- Detection of frozen, delisted and renamed codes (`STALE_DATA`, `NO_QUOTE`, `POSSIBLY_INACTIVE`).
- A coverage registry for Tokyo and the UAE. Requests for those markets are refused with `NOT_COVERED` before they cost anything, and the calendars warn about them. `EODHD_GUARD_ALLOW_UNCOVERED` switches the pre-check off.
- End-of-day prices (`get_eod_prices`):
  - a `price_basis` choice of raw, adjusted or both, with a note that says which columns are which;
  - removal of holiday filler bars, with the staleness check judging the newest bar that is not a filler bar;
  - weekly and monthly bars kept when their period overlaps the range;
  - warnings for the known spin-off back-adjustments (Novartis, Unilever and Reliance), judged on the first bar returned, and pence-quoting notes for London lines;
  - a `NO_DATA` error for an empty answer to a range with trading days, which states what the call cost.
- Intraday prices (`get_intraday_prices`):
  - `end` defaults to now, rounded up to the next quarter hour, so that the 15-minute cache can hit;
  - a date-only `start` means 00:00 UTC, a date-only `end` means the end of that UTC day (23:59:59, inclusive), and a date-time `end` is exact;
  - `RANGE_CLAMPED` for an end in the future, and `INTRADAY_NOT_SAME_DAY` when the window reaches today's session and EODHD has no bar for it yet.
- Quotes (`get_quote`): symbols as a list or a comma-separated string, `data` as a list of entries in request order, a status for each symbol (`ok`, `no_quote`, `missing` or `not_covered`), and a cost of 1 for each symbol sent. `DELAYED_QUOTE` is always present.
- Fundamentals (`get_fundamentals`):
  - the whole document is fetched once for 10 calls and cut here, so further reads for 24 hours are free;
  - trimmed output capped at about 150 KB, with the newest `periods` kept in each statement, in `Earnings` and in `outstandingShares`;
  - `sections=[]` for statements only, `Financials` refused with a hint to use `statements`, and requested items that EODHD's document lacks listed in `data.not_in_document`;
  - warnings for currency mismatches, unreliable filing dates, an inconsistent dividend yield and the analyst basis of EPS, and a note that `Earnings.History` includes the next unreported quarter.
- Calendars, dividends and splits:
  - the earnings and split calendars are served from one whole-market request that is filtered locally by `symbols` and `exchange`; an empty `symbols` list means no filter;
  - `get_splits` sends no dates to EODHD and filters locally;
  - on London lines, dividends in another currency than GBX give `CURRENCY_MISMATCH` and dividends in GBX give `CURRENCY_NOTE`;
  - `RANGE_TRIMMED` marks rows outside the window, `surprise` and `surprise_pct` are null unless both the actual and the estimate exist, and a 403 names the plans that include the endpoint.
- Reference tools: `list_exchanges` always carries `PARTIAL_COVERAGE`; `list_exchange_symbols` filters and pages one cached list locally (`max_rows` 1 to 500, with `list_size`, `total`, `offset`, `count` and `next_offset`); `search_symbols` sets `possibly_inactive` when the previous close is old or missing, and warns with `TRUNCATED` when EODHD returns more rows than asked for.
- `check_symbol`, which costs 0 to 5 calls: the last 14 days of bars, an optional quote, and for a dead line up to three calls (a search by code limited to the exchange, then the exchange's delisted list if search no longer returns the line, then a search by name). The name never comes from another exchange's row, candidates that share the ISIN's country come first, and a failed optional step keeps the verdict.
- `get_usage`: the local ledger, the budget, EODHD's account counters (never the holder's details) and the cost table. When the account call fails, a missing key included, the ledger and budget are still returned, with `account: null` and an `account_error`.
- Cost control: a per-endpoint cost table, the cost on every response, a local SQLite usage ledger that keeps 90 days, and a daily budget per GMT day (default 1000) that also counts calls in flight, with `BUDGET_EXCEEDED` and `BUDGET_LOW`.
- A local SQLite response cache with a lifetime for each endpoint. `EODHD_GUARD_CACHE=off` turns it off.
- Key handling: the key is read from `EODHD_API_KEY` or `EODHD_API_KEY_FILE`, and is never a tool argument, never logged, never cached and never returned. `EODHD_GUARD_BASE_URL` must be `https` unless the host is loopback, and redirects are never followed. The configured log level is passed to the MCP SDK, which would otherwise log at INFO, and log redaction is installed after the SDK has set up its handlers. Logs and errors are redacted, and the account tool returns counters only.
- Retries with backoff for 429, 5xx and network errors.
- Command line and transports: stdio by default and streamable HTTP on `127.0.0.1`, with `--check`, `--check --online` and `--version`, and `python -m eodhd_guard_mcp`.
- Short instructions that the server sends to the model when it connects.
- Support for Python 3.10 to 3.13 on Linux, macOS and Windows.
- Tests: about 2,070 tests with more than 99% coverage, none of which calls EODHD. Contract tests replay real answers from a local, git-ignored cache, and a live check makes about six calls when `EODHD_GUARD_LIVE=1` is set.
- Documentation: README, `docs/DESIGN.md` and `docs/caveats.md`, with configuration examples for Claude Code and Claude Desktop.
- Project files: contributing guide with a synthetic-fixtures-only policy, security policy, code of conduct, issue and pull request templates.
- Tooling: CI (lint, type check, tests on three operating systems, package build, secret scan), Dependabot, pre-commit hooks and a gitleaks configuration.
- Release automation: after CI passes on `main`, a workflow builds the package and, when the version is new, publishes it to PyPI, tags a GitHub release and lists the server in the MCP registry (`server.json`). It does nothing until the repository variable `RELEASE_ENABLED` is `true`, and a script and a test check that `server.json` and the package carry the same version.

[Unreleased]: https://github.com/techfreakworm/eodhd-guard-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/techfreakworm/eodhd-guard-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/techfreakworm/eodhd-guard-mcp/releases/tag/v0.1.0
