# EODHD data caveats

These are the quirks in EODHD's answers that an AI assistant cannot see for itself, and what eodhd-guard-mcp does about each one.

They come from an evaluation of the EODHD API in October 2026 (about 930 calls) and a follow-up check in November 2026. EODHD changes its data and its API over time, so any item here may no longer hold. Where a section gives a lag, a date or a market, it describes what was observed then.

This page quotes no EODHD prices, ratios or other data values. It lists symbols, the dates of public corporate events, and the shape of answers. If you see EODHD behave differently from what is written here, open an issue with the endpoint, the date and the shape of the answer. Do not paste data.

The server cannot make EODHD's data better. It makes sure the model, and you, know what the data is.

## At a glance

| Caveat | Warning or error codes | Tools |
|---|---|---|
| [Coverage gaps](#coverage-gaps) | `NOT_COVERED`, `PARTIAL_COVERAGE` | all price, quote, fundamentals, dividend and split tools; earnings calendar; `search_symbols`; `list_exchanges` |
| [Frozen or renamed codes](#frozen-or-renamed-codes) | `STALE_DATA`, `NO_DATA`, `POSSIBLY_INACTIVE` | `get_eod_prices`, `check_symbol`, `search_symbols` |
| [NA quotes](#na-quotes) | `NO_QUOTE`, `STALE_QUOTE`, `NO_DATA` | `get_quote`, `check_symbol` |
| [Ignored parameters](#ignored-parameters) | `INVALID_PARAMS`, `RANGE_CLAMPED`, `RANGE_TRIMMED` | all |
| [Raw and adjusted prices](#raw-and-adjusted-prices) | `SPINOFF_ADJUSTED_CLOSE`, `ODD_SPLIT_RATIO` | `get_eod_prices`, `get_splits` |
| [Two meanings of NSE close](#two-meanings-of-nse-close) | none | `get_eod_prices` |
| [Holiday filler bars](#holiday-filler-bars) | `FILLER_BARS_REMOVED` | `get_eod_prices` |
| [Publication lag](#publication-lag) | `STALE_DATA` | `get_eod_prices`, `check_symbol` |
| [Same-day intraday bars and delayed quotes](#same-day-intraday-bars-and-delayed-quotes) | `INTRADAY_NOT_SAME_DAY`, `NO_INTRADAY_DATA`, `DELAYED_QUOTE` | `get_intraday_prices`, `get_quote` |
| [Fundamentals](#fundamentals) | `FILING_DATE_UNRELIABLE`, `DIVIDEND_YIELD_INCONSISTENT`, `EPS_BASIS`, `CURRENCY_MISMATCH`, `CURRENCY_NOTE` | `get_fundamentals`, `get_dividends`, `get_eod_prices`, `get_quote` |
| [Calendars](#calendars) | `CALENDAR_TIMING_UNRELIABLE`, `NO_CALENDAR_ROWS`, `PARTIAL_COVERAGE`, `TRUNCATED` | `get_earnings_calendar`, `get_dividend_calendar`, `get_split_calendar` |
| [Delisted names and search](#delisted-names-and-search) | `POSSIBLY_INACTIVE`, `EMPTY_RESULT` | `search_symbols`, `list_exchange_symbols` |
| [Holiday lists](#holiday-lists) | none | `get_eod_prices`, `check_symbol` |

## Coverage gaps

**What we saw.** In October 2026 two markets had no usable data.

- **Tokyo.** There were no prices, fundamentals, dividends or symbol lists. Every Tokyo suffix we tried (`TSE`, `T`, `JP`, `JPX`, `XTKS`, `TYO`) answered HTTP 404. The whole-market earnings calendar still carried `.TSE` rows, with dates only.
- **The UAE.** Dubai (DFM) and Abu Dhabi (ADX) had no prices, fundamentals or dates. The exchange code `AE` exists, but its symbol list is empty, and a request for a symbol on it (`EMAAR.AE`) answered HTTP 200 with zero rows.

A 404 is at least visible. An HTTP 200 with zero rows looks like a quiet period.

**What the server does.**

- A request for a symbol on one of these markets is stopped before it is sent. The tool fails with `NOT_COVERED` and no API call is spent. The message says what is missing and when it was checked, and names alternatives. For Tokyo, those are J-Quants (JPX's own API) or a US-listed ADR such as `TM.US` for Toyota. For the UAE, they are DFM's own historical export or a vendor with DFM and ADX coverage.
- `check_symbol` reports the status `not_covered`. `get_quote` reports it per symbol.
- `get_earnings_calendar` still returns Tokyo rows, with `PARTIAL_COVERAGE` to say they are dates only. UAE symbols get a not-covered warning there.
- `list_exchanges` and `search_symbols` flag rows on these exchanges with `PARTIAL_COVERAGE`.
- If EODHD adds coverage, set `EODHD_GUARD_ALLOW_UNCOVERED=1` to switch the pre-check off, and open an issue so the registry can be updated.
- `T` is a Tokyo alias, but `TO` (Toronto) and `TA` (Tel Aviv) are different markets and are not blocked.

## Frozen or renamed codes

**What we saw.** EODHD keeps answering for a code after the security stops trading under it. Roche's Swiss line changed its code from `ROG.SW` to `ROP.SW` in March 2026. In October 2026 `ROG.SW` still answered HTTP 200 with data that had stopped updating. Nothing in the answer, such as a flag, an error or an empty list, says the code is dead. A model that asks for recent prices gets a plausible series that stops short of today, and no sign of a problem.

**What the server does.**

- `get_eod_prices` compares the last bar with the latest session the market should have published by now. When the gap is `EODHD_GUARD_STALE_SESSIONS` sessions or more (default 3), it adds `STALE_DATA`, names both dates and points to `check_symbol`. The check runs on daily bars when `end` is within 7 days of today, so a deliberately historical query is not flagged.
- An empty answer for a range that contains session days is `NO_DATA`, never a quiet empty success.
- `check_symbol` fetches the last 14 days of bars. When the status is not ok, it searches by code and by company name, and lists candidates on the same exchange that have a recent previous close and a different code. Their ISINs are included so you can confirm it is the same company.
- `search_symbols` marks lines whose previous close is old (`possibly_inactive`, `POSSIBLY_INACTIVE`).
- `get_quote` flags the same condition with `NO_QUOTE` or `STALE_QUOTE` (see below).

The successor candidates are a lead, not a verdict. They come from a name search, so a company that changed its name as well as its code may not appear.

## NA quotes

**What we saw.** `real-time` for an unknown, delisted or renamed code answers HTTP 200 and sets every field, `timestamp` and `close` included, to the string `"NA"`. There is no error status. A client that checks only the status code treats it as a good quote, and a model may read `"NA"` as missing data without learning that the code is the problem.

**What the server does.**

- Every requested symbol gets its own entry in the answer. A symbol whose `timestamp` or `close` is `"NA"` or null gets the status `no_quote` and a `NO_QUOTE` warning. The warning says the code may be unknown, delisted or renamed, and suggests `check_symbol`.
- If no requested symbol has a usable quote, the tool fails with `NO_DATA` and lists the status of each symbol.
- `STALE_QUOTE` is added when a quote's date is `EODHD_GUARD_STALE_SESSIONS` sessions or more behind.
- Because the answer is HTTP 200, the local usage ledger counts it like any other call.

## Ignored parameters

**What we saw.** EODHD does not reject bad parameters. It ignores them.

- An invalid `from` date is ignored, so the whole history comes back instead of a window.
- An unknown `period` falls back to daily.
- A symbol without an exchange suffix means the US exchange. A Swiss or British code sent without its suffix is looked up as a US code.

None of these produces an error, so a typo changes the meaning of a request with no signal.

**What the server does.** It validates before it calls. Every error names the field, the value it received and what it accepts.

- Dates must be real `YYYY-MM-DD` dates. The intraday tool also accepts ISO 8601 timestamps.
- Choices such as `period`, `interval`, `price_basis` and `type` must be one of the listed values.
- A symbol must be `CODE.EXCHANGE`. A bare code fails with an explanation that EODHD would have used the US exchange, and examples: `AAPL.US`, `VOD.LSE`, `NESN.SW`, `RELIANCE.NSE`.
- A start after the end, or a span beyond the tool's limit, is rejected.
- An `end` in the future is clamped to today (`RANGE_CLAMPED`). Rows that EODHD returns outside the requested range are dropped (`RANGE_TRIMMED`).

The server validates only the parameters of the endpoints it wraps.

## Raw and adjusted prices

**What we saw.** The `close` column is not adjusted for splits or dividends, and `adjusted_close` is adjusted for both. That much is the usual convention.

The surprise is that `close` is back-adjusted for some spin-offs and demergers. For those events EODHD folds the spin-off into `close`, so the history before the event date does not match the prices at which the shares changed hands, and the earlier `close` values sit below the traded price. We saw this around:

| Line | Event | Date |
|---|---|---|
| Novartis, `NOVN.SW` | Alcon spin-off | 9 April 2019 |
| Novartis, `NOVN.SW` | Sandoz spin-off | 4 October 2023 |
| Unilever, `ULVR.LSE` | Demerger of The Magnum Ice Cream Company | 8 December 2025 |
| Reliance Industries, `RELIANCE.NSE` | Demerger of Jio Financial Services | 20 July 2023 |

How large the difference is depends on the value of the spun-off business relative to the parent. Someone who treats `close` as the traded price gets the wrong level before the event date. Someone who uses `adjusted_close` for returns gets splits and dividends handled, which is its purpose.

**What the server does.**

- `get_eod_prices` takes `price_basis`. `both` (the default) returns `close` and `adjusted_close` side by side, `raw` returns only the reported columns, and `adjusted` scales open, high, low and close by `adjusted_close / close`.
- Every EOD answer carries a note in `meta.notes` that says which columns are which. For `both` and `raw` it says that open, high, low and close are as EODHD reports them, may differ from the traded price, and that `adjusted_close` is the one to use for returns.
- When the requested range starts before one of the events in the table, the answer carries `SPINOFF_ADJUSTED_CLOSE` and names the event and its date.
- `get_splits` flags ratios that look like spin-off or rights adjustments rather than splits (`ODD_SPLIT_RATIO`).

The list of known events is short and fixed in the code. A spin-off that is not on it is not flagged. CONTRIBUTING.md explains how to add one.

## Two meanings of NSE close

**What we saw.** In data dated before August 2026, EODHD's `close` for NSE names meant one of two things. For some names it was the last traded price. For others it was the exchange's official closing price, which NSE computes as the volume-weighted average price over the last 30 minutes of trading. The two prices can differ, and nothing in the answer says which one a given bar holds.

**What the server does.** Nothing special. There is no NSE-specific guard. `get_eod_prices` returns `close` as EODHD reports it and says so in `meta.notes`. If you compare an NSE close from before August 2026 with another source, a small difference does not by itself mean either source is wrong. For official closing values, use NSE's own data.

## Holiday filler bars

**What we saw.** On days when an exchange was closed, EODHD can return a bar anyway: volume zero, and open, high, low and close all equal. It looks like a trading day on which nothing moved.

**What the server does.**

- `get_eod_prices` drops these bars by default and adds `FILLER_BARS_REMOVED`.
- `include_filler_bars=true` keeps them.
- The rule is exactly "volume is zero and open, high, low and close are equal". It cannot tell a holiday from a genuine day without a trade in a very thinly traded security. If you need every row, set `include_filler_bars=true`.

## Publication lag

**What we saw.** End-of-day bars are not there when the market closes. In October 2026 the day's bar appeared this long after the close:

| Market | Time after the close |
|---|---|
| US | 10 to 30 minutes |
| London | 1 to 2 hours |
| Switzerland | 2 to 4 hours |
| NSE (India) | 2.5 to 4.5 hours |

Other markets were not measured.

**What the server does.**

- To decide when a bar should exist, the server uses an allowance per market: US 30 minutes, London 2 hours, Switzerland 4 hours and NSE 5 hours. Each is at or a little above the observed range, so asking soon after the close does not look like missing data. Other markets use rough estimates that the code marks as unmeasured, and an unknown exchange gets 6 hours.
- `STALE_DATA` needs `EODHD_GUARD_STALE_SESSIONS` sessions of lag (default 3), so ordinary publication lag never triggers it.
- A missing bar for today is normal and produces no warning.

## Same-day intraday bars and delayed quotes

**What we saw.**

- Intraday bars are published after the session. They are not available while the session is open, and today's bars appear only after the close plus a lag.
- Quotes from `real-time` are 15 to 20 minutes delayed.

**What the server does.**

- `get_intraday_prices` adds `INTRADAY_NOT_SAME_DAY` when the window includes today, the session is open or has closed today, and no bar falls on today's local date. The message says bars are published after the session and suggests `get_quote`.
- A window with no bars at all returns an empty list and `NO_INTRADAY_DATA`. How far back intraday history goes varies by exchange and plan.
- `get_quote` always adds `DELAYED_QUOTE`, and returns `as_of`, `age_seconds` and `delayed: true` for each symbol so the age of the number is explicit.
- An intraday window that includes today is cached for 15 minutes, and a quote for 60 seconds.

## Fundamentals

**What we saw.**

- **Arrival lag.** New quarters and years usually appear 1 to 4 days after the company's release, so a missing latest period in the days after results is normal.
- **`filing_date` is not always a filing date.** For NSE names it equals the period end. Treating it as the publication date, in a point-in-time backtest for example, would use data before it existed.
- **A dividend yield of 0 next to a forward rate.** In some documents `Highlights.DividendYield` is 0 while a forward annual dividend rate is set. A model that reads only the yield concludes the stock pays nothing.
- **EPS on the analyst basis.** EPS actuals in `Earnings` follow the analyst (adjusted) basis, which is not necessarily the figure in the reported statement.
- **Quote currency and statement currency can differ.** A London line is quoted in pence (GBX), while its statements can be in US dollars or another currency. Combining a price in pence with a statement figure in pounds or dollars, in a ratio such as P/E or a yield, is wrong by roughly a factor of 100.
- **Size.** One request returns the whole document, which is large, and costs 10 API calls.

**What the server does.**

- The first `get_fundamentals` call for a symbol costs 10. The server caches the whole document for 24 hours, so reading further sections or statements for that symbol costs nothing in that time.
- It trims the output: it drops officer lists, addresses and logos, cuts long descriptions, limits holders and insider transactions, and keeps the newest `periods` of each statement. The result is capped at about 150 KB, and `TRUNCATED` says when something was cut.
- A note gives the typical 1 to 4 day arrival lag, together with `General.UpdatedAt`.
- `FILING_DATE_UNRELIABLE` appears when `filing_date` equals the period end in any returned period.
- `DIVIDEND_YIELD_INCONSISTENT` appears when the yield is 0 and a forward dividend rate is set.
- `EPS_BASIS` appears whenever `Earnings` is returned.
- `CURRENCY_MISMATCH` appears when a statement's currency differs from `General.CurrencyCode`. `get_dividends` raises it when dividends are paid in a currency other than the quote's.
- `get_eod_prices`, `get_quote` and `get_dividends` add `CURRENCY_NOTE` for London lines, which are usually quoted in GBX.

## Calendars

**What we saw.**

- **Before and after the market.** The flags that say whether a company reports before the open or after the close were often wrong in our checks.
- **Missing rows.** Some issuers have no row in a window in which they report.
- **A short default window.** With no dates, the earnings calendar covers about 7 days.
- **Cost.** A whole-market request costs 1 API call whatever the window. A request per symbol costs 1 each, and returns nothing for `.TSE` codes.
- **Dividend calendar rows have no amounts.**

**What the server does.**

- `get_earnings_calendar` always sends an explicit window. `start` defaults to today and `end` to 14 days later. A window is at most 90 days, and `symbols` holds at most 100.
- It makes one whole-market request, which is cached for 6 hours, and filters by `symbols` and `exchange` locally. When more rows match than `max_rows`, `TRUNCATED` reports the total, and `offset` pages through the rest.
- `CALENDAR_TIMING_UNRELIABLE` is always present on earnings results.
- `NO_CALENDAR_ROWS` lists requested symbols that have no row in the window. A missing row is not evidence that the company does not report.
- `.TSE` rows come back with `PARTIAL_COVERAGE`, because Tokyo has dates only.
- `get_dividend_calendar` returns the symbol and date, and its notes point to `get_dividends` for amounts.

## Delisted names and search

**What we saw.** Search returns live lines. A company that has been delisted does not appear, so an empty result can mean a typo or a delisted company. Delisted lines are available through the exchange symbol list with `delisted=1`. For the US that list has about 60,000 rows and is about 8.5 MB.

**What the server does.**

- `search_symbols` notes that delisted lines are not searchable and points to `list_exchange_symbols(delisted=true)`. An empty result is reported as `EMPTY_RESULT`.
- Search results carry the previous close and its date, and a line whose previous close is old is marked `possibly_inactive`.
- `list_exchange_symbols` fetches the list once per exchange and flag (1 API call), caches it for 24 hours, and filters and pages it locally, so the large list never goes into the model's context. `TRUNCATED` reports the total.

## Holiday lists

**What we saw.** EODHD's exchange holiday lists cover the current year only. They cannot tell you whether a date in a past year was a trading day, so they are no help in deciding whether an old series is stale.

**What the server does.** It does not use them. Its market calendar knows weekends and trading hours, but not holidays. Two consequences follow.

- A long closure can trigger a false `STALE_DATA`. The default threshold of 3 sessions absorbs ordinary closures. If your markets close for longer, raise `EODHD_GUARD_STALE_SESSIONS` or run `check_symbol`.
- A short range that falls entirely on a weekday holiday can come back as `NO_DATA`, because the server cannot tell it from a delisted code. `check_symbol` settles the doubt.
