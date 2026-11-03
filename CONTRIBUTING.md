# Contributing

Thanks for helping. This is a small, deliberately narrow project, so please read this page and [docs/DESIGN.md](docs/DESIGN.md) before you change code. Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

For a new tool or a large change, open an issue first so we can agree that it fits. A tool belongs here when it guards against a failure an AI assistant cannot see, or against a cost it cannot see.

## Fixture policy: read this first

**Tests use synthetic data only. Never commit a real EODHD response, or values copied from one.**

EODHD's licence is for personal use and forbids redistributing its data. A real response in a fixture, an issue, a pull request, a log or a doc counts as redistribution.

- Fixtures live in `tests/fixtures/` as JSON. They use fictional companies (`ACME.US`, `GLOBEX.LSE`, `INITECH.SW`, `UMBRELLA.NSE`) and invented numbers. A test that reproduces one documented real case, such as Roche's re-key from `ROG.SW` to `ROP.SW`, a Tokyo or UAE code, or a known spin-off, may use the real ticker and company name, with invented prices, ISINs and everything else.
- Copy only the **shape** of real answers: field names, types and quirks such as `"NA"` strings, `[]` bodies, plain-text 404 bodies, filler bars and odd split ratios. Do not copy values.
- A real number that has been nudged is still a real number. Invent new ones.
- Real responses may be used only from a local, git-ignored folder, through the contract tests described below. That folder never leaves your machine.
- The same rule covers issues, pull requests, commit messages and docs: describe the shape of a result and do not paste it. Percentages and dates of public corporate events are fine.
- The secret scanners catch keys, not licensed data. You are the check.

## Set up

```bash
git clone https://github.com/techfreakworm/eodhd-guard-mcp.git
cd eodhd-guard-mcp
python -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pip install pre-commit && pre-commit install    # optional, but it catches problems early
```

Python 3.10 is the floor, and the code must run on 3.10 to 3.13. Do not use `StrEnum`, `datetime.UTC`, `typing.Self` or `tomllib`. Use `datetime.timezone.utc` and `class X(str, Enum)`.

## Run the checks

```bash
pytest                                                    # the test suite; it makes no network calls
pytest --cov=eodhd_guard_mcp --cov-report=term-missing    # with coverage; CI fails below 85%
ruff check .                                              # lint
ruff format .                                             # format (add --check to only report)
mypy                                                      # strict type check
```

CI runs the same checks. It runs the tests on Linux with Python 3.10 to 3.13, and on macOS and Windows with Python 3.12. It also builds the package and scans the history for secrets.

## Contract tests and live tests

**Contract tests** (`tests/contract`, marker `contract`) run against a local folder of real responses. They are skipped unless `EODHD_GUARD_EVAL_CACHE` points at that folder. The folder holds one JSON file per answer, named `<endpoint>/<name>__<hash>.json`, each a wrapper `{"status": ..., "body": ...}` around the raw answer. The tests assert codes and shapes, never values.

```bash
EODHD_GUARD_EVAL_CACHE=/path/to/your/local/folder pytest tests/contract
```

The folder holds licensed data. Keep it outside the repository, or in `_local/`, which is git-ignored. Never commit it, share it or attach it to an issue.

**Live tests** (`tests/live`, marker `live`) call the real API. They run only with `EODHD_GUARD_LIVE=1` and a key, and must stay under 10 calls per run (the current ones make about six, with a budget of 25). CI never runs them.

```bash
EODHD_GUARD_LIVE=1 EODHD_API_KEY_FILE="$HOME/.config/eodhd-guard-mcp/api_key" pytest tests/live
```

On Windows PowerShell, set the variables with `$env:EODHD_GUARD_LIVE = "1"`. Prefer a mock test whenever one can prove the same thing, because every live call spends your allowance.

## Add a tool

Follow [docs/DESIGN.md](docs/DESIGN.md). It is the contract between the modules, so change it in the same pull request as the code.

