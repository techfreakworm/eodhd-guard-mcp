# eodhd-guard-mcp: design

This document is the contract between the modules. Read it before changing a public function, a warning
code or a tool. If code and this document disagree, fix one of them in the same change.

## Why this server exists

EODHD (eodhd.com) is a broad, inexpensive market-data API. An evaluation in October 2026 (about 930 calls)
found the data sound for the US, London, Switzerland and India. It also found failure modes that an AI
assistant cannot see:

- **Silent failures.** A delisted or renamed code keeps answering HTTP 200 with frozen data. A real-time
  quote for such a code comes back as HTTP 200 with every field set to `"NA"`. Unsupported markets return
  `[]` or a bare 404. An invalid `from` date is ignored, so the whole history comes back. An unknown
  `period` falls back to daily. A symbol without an exchange suffix silently means the US.
- **Prices that are not what they look like.** `close` is not adjusted for splits or dividends, but it *is*
  back-adjusted for some spin-offs and demergers. `adjusted_close` is adjusted for splits and dividends.
- **Lateness.** Quotes are 15 to 20 minutes delayed. Intraday bars are published after the session, never
  during it. End-of-day bars appear from about 30 minutes (US) to 5 hours (India) after the close.
- **Cost.** Each endpoint has a price in "API calls": EOD 1, intraday 5, fundamentals 10, and so on. A
  careless agent can spend a day's allowance in minutes.
- **Coverage gaps.** Tokyo and the UAE have no prices at all.

In a check in November 2026, EODHD's official MCP server passed these cases through as successful results.
That server covers far more of the API. This one is smaller, and it is built around catching them.

## Principles

1. **An empty or placeholder answer is not data.** HTTP 200 with `[]`, `"NA"` fields or the wrong shape
   becomes an error or an explicit warning, never a silent success.
2. **Say what each number is:** raw or adjusted, delayed or not, in which currency, and as of when.
3. **Every call has a price.** Each response carries its cost. Every call is counted in a local ledger, and
   a daily budget caps spending. Cached answers cost nothing.
4. **Small outputs by default.** Recent windows, trimmed sections, row caps and pagination. A tool never
   dumps a megabyte into the model's context.
5. **The API key goes nowhere but eodhd.com.** It is never a tool argument, never logged, never cached,
   never in an error message, and never returned.
6. **Validate before calling,** because the API ignores bad parameters instead of rejecting them.

## Layout

```
src/eodhd_guard_mcp/
  __init__.py        __version__
  __main__.py        python -m eodhd_guard_mcp
  cli.py             argument parsing, logging set-up, --check, run()
  server.py          build_deps(), build_server(), server instructions
  config.py          Settings.from_env()
  errors.py          ErrorCode, GuardError
  redact.py          redact(), RedactingFilter, install_log_redaction()
  costs.py           cost table, cost_for()
  ledger.py          UsageLedger (SQLite), Budget
  cache.py           ResponseCache (SQLite)
  client.py          EODHDClient, ApiResponse
  envelope.py        ToolWarning, Meta, ToolResponse, build_response()
  validation.py      Symbol, parse_symbol(), parse_symbols(), parse_date(), parse_datetime_utc(), resolve_range()
  coverage.py        CoverageGap registry, find_gap(), ensure_covered()
  markets.py         MarketInfo table, expected_latest_session(), sessions_between(), session_state()
  freshness.py       Freshness, assess(), stale_warning()
  semantics.py       Bar, normalize_eod_rows(), filler bars, price basis, known spin-off adjustments
  tools/
    __init__.py      register_all(server, deps), the guarded decorator, READ_ONLY annotations
    prices.py        get_eod_prices, get_intraday_prices
    quotes.py        get_quote
    fundamentals.py  get_fundamentals
    calendar.py      get_earnings_calendar, get_dividends, get_dividend_calendar, get_splits, get_split_calendar
    reference.py     list_exchanges, list_exchange_symbols, search_symbols, check_symbol
    usage.py         get_usage
tests/
  helpers.py         MockEODHD (httpx2.MockTransport), FixedClock, load_fixture()
  conftest.py        shared fixtures
  fixtures/          SYNTHETIC JSON only (see "Fixture policy")
  unit/              one file per core module
  tools/             one file per tools module
  integration/       server-level tests over an in-process MCP client
  contract/          optional, runs against a local cache of real responses (never committed)
  live/              optional, real API, a handful of calls
```

Python 3.10 is the floor. Do not use `StrEnum`, `datetime.UTC`, `typing.Self` or `tomllib`. Use
`datetime.timezone.utc` and `class X(str, Enum)`.

Runtime dependencies: `mcp>=2.3,<3` (the official SDK; `MCPServer` lives in `mcp.server.mcpserver`),
`httpx2` (the HTTP client the SDK itself uses), `pydantic>=2.12`, and `tzdata` on Windows. Nothing else.

## Module contracts

### config.py

```python
@dataclass(frozen=True)
class Settings:
    api_key: str | None              # masked in repr; never logged
    base_url: str = "https://eodhd.com/api"
    state_dir: Path = <platform default>
    daily_budget: int = 1000         # cost units per GMT day; 0 = no limit
    cache_enabled: bool = True
    stale_sessions: int = 3          # sessions behind before STALE_DATA
    timeout_s: float = 30.0
    max_retries: int = 2             # for 429, 5xx and network errors
    allow_uncovered: bool = False    # skip the coverage pre-check
    log_level: str = "WARNING"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings: ...
    def require_key(self) -> str: ...  # GuardError(CONFIG_ERROR) when missing
```

