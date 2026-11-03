# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below 1.0, a minor release can change tool names, parameters and output fields.

## [Unreleased]

The first release, planned as 0.1.0.

### Added

- Fourteen read-only tools: `get_eod_prices`, `get_intraday_prices`, `get_quote`, `get_fundamentals`, `get_earnings_calendar`, `get_dividends`, `get_dividend_calendar`, `get_splits`, `get_split_calendar`, `list_exchanges`, `list_exchange_symbols`, `search_symbols`, `check_symbol` and `get_usage`.
- A common response envelope: `data`, `warnings` with stable codes, and `meta` with the cost, the cache state, when EODHD produced the data, and notes. Failures are errors with stable codes and hints.
- Argument validation before any call, because EODHD ignores bad parameters: strict `YYYY-MM-DD` dates, `CODE.EXCHANGE` symbols, range and size limits, and clamping of future end dates.
- Guards for empty and placeholder answers: HTTP 200 with `[]`, `"NA"` quote fields or the wrong shape becomes an error or an explicit warning.
- Detection of frozen, delisted and renamed codes (`STALE_DATA`, `NO_QUOTE`, `POSSIBLY_INACTIVE`), and `check_symbol`, which also looks for likely successor codes.
- A coverage registry for Tokyo and the UAE. Requests for those markets are refused with `NOT_COVERED` before they cost anything. `EODHD_GUARD_ALLOW_UNCOVERED` switches the pre-check off.
- Price semantics: a `price_basis` choice of raw, adjusted or both, removal of holiday filler bars, warnings for known spin-off back-adjustments (Novartis, Unilever and Reliance), pence-quoting notes for London lines, and a check for split ratios that look like spin-offs.
- Timing guards: quotes are always marked as delayed, same-day intraday bars are flagged as not yet available, and a per-market model of publication lag decides when a bar should exist.
- Trimmed fundamentals, capped at about 150 KB, with warnings for currency mismatches, unreliable filing dates, an inconsistent dividend yield and the analyst basis of EPS.
- Calendars served from one whole-market request that is filtered locally, with warnings for unreliable timing flags and missing rows.
- Cost control: a per-endpoint cost table, the cost on every response, a local SQLite usage ledger that keeps 90 days, a daily budget per GMT day (default 1000) with `BUDGET_EXCEEDED` and `BUDGET_LOW`, and the `get_usage` tool.
- A local SQLite response cache with a lifetime for each endpoint. `EODHD_GUARD_CACHE=off` turns it off.
- Key handling: the key is read from `EODHD_API_KEY` or `EODHD_API_KEY_FILE`, and is never a tool argument, never logged, never cached and never returned. Logs and errors are redacted, and the account tool returns counters only.
- Retries with backoff for 429, 5xx and network errors.
- Command line and transports: stdio by default and streamable HTTP on `127.0.0.1`, with `--check`, `--check --online` and `--version`, and `python -m eodhd_guard_mcp`.
- Short instructions that the server sends to the model when it connects.
- Support for Python 3.10 to 3.13 on Linux, macOS and Windows.
- Documentation: README, `docs/DESIGN.md` and `docs/caveats.md`, with configuration examples for Claude Code and Claude Desktop.
- Project files: contributing guide with a synthetic-fixtures-only policy, security policy, code of conduct, issue and pull request templates.
- Tooling: CI (lint, type check, tests on three operating systems, package build, secret scan), Dependabot, pre-commit hooks and a gitleaks configuration.

[Unreleased]: https://github.com/techfreakworm/eodhd-guard-mcp/commits/main