1. **Design first.** Add the tool to DESIGN.md: signature with defaults, cost, cache lifetime, guards in order, and output shape.
2. **Price it.** Add the endpoint to the cost table in `costs.py`. If you took the price from EODHD's documentation and did not measure it, say so with `measured=False`.
3. **Validate before calling.** Use `validation.py` (`parse_symbol`, `parse_date`, `resolve_range`, `check_int` and the rest). EODHD ignores bad parameters, so the tool must not. Every `INVALID_PARAMS` message names the field, the value received and what is accepted.
4. **Write the tool** in the matching module under `src/eodhd_guard_mcp/tools/`, and register it in that module's `register(server, deps)`. A new module also goes into the list in `register_all`. Declare parameters as `Annotated[T, Field(description=...)]`, because the SDK does not read docstrings for parameter descriptions. The tool description is a module constant. It states the cost in API calls and the main caveat, in two to four sentences. Tools are read-only (use `READ_ONLY`) and go through the `guarded` decorator.
5. **Treat empty and placeholder answers as non-data.** HTTP 200 with `[]`, `"NA"` fields or the wrong shape becomes an error or an explicit warning, never a silent success. Return `build_response`, add any new warning code to `WarningCode`, to the table in DESIGN.md and to the table in the README, and keep default outputs small.
6. **Test every guard.** For each guard, write a test that fails when the guard is removed. Use the `MockEODHD` helper and synthetic fixtures. An unmocked path fails the test with an error that names it, so a test cannot reach the network by accident. Add a test through the in-process MCP client (the `mcp_client` fixture) for registration and error mapping. Those tests sit beside the module's other tests in `tests/tools/`.
7. **Document it.** Update the README tool table and tool section, `docs/caveats.md` if the tool guards a new quirk of the data, and `CHANGELOG.md`.

## Update the coverage registry

`coverage.py` holds `GAPS`, the markets where EODHD has no usable data. A request for a symbol on one of them is refused with `NOT_COVERED` before it costs anything.

To add a gap:

1. Check it on your own account. Record the date, the endpoints you tried and the HTTP statuses. Put that evidence in the pull request as statuses and shapes, not data.
2. Add a `CoverageGap` with its `key`, `market`, every exchange code and suffix that means the market (`suffixes`), whether the earnings calendar still has rows (`calendar_dates`), a `message` that says what is missing and when it was checked ("in checks in October 2026"), and `alternatives`.
3. Be careful with look-alike suffixes. `T` is Tokyo, but `TO` is Toronto and `TA` is Tel Aviv.
4. Add tests for `find_gap`, `ensure_covered` and `ensure_exchange_covered`, and update the registry list in DESIGN.md and the section in `docs/caveats.md`.

To remove a gap, repeat the check when EODHD announces coverage, delete the entry, and say so in the changelog. Users can switch the pre-check off in the meantime with `EODHD_GUARD_ALLOW_UNCOVERED=1`.

Two other registries work the same way:

- `KNOWN_SPINOFF_ADJUSTMENTS` in `semantics.py` lists spin-offs and demergers that EODHD folds into `close`. Add one only after confirming the back-adjustment against a second source, and give the event date and a plain description. Both are public facts.
- `MARKETS` in `markets.py` holds trading hours, time zones and publication lags. Set `lag_measured` to true only for a lag you measured.

## Commit messages

Write the subject in the imperative mood, in about 72 characters or fewer, and explain why in the body. The diff already shows what changed.

```text
Drop holiday filler bars from EOD results

EODHD returns flat, zero-volume bars on exchange holidays. A model reads
them as trading days on which nothing moved, so remove them unless the
caller asks to keep them.
```

Keep one logical change in each commit, and keep pull requests small.

## Pull request checklist

The pull request template has the same list.

- [ ] `pytest` passes and the 85% coverage gate holds.
- [ ] `ruff check .`, `ruff format --check .` and `mypy` are clean.
- [ ] Every new or changed guard has a test that fails without it.
- [ ] Test data is synthetic: fictional tickers and invented numbers, no real EODHD responses.
- [ ] A new or changed tool states its cost in API calls and its main caveat in its description, and DESIGN.md and the README match.
- [ ] `CHANGELOG.md` has a line under "Unreleased".
- [ ] No API keys, account details or private paths appear in the diff, in logs, or in the description.

## Never paste API keys

Do not put an API key in an issue, a pull request, a commit message, a test, a log, a screenshot or a chat. The same goes for account details such as your name, email address or payment method.

- Give the server your key through `EODHD_API_KEY_FILE`, so it stays out of your shell history.
- `eodhd-guard-mcp --check` shows whether a key is set and never shows the key.
- If a key leaks, rotate it in your EODHD account straight away. Deleting a comment is not enough, because git history, caches and mirrors keep copies.
- The tests use the fake key `test-key-0123456789`, and `.gitleaks.toml` excuses that string inside `tests/` only. Use it for nothing else, and do not add other exceptions without a reason.
- To report a security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.