| Variable | Meaning | Default |
|---|---|---|
| `EODHD_API_KEY` | API key | none |
| `EODHD_API_KEY_FILE` | file holding the key (used when `EODHD_API_KEY` is unset) | none |
| `EODHD_GUARD_DAILY_BUDGET` | cost units per GMT day, 0 = unlimited | `1000` |
| `EODHD_GUARD_STATE_DIR` | ledger and cache folder | `$XDG_STATE_HOME/eodhd-guard-mcp`, else `~/.local/state/eodhd-guard-mcp`; `%LOCALAPPDATA%\eodhd-guard-mcp` on Windows |
| `EODHD_GUARD_CACHE` | `on`/`off` (also 1/0, true/false, yes/no) | `on` |
| `EODHD_GUARD_STALE_SESSIONS` | integer >= 1 | `3` |
| `EODHD_GUARD_TIMEOUT` | seconds, > 0 | `30` |
| `EODHD_GUARD_MAX_RETRIES` | integer 0..5 | `2` |
| `EODHD_GUARD_ALLOW_UNCOVERED` | bool | `off` |
| `EODHD_GUARD_BASE_URL` | API base, for tests and proxies | `https://eodhd.com/api` |
| `EODHD_GUARD_LOG_LEVEL` | DEBUG..CRITICAL | `WARNING` |

An invalid value raises `GuardError(CONFIG_ERROR)` naming the variable and the accepted values. A missing
key does not stop the server: listing tools works, and each tool call fails with `CONFIG_ERROR`. The key is
stripped of whitespace. A key file must be readable; its content is stripped.

### errors.py

```python
class ErrorCode(str, Enum):
    CONFIG_ERROR, INVALID_PARAMS, NOT_COVERED, NOT_FOUND, NO_DATA,
    AUTH_ERROR, PLAN_ERROR, QUOTA_EXCEEDED, RATE_LIMITED, BUDGET_EXCEEDED,
    UPSTREAM_ERROR, NETWORK_ERROR, BAD_RESPONSE

class GuardError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, hint: str | None = None,
                 details: dict[str, Any] | None = None) -> None: ...
    code: ErrorCode; message: str; hint: str | None; details: dict[str, Any]
    def __str__(self) -> str:  # "NOT_COVERED: <message> Hint: <hint>"
```

How upstream answers map to errors (in `client.py`):

| Upstream | Error | Billed |
|---|---|---|
| 401 | `AUTH_ERROR`: key missing, wrong or expired | no |
| 402 | `QUOTA_EXCEEDED`: the account's daily API limit is used up; it resets at 00:00 GMT | no |
| 403 | `PLAN_ERROR`: the subscription does not include this endpoint (the message names the plans that do) | no |
| 404 | `NOT_FOUND` with the upstream text (for example "Ticker Not Found.") | no |
| 429 (after retries) | `RATE_LIMITED` | no |
| 5xx (after retries) | `UPSTREAM_ERROR` | no |
| network error or timeout (after retries) | `NETWORK_ERROR`, with the exception class name only | no |
| 200 with a body that is not JSON | `BAD_RESPONSE` | yes |

Other 4xx statuses become `UPSTREAM_ERROR` with the status code.

### redact.py

```python
def redact(text: str, secrets: Iterable[str | None]) -> str
    # Replaces each secret of 6 or more characters with "***". Also masks the values of query
    # parameters named api_token, apikey, api_key or token in URL-like text.
class RedactingFilter(logging.Filter): ...   # redacts record.msg and record.args
def install_log_redaction(secret: str | None) -> None
    # Adds RedactingFilter to the root logger's handlers and to the "httpx2", "httpcore2" and "mcp"
    # loggers. Sets "httpx2" and "httpcore2" to WARNING: httpx2 logs every request URL at INFO, and the
    # URL carries the key.
```

### costs.py

Costs are in EODHD "API calls" as documented by EODHD and measured in October 2026. Only HTTP 200 answers
are billed (401, 402, 403 and 404 were measured as free).

| key | path | units | notes |
|---|---|---|---|
| `eod` | `eod/{symbol}` | 1 | |
| `intraday` | `intraday/{symbol}` | 5 | |
| `real-time` | `real-time/{symbol}` | 1 per symbol | `s=` adds symbols, 1 each |
| `fundamentals` | `fundamentals/{symbol}` | 10 | one request for the whole document |
| `calendar/earnings` | `calendar/earnings` | 1 | 1 for any window without `symbols`; 1 per symbol with `symbols` (not used) |
| `calendar/splits` | `calendar/splits` | 1 | |
| `calendar/dividends` | `calendar/dividends` | 1 | assumed from the Calendar API price; not measured |
| `div` | `div/{symbol}` | 1 | |
| `splits` | `splits/{symbol}` | 1 | |
| `exchanges-list` | `exchanges-list` | 1 | |
| `exchange-symbol-list` | `exchange-symbol-list/{exchange}` | 1 | |
| `search` | `search/{query}` | 1 | |
| `user` | `user` | 0 | account counters |

