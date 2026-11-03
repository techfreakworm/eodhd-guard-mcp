## What and why

<!-- One or two sentences. Say why the change is needed, not only what it does. Link the issue if there is one. -->

## Checklist

- [ ] `pytest` passes and the 85% coverage gate holds.
- [ ] `ruff check .`, `ruff format --check .` and `mypy` are clean.
- [ ] Every new or changed guard has a test that fails without it.
- [ ] Test data is synthetic: fictional tickers and invented numbers, no real EODHD responses (see the fixture policy in CONTRIBUTING.md).
- [ ] A new or changed tool states its cost in API calls and its main caveat in its description, and `docs/DESIGN.md` and the README tool table match.
- [ ] `CHANGELOG.md` has a line under "Unreleased".
- [ ] No API keys, account details or private paths appear in the diff, in logs, or in this description.