```python
@dataclass(frozen=True)
class EndpointCost:
    key: str; units: int; per_symbol: bool = False; measured: bool = True; note: str = ""
COSTS: Mapping[str, EndpointCost]
def cost_for(key: str, n_symbols: int = 1) -> int   # KeyError for unknown keys
```

### ledger.py

`usage.sqlite` in the state directory, table `calls(id, ts REAL, day TEXT, endpoint TEXT, cost INTEGER,
status INTEGER, cached INTEGER, duration_ms REAL, bytes INTEGER, symbol TEXT)`. `day` is the GMT date,
because EODHD's daily counter resets at 00:00 GMT. Rows older than 90 days are pruned when the ledger
opens. The ledger never stores the key, URLs or response bodies.

```python
class UsageLedger:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None
    def record(self, *, endpoint: str, cost: int, status: int, cached: bool, duration_ms: float,
               bytes_: int, symbol: str | None = None) -> None
    def spent_today(self) -> int                    # sum of cost for the current GMT day
    def summary(self, days: int = 7) -> dict[str, Any]
        # {"today": {"day", "cost", "requests", "cache_hits", "by_endpoint": {ep: {"cost", "requests",
        #   "cache_hits"}}}, "days": [{"day", "cost", "requests", "cache_hits"}, ...newest first],
        #  "session": {"since", "cost", "requests", "cache_hits"}}
    def close(self) -> None

class Budget:
    def __init__(self, ledger: UsageLedger, daily_limit: int, *, clock: Callable[[], datetime]) -> None
    def check(self, cost: int) -> None   # GuardError(BUDGET_EXCEEDED) if limit > 0 and spent + cost > limit
    def status(self) -> dict[str, Any]   # {"daily_limit", "spent_today", "remaining", "resets_at"} (ISO UTC)
```

The `BUDGET_EXCEEDED` message names the budget, the amount spent today, the variable that raises it, and
the reset time.

### cache.py

`cache.sqlite` in the state directory, table `responses(key TEXT PRIMARY KEY, endpoint TEXT, created REAL,
expires REAL, status INTEGER, body BLOB)`. The body is zlib-compressed. Only 200 and 404 answers are
cached.

```python
@dataclass(frozen=True)
class CachedResponse:
    status: int; body: bytes; created: datetime
class ResponseCache:
    def __init__(self, path: Path, *, enabled: bool, clock: Callable[[], datetime]) -> None
    @staticmethod
    def make_key(path: str, params: Mapping[str, Any]) -> str
        # sha256 of the path and the sorted params, with api_token removed
    def get(self, key: str) -> CachedResponse | None    # None when missing, expired or disabled
    def set(self, key: str, *, endpoint: str, status: int, body: bytes, ttl_s: float) -> None
    def purge_expired(self) -> int
    def clear(self) -> None
    def close(self) -> None
```

### client.py

```python
@dataclass(frozen=True)
class ApiResponse:
    endpoint: str            # cost key, e.g. "eod"
    status: int              # 200 (errors are raised)
    data: Any                # parsed JSON
    cached: bool
    cost: int                # units charged by this call (0 when cached)
    fetched_at: datetime     # when EODHD produced it (the original time for a cache hit)
    duration_ms: float

class EODHDClient:
    def __init__(self, settings: Settings, *, ledger: UsageLedger, cache: ResponseCache, budget: Budget,
                 clock: Callable[[], datetime], transport: httpx2.AsyncBaseTransport | None = None) -> None
    async def get(self, path: str, params: Mapping[str, Any] | None = None, *, endpoint: str, cost: int,
                  ttl_s: float, symbol: str | None = None) -> ApiResponse
    async def get_account(self) -> dict[str, Any]
        # GET user (free, never cached). Returns only apiRequests, apiRequestsDate, dailyRateLimit,
        # extraLimit, subscriptionType, subscriptionMode. Name, email, payment method and invite token
        # are dropped.
    async def aclose(self) -> None
```

What `get()` does, in order:

1. `settings.require_key()`.
2. Build the cache key from `path` and `params`. Every path segment is percent-encoded (`quote(seg, safe="")`),
   so `M&M.NSE` and search queries are safe.
3. On a cache hit, record a ledger row (cost 0, cached) and return it, or raise `NOT_FOUND` for a cached 404.
4. `budget.check(cost)`.
5. Send `GET {base_url}/{path}` with `params`, plus `fmt=json` and `api_token`. Retry 429, 5xx and network
   errors with backoff (0.5 s, 1 s, 2 s, at most `max_retries`), respecting `Retry-After` up to 5 s.
6. Record a ledger row for every network answer: cost is `cost` for a 200 and 0 otherwise.
7. Map non-200 answers to `GuardError` (see the table above). Cache 200 bodies for `ttl_s` and 404 bodies
   for 24 hours.
8. Parse the JSON. Anything that isn't JSON is `BAD_RESPONSE`.

Exceptions from httpx2 must never escape: their text holds the URL, and the URL holds the key. Every error
message passes through `redact()`.

### envelope.py

```python
class ToolWarning(BaseModel):
    code: str; message: str; details: dict[str, Any] | None = None
class Meta(BaseModel):
    source: str = "EODHD"; endpoints: list[str]; cost: int; cached: bool; fetched_at: str | None
    notes: list[str] = []
class ToolResponse(BaseModel):
    data: Any; warnings: list[ToolWarning] = []; meta: Meta
def build_response(data: Any, responses: Sequence[ApiResponse], *, warnings: Sequence[ToolWarning] = (),
                   notes: Sequence[str] = ()) -> ToolResponse
    # cost = sum, cached = all cached (True when no responses), fetched_at = the oldest fetched_at
    # (ISO UTC, "Z"), endpoints = unique in order
```

Warning codes, all listed in `WarningCode(str, Enum)`:

| Code | Raised by | Meaning |
|---|---|---|
| `STALE_DATA` | EOD, check_symbol | last bar is `stale_sessions` or more sessions behind the expected latest session |
| `FILLER_BARS_REMOVED` | EOD | zero-volume bars with open = high = low = close were dropped (holiday filler) |
| `RANGE_TRIMMED` | EOD, intraday | EODHD returned rows outside the requested range; they were dropped |
| `RANGE_CLAMPED` | EOD, intraday, calendars | an end date in the future was clamped to today |
| `TRUNCATED` | any list | more rows than `max_rows`; the most recent or first rows were kept |
| `SPINOFF_ADJUSTED_CLOSE` | EOD | the range starts before a known spin-off or demerger that EODHD folds into `close` |
| `CURRENCY_NOTE` | EOD, quote, dividends | London lines are usually quoted in GBX (pence) |
| `CURRENCY_MISMATCH` | fundamentals, dividends | statements or dividends use another currency than the quote |
| `DELAYED_QUOTE` | quote | quotes are 15-20 minutes delayed (always present) |
| `STALE_QUOTE` | quote | the quote's timestamp is far behind the expected latest session |
| `NO_QUOTE` | quote | EODHD answered with `"NA"` fields for a symbol |
| `INTRADAY_NOT_SAME_DAY` | intraday | the session is open or just closed, and EODHD has no bars for it yet |
| `NO_INTRADAY_DATA` | intraday | no bars in the window |
| `FILING_DATE_UNRELIABLE` | fundamentals | `filing_date` equals the period end, so it is not the publication date |
| `DIVIDEND_YIELD_INCONSISTENT` | fundamentals | `Highlights.DividendYield` is 0 while a forward dividend rate is set |
| `EPS_BASIS` | fundamentals | EPS actuals follow the analyst (adjusted) basis |
| `CALENDAR_TIMING_UNRELIABLE` | earnings | before/after-market flags were often wrong in checks |
| `NO_CALENDAR_ROWS` | earnings | requested symbols with no row in the window |
| `PARTIAL_COVERAGE` | earnings, search, exchanges | a market with dates only, or a listed-but-empty exchange |
| `POSSIBLY_INACTIVE` | search | a line's previous close is old: it may be delisted or renamed |
| `ODD_SPLIT_RATIO` | splits | a ratio that looks like a spin-off or rights adjustment, not a split |
| `EMPTY_RESULT` | lists | a legitimately empty answer (for example no dividends) |
| `BUDGET_LOW` | any | 80% or more of the daily budget is spent |

### validation.py

```python
@dataclass(frozen=True)
class Symbol:
    code: str          # upper-case
    exchange: str      # upper-case suffix after the LAST dot
    @property
    def full(self) -> str: ...   # "AAPL.US"
def parse_symbol(raw: str) -> Symbol
    # Strips whitespace and upper-cases. Requires CODE.EXCHANGE. The code allows A-Z 0-9 . - _ & ^ = + ;
    # the exchange allows A-Z 0-9, 1-10 characters. Without a suffix: INVALID_PARAMS that explains that
    # EODHD would silently use the US exchange, with examples (AAPL.US, VOD.LSE, NESN.SW, RELIANCE.NSE).
def parse_symbols(raw: Sequence[str] | str, *, max_n: int) -> list[Symbol]  # list or comma string; de-duplicated
def parse_exchange(raw: str) -> str                    # upper-case, A-Z 0-9, 1-10 chars
def parse_date(value: str | None, field: str) -> date | None   # strict YYYY-MM-DD, a real date
def parse_datetime_utc(value: str | None, field: str) -> datetime | None
    # YYYY-MM-DD (00:00 UTC) or ISO 8601 with optional seconds and Z/offset; naive means UTC
@dataclass(frozen=True)
class DateRange:
    start: date; end: date; clamped: bool
def resolve_range(start: date | None, end: date | None, *, today: date, default_days: int,
                  max_days: int | None = None) -> DateRange
    # end defaults to today; an end after today is clamped (clamped=True); start defaults to
    # end - default_days; start > end or a span over max_days is INVALID_PARAMS
def check_int(value: int, *, lo: int, hi: int, field: str) -> int
```

Every `INVALID_PARAMS` message names the field, the value received and what is accepted.

### coverage.py

```python
@dataclass(frozen=True)
class CoverageGap:
    key: str                         # "tokyo", "uae"
    market: str                      # "Tokyo Stock Exchange"
    suffixes: frozenset[str]         # exchange codes and suffixes that mean this market
    calendar_dates: bool             # True when the earnings calendar still has rows (Tokyo)
    message: str                     # what is missing and since when it was checked
    alternatives: str                # where the data can come from instead
GAPS: tuple[CoverageGap, ...]
def find_gap(exchange: str) -> CoverageGap | None
def ensure_covered(symbol: Symbol, capability: str, *, allow_uncovered: bool) -> None
    # capability in {"prices", "quotes", "fundamentals", "dividends", "splits", "symbols"};
    # raises NOT_COVERED unless allow_uncovered
```

The registry as of the October 2026 checks:

- **Tokyo** (`TSE`, `T`, `JP`, `JPX`, `XTKS`, `TYO`): no prices, fundamentals, dividends or symbol lists.
  Every suffix answered 404. The whole-market earnings calendar does carry `.TSE` rows. Alternatives:
  J-Quants (JPX's own API), or US ADRs such as `TM.US` for Toyota.
- **UAE** (`AE`, `DFM`, `ADX`, `AD`, `XDFM`, `XADS`): no prices, fundamentals or dates. `AE` exists but its
  symbol list is empty, and `EMAAR.AE` answers 200 with zero rows. Alternatives: DFM's own historical
  export, or a vendor with DFM and ADX coverage.

Messages say "in checks in October 2026" and tell the user to set `EODHD_GUARD_ALLOW_UNCOVERED=1` if
EODHD adds coverage. `TO` (Toronto) and `TA` (Tel Aviv) are not gaps; do not confuse them.

### markets.py

```python
@dataclass(frozen=True)
class MarketInfo:
    code: str; name: str; tz: str; open: time; close: time
    publish_lag: timedelta     # typical delay before EODHD's end-of-day bar appears
    lag_measured: bool         # True only for US, LSE, SW and NSE (October 2026)
    weekend: tuple[int, ...] = (5, 6)
    currency_hint: str | None = None   # "GBX" for LSE
MARKETS: Mapping[str, MarketInfo]
DEFAULT_MARKET: MarketInfo     # unknown exchanges: UTC, 00:00-23:59, lag 6 h
def market_for(exchange: str) -> MarketInfo                    # falls back to DEFAULT_MARKET
def expected_latest_session(exchange: str, now: datetime) -> date
    # the most recent session day (not a weekend day) whose close + publish_lag is at or before now,
    # in the market's time zone; holidays are not modelled
def sessions_between(earlier: date, later: date, exchange: str) -> int
    # session days in (earlier, later]; 0 when earlier >= later
def session_state(exchange: str, now: datetime) -> Literal["closed_day", "pre_open", "open", "post_close"]
```

The table covers at least these, with measured lags marked:

| Code | Name | TZ | Hours | Lag |
|---|---|---|---|---|
| `US` | US exchanges | America/New_York | 09:30-16:00 | 30 min (measured) |
| `LSE` | London | Europe/London | 08:00-16:30 | 2 h (measured) |
| `SW` | SIX Swiss | Europe/Zurich | 09:00-17:30 | 4 h (measured) |
| `NSE` | NSE India | Asia/Kolkata | 09:15-15:30 | 5 h (measured) |
| `BSE` | BSE India | Asia/Kolkata | 09:15-15:30 | 5 h |
| `XETRA`, `F` | Germany | Europe/Berlin | 09:00-17:30 | 4 h |
| `PA`, `AS`, `BR`, `LS`, `MC`, `MI` | Euronext, Madrid, Milan | local | 09:00-17:30 | 4 h |
| `TO`, `V` | Toronto, TSX Venture | America/Toronto | 09:30-16:00 | 2 h |
| `HK` | Hong Kong | Asia/Hong_Kong | 09:30-16:00 | 4 h |
| `AU` | ASX | Australia/Sydney | 10:00-16:00 | 4 h |
| `CC` | crypto | UTC | 00:00-23:59, no weekend | 1 h |
| `FOREX` | forex | UTC | 00:00-23:59 | 2 h |

### freshness.py

```python
@dataclass(frozen=True)
class Freshness:
    last_bar: date | None; expected_latest: date; sessions_behind: int; stale: bool
def assess(exchange: str, last_bar: date | None, now: datetime, *, threshold: int) -> Freshness
    # stale when last_bar is None or sessions_between(last_bar, expected_latest) >= threshold
def stale_warning(symbol: Symbol, fr: Freshness) -> ToolWarning
    # STALE_DATA: names the dates and the gap, says EODHD keeps answering 200 for frozen codes, and
    # points to check_symbol and search_symbols by company name
```

### semantics.py

```python
@dataclass(frozen=True)
class Bar:
    day: date; open: float; high: float; low: float; close: float
    adjusted_close: float | None; volume: float
def normalize_eod_rows(rows: Any) -> list[Bar]
    # Needs a list of dicts with date/open/high/low/close/volume; adjusted_close optional. Rows that
    # cannot be read are skipped. A non-list, or a non-empty list with no readable row, is BAD_RESPONSE.
def is_filler_bar(bar: Bar) -> bool       # volume == 0 and open == high == low == close
def apply_basis(bars: Sequence[Bar], basis: Literal["both", "raw", "adjusted"]) -> list[dict[str, Any]]
    # both: date, open, high, low, close, adjusted_close, volume
    # raw: date, open, high, low, close, volume
    # adjusted: open/high/low/close scaled by adjusted_close / close, volume as reported
KNOWN_SPINOFF_ADJUSTMENTS: Mapping[str, tuple[tuple[date, str], ...]]
    # NOVN.SW: Alcon spin-off (2019-04-09), Sandoz spin-off (2023-10-04)
    # ULVR.LSE: The Magnum Ice Cream Company demerger (2025-12-08)
    # RELIANCE.NSE: Jio Financial Services demerger (2023-07-20)
def spinoff_warnings(symbol: Symbol, first_day: date) -> list[ToolWarning]
PRICE_BASIS_NOTES: Mapping[str, str]       # the sentence each basis adds to meta.notes
```

The note for `both` and `raw` reads: "open/high/low/close are as EODHD reports them: not adjusted for
splits or dividends, but sometimes back-adjusted for spin-offs and demergers, so they may differ from the
traded price. adjusted_close is adjusted for splits and dividends; use it for returns."

## Tools

All tools are async, read-only (`readOnlyHint=True`, `openWorldHint=True`), take parameters declared as
`Annotated[T, Field(description=...)]` (the SDK does not read docstrings for parameter descriptions), and
return `ToolResponse`. A `GuardError` becomes `ToolError(str(error))`, which the model sees as an error
result. The `guarded` decorator in `tools/__init__.py` does this and redacts the message. Each description
states the cost in API calls and the main caveat in two to four sentences.

Every tool appends `BUDGET_LOW` when 80% or more of the budget is spent (`build_response` does not do this;
`guarded` adds it).

### get_eod_prices(symbol, start=None, end=None, period="d", price_basis="both", include_filler_bars=False, max_rows=500)

- 1 call. `eod/{symbol}` with `from`, `to`, `period`, `order=a`. TTL 6 h when `end` is within a day of today,
  else 24 h.
- `end` defaults to today (UTC) and `start` to `end` - 30 days; `max_rows` is 1..5000.
- Guards, in order:
  1. Coverage check.
  2. A 404 becomes `NOT_FOUND`, with a hint to use `search_symbols`, plus
     `list_exchange_symbols(delisted=true)` for delisted lines.
  3. A non-list body is `BAD_RESPONSE`.
  4. Rows outside the range are dropped (`RANGE_TRIMMED`).
  5. An empty result for a range that contains at least one session day is `NO_DATA`: "HTTP 200 with an
     empty list; the code may be delisted, renamed or not covered; run check_symbol". An empty range with
     no session days returns no bars and `EMPTY_RESULT`.
  6. Filler bars are dropped unless asked for (`FILLER_BARS_REMOVED`).
  7. For daily bars whose `end` is within 7 days of today, a freshness check (`STALE_DATA`).
  8. Spin-off warnings.
  9. `CURRENCY_NOTE` for LSE.
  10. Truncation keeps the most recent `max_rows` (`TRUNCATED`).
- data: `{symbol, period, price_basis, start, end, first_date, last_date, count, bars}`. The price-basis
  note goes in `meta.notes`.

### get_intraday_prices(symbol, interval="5m", start=None, end=None, max_rows=500)

- 5 calls. `intraday/{symbol}` with `interval`, `from` and `to` as Unix seconds. TTL 15 min when the window
  includes today, else 24 h.
- `end` defaults to now. `start` defaults to `end` - 1 day for 1m, 3 days for 5m and 14 days for 1h. The
  maximum span is 120, 600 and 7200 days respectively (`INVALID_PARAMS` above it).
- Rows carry `timestamp`, `gmtoffset`, `datetime` (UTC), `open`, `high`, `low`, `close` and `volume`. Output
  rows: `{datetime_utc, open, high, low, close, volume}`.
- Guards:
  - Coverage check.
  - Out-of-window rows are dropped.
  - `INTRADAY_NOT_SAME_DAY` when the window includes today, the session is open or has closed today, and no
    bar falls on today's local date. The message says bars are published after the session and suggests
    `get_quote`.
  - Empty data is not an error: no bars, plus `NO_INTRADAY_DATA` (history depth varies by exchange and
    plan).
- A 403 means the plan lacks intraday (EOD+Intraday or All-in-One).

### get_quote(symbols)

- 1 call per symbol, at most 20. `real-time/{first}` with `s=` for the rest. TTL 60 s. The body is a dict
  for one symbol and a list for several.
- Each requested symbol gets an entry:
  - `{symbol, status: "ok", price, open, high, low, previous_close, change, change_pct, volume, as_of,
    age_seconds, delayed: true}`
  - `{symbol, status: "no_quote" | "missing" | "not_covered", message}`
- Guards:
  - A symbol whose `timestamp` or `close` is `"NA"` or null gets `no_quote` plus a `NO_QUOTE` warning
    ("unknown, delisted or renamed code; run check_symbol").
  - `STALE_QUOTE` when the quote's date is `stale_sessions` or more sessions behind.
  - Uncovered symbols are never sent.
  - When no symbol has a quote, the tool raises `NO_DATA` listing each symbol's status.
  - `DELAYED_QUOTE` is always present. `CURRENCY_NOTE` applies to LSE lines.

### get_fundamentals(symbol, sections=None, statements=None, period="quarterly", periods=4)

- 10 calls for the first request. One `fundamentals/{symbol}` request fetches the whole document, which is
  cached for 24 h. Every later section or statement read for that symbol is free until the cache expires.
- Default sections:
  - stocks: General, Highlights, Valuation, SharesStats, SplitsDividends, Earnings
  - ETFs (`General.Type == "ETF"`): General, Technicals, ETF_Data
  - Funds: General, MutualFund_Data
- `statements` is a subset of income, balance and cashflow, empty by default. `period` is quarterly or
  yearly, and `periods` is 1..40.
- Trimming:
  - General drops Officers, AddressData, Phone and LogoURL, and cuts Description to 500 characters.
  - SplitsDividends keeps the 5 newest years of NumberDividendsByYear.
  - Holders keep the top 10. InsiderTransactions keep the newest 10.
  - Earnings History, Trend and Annual keep the newest `periods`.
  - Each statement becomes `{currency, periods: [{date, filing_date, ...non-null fields}]}` with the newest
    `periods`.
  - ETF_Data drops Holdings and keeps Top_10_Holdings, Sector_Weights and World_Regions.
  - The output is capped at about 150 KB (`TRUNCATED`).
- Guards:
  - An empty document is `NO_DATA`; a 404 is `NOT_FOUND`.
  - `CURRENCY_MISMATCH` when a statement's currency differs from `General.CurrencyCode`.
  - `FILING_DATE_UNRELIABLE` when `filing_date == date` in any returned period.
  - `DIVIDEND_YIELD_INCONSISTENT`.
  - `EPS_BASIS` when Earnings is included.
  - A note that new quarters usually arrive 1-4 days after the release, with `General.UpdatedAt`.
- A 403 means the plan lacks fundamentals (Fundamentals Data Feed or All-in-One).

### get_earnings_calendar(start=None, end=None, symbols=None, exchange=None, max_rows=200, offset=0)

- 1 call for any window. One whole-market `calendar/earnings` request with `from`/`to` (TTL 6 h), filtered
  here by `symbols` and `exchange`.
- Per-symbol requests would cost 1 each, and they return nothing for `.TSE` codes.
- `start` defaults to today and `end` to `start` + 14 days. The window is at most 90 days; `symbols` holds
  at most 100.
- Output rows are sorted by `report_date` then symbol: `{symbol, report_date, period_end, timing, currency,
  eps_actual, eps_estimate, surprise, surprise_pct}`.
- Guards:
  - UAE symbols get `NOT_COVERED` warnings.
  - `.TSE` symbols get `PARTIAL_COVERAGE` (dates only, no prices).
  - `NO_CALENDAR_ROWS` for requested symbols that have no row.
  - `CALENDAR_TIMING_UNRELIABLE` is always present.
  - `TRUNCATED` reports `total`.
- A 403 means the plan lacks the calendar.

### get_dividends(symbol, start=None, end=None, max_rows=100)

- 1 call. `div/{symbol}` with `from`/`to`. TTL 24 h.
- No `start` means full history. The newest `max_rows` rows are kept.
- Rows: `{ex_date, declaration_date, record_date, payment_date, period, value, unadjusted_value, currency,
  upcoming}`.
- Guards:
  - An empty answer is `EMPTY_RESULT`, not an error.
  - `CURRENCY_MISMATCH` when the dividend currency differs from the line's quote currency (USD dividends
    on GBX lines).
  - A note that `value` is split-adjusted and `unadjusted_value` is as declared.

### get_dividend_calendar(date, max_rows=200, offset=0)

- 1 call. `calendar/dividends` with `filter[date_eq]`, `page[limit]` and `page[offset]`. TTL 6 h.
- Rows: `{symbol, date}`. Calendar rows have no amounts; a note points to `get_dividends`.

### get_splits(symbol, start=None, end=None)

- 1 call. `splits/{symbol}`. TTL 24 h.
- Rows: `{date, ratio, new_shares, old_shares, factor}`, parsed from `"new/old"`.
- `ODD_SPLIT_RATIO` when either side is not a whole number, or when the factor lies within 0.8..1.25 and is
  not 1: these are usually spin-off or rights adjustments.

### get_split_calendar(start=None, end=None, symbols=None, exchange=None, max_rows=200, offset=0)

- 1 call. `calendar/splits` with `from`/`to`. TTL 6 h.
- `start` defaults to today and `end` to `start` + 30 days; the window is at most 90 days.
- Rows: `{symbol, split_date, old_shares, new_shares, factor, optionable}`, filtered here.

### list_exchanges(query=None)

- 1 call. `exchanges-list`. TTL 7 days.
- Rows: `{code, name, country, currency, operating_mic, country_iso2, country_iso3, coverage_note}`.
- `query` filters by code, name or country.
- `PARTIAL_COVERAGE` notes that Tokyo, DFM and ADX have no data and that `AE` is listed but empty.

### list_exchange_symbols(exchange, query=None, type=None, delisted=False, max_rows=100, offset=0)

- 1 call. `exchange-symbol-list/{exchange}`, with `delisted=1` when asked. TTL 24 h. One cached list per
  exchange and flag; filtering happens here.
- Rows: `{symbol: CODE.<requested exchange>, code, name, type, currency, country, isin, venue}`.
- `type` is matched case-insensitively against EODHD's Type, with spaces as underscores: common_stock, etf,
  fund, preferred_stock and so on.
- Guards:
  - Coverage check (`NOT_COVERED`).
  - A 404 is `NOT_FOUND`.
  - `[]` is `NO_DATA` ("listed but empty", as with AE).
  - `TRUNCATED` reports the `total`.
- The US delisted list has about 60,000 rows (8.5 MB); it is cached and paged locally.

### search_symbols(query, exchange=None, type="all", max_results=15)

- 1 call. `search/{query}` with `limit`, `exchange` and `type`. TTL 24 h. `max_results` is 1..50.
- `type` is one of all, stock, etf, fund, bonds, index or crypto.
- Rows: `{symbol, code, exchange, name, type, country, currency, isin, is_primary, previous_close,
  previous_close_date, possibly_inactive}`.
- `possibly_inactive` is set (with `POSSIBLY_INACTIVE`) when `previous_close_date` is `stale_sessions` or
  more sessions behind for that exchange.
- Rows on gap exchanges get `PARTIAL_COVERAGE`.
- A note says that delisted lines are not searchable; use `list_exchange_symbols(delisted=true)`.
- An empty result is `EMPTY_RESULT`.

### check_symbol(symbol, include_quote=False, find_successor=True)

- 1 to 4 calls.
- Steps:
  1. Coverage check. A gap returns status `not_covered` and costs 0.
  2. `eod/{symbol}` for the last 14 days. A 404 means `not_found`; an empty answer or stale data means
     `stale`.
  3. Optionally a quote: `"NA"` means `no_quote`.
  4. When the status is not ok and `find_successor` is true:
     - `search/{code}` finds the line and its name;
     - `search/{name}` with the same exchange lists candidates with a recent `previous_close_date` and a
       different code.
- data: `{symbol, status: ok|stale|not_found|no_quote|not_covered, last_bar_date, expected_latest_session,
  sessions_behind, quote_status, name, isin, successor_candidates: [{symbol, name, isin,
  previous_close_date}], advice}`.

### get_usage(days=7, include_account=True)

- 0 calls. The account part is the free `user` endpoint.
- data:
  - `ledger`: `UsageLedger.summary(days)`
  - `budget`: `Budget.status()`
  - `account`: `EODHDClient.get_account()` mapped to `{api_requests_today, requests_date,
    daily_rate_limit, extra_limit, subscription_type, subscription_mode}`
  - `cost_table`: `{key: units}`
- No name, email, payment method or invite token, ever.
- Notes:
  - EODHD counts per GMT day.
  - The account counter includes other apps that share the key; this ledger counts only this server.
  - Cached answers cost 0.

## Server

`build_deps(settings, *, transport=None, clock=None) -> Deps` creates the ledger, cache, budget and client.
`Deps` is a dataclass holding `settings`, `client`, `ledger`, `cache`, `budget` and `clock`, where the clock
returns an aware UTC `datetime`. `build_server(settings=None, *, deps=None) -> MCPServer` creates
`MCPServer("eodhd-guard-mcp", version=__version__, instructions=INSTRUCTIONS, lifespan=...)`, calls
`register_all(server, deps)` and closes the client, ledger and cache in the lifespan exit.

`INSTRUCTIONS` (kept under 1,500 characters) tells the model:

- symbols are CODE.EXCHANGE;
- surface every warning to the user;
- check `meta.cost` and use `get_usage` before bulk work;
- use adjusted_close for returns and treat close as reported;
- quotes are delayed;
- Tokyo and the UAE are not covered;
- call `check_symbol` when data looks stale.

`cli.py`:

- `eodhd-guard-mcp [--transport stdio|streamable-http] [--host 127.0.0.1] [--port 8000] [--check [--online]]
  [--version]`.
- Logs go to stderr only (stdout carries the protocol), at the configured level, with
  `install_log_redaction`.
- `--check` prints the configuration with the key masked as set/not set, the state dir and the budget.
- `--online` adds the free `user` call and prints only its counters. The exit code is non-zero on a
  problem.
- With streamable-http, the default host is 127.0.0.1 and a stderr warning says the endpoint has no
  authentication.

## Fixture policy

EODHD's licence is for personal use and forbids redistributing its data. **Never commit a real EODHD
response, or values copied from one.**

- Tests use synthetic fixtures with fictional companies (ACME.US, GLOBEX.LSE, INITECH.SW, UMBRELLA.NSE) and
  invented numbers. They copy only the shape of real answers: field names, types and quirks such as "NA"
  strings, `[]`, plain-text 404 bodies, filler bars and odd split ratios.
- Real responses may be used only from a local, git-ignored folder, through `tests/contract`. Those tests
  are skipped unless `EODHD_GUARD_EVAL_CACHE` points to such a folder.
- Live tests (`tests/live`, marker `live`) run only with `EODHD_GUARD_LIVE=1` and a key, and stay under 10
  calls per run.

## Testing helpers (tests/helpers.py)

```python
class MockEODHD:
    base_url = "https://eodhd.test/api"
    def add(self, path: str, body: Any = None, *, status: int = 200, text: str | None = None,
            times: int | None = None) -> None   # path like "eod/ACME.US"; JSON body or raw text
    def transport(self) -> httpx2.MockTransport
    requests: list[RecordedRequest]            # path and params, with api_token removed
    def count(self, path_prefix: str = "") -> int
    # An unmocked path fails the test with an AssertionError that names the path.
class FixedClock:
    def __init__(self, now: datetime) -> None
    def __call__(self) -> datetime
    def advance(self, **kwargs: float) -> None
def load_fixture(name: str) -> Any               # tests/fixtures/<name>.json
```

`conftest.py` provides these fixtures:

- `clock`: Tue 2026-11-03 15:00 UTC;
- `mock_api`;
- `settings`: tmp state dir, key `"test-key-0123456789"`, budget 1000, base URL `https://eodhd.test/api`;
- `deps`;
- `server`;
- `mcp_client`: an async context over `mcp.Client(server)`.
