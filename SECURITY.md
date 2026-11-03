# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| Anything older | No |

While the project is in alpha, only the latest 0.1.x release gets fixes.

## Report a vulnerability

Use GitHub's private vulnerability reporting. On the repository page, open the **Security** tab, choose **Report a vulnerability** and describe the problem. Only the maintainer can read the report.

Please do not open a public issue, pull request or discussion for a vulnerability.

If the **Report a vulnerability** option is not available, open an issue that says only that you have a security report, with no details, and the maintainer will arrange a private channel.

A useful report has:

- the version (`eodhd-guard-mcp --version`) and how you run the server (stdio or streamable HTTP);
- what you did, what you expected and what happened;
- the impact, in your view;
- a minimal reproduction that uses synthetic data.

**Never include a real API key, account details or raw EODHD responses in a report.** If a key shows up in an output you want to report, replace it with `***` and show only where it appeared. If your own key has appeared anywhere it should not, rotate it in your EODHD account.

This is a one-person project. The maintainer will acknowledge a report as soon as they can and will say what they plan to do about it. If you want credit for the finding, it can go in the changelog.

## What counts

In scope:

- **Key leakage.** Any route by which the EODHD API key reaches a log line, an error message, a tool result, the cache, the usage ledger, the process's standard output, or any file that is not meant to hold it.
- **Injection through tool output.** Text that comes from EODHD or the network, such as company names, descriptions and error bodies, reaches the model in tool results and error messages. Anything that lets such text pass as the server's own warnings or instructions, or change what the server does, is in scope.
- **Path handling.** A tool argument (a symbol, an exchange, a search query) that can change the path or query of the request sent to EODHD. Also unsafe handling of local paths, such as the key file and the state directory, for example reading or writing outside the intended location.
- Requests that go somewhere other than the configured EODHD base URL, and a network transport that exposes more than the documentation says.

Out of scope:

- The missing authentication on the streamable-HTTP transport. It is documented. Keep the server on `127.0.0.1`, or put your own authentication in front of it.
- Flaws in EODHD's API or data, in the MCP SDK, in Python or in other dependencies. Report those to their maintainers.
- Attacks that need control of your machine, your environment variables or your MCP client's configuration.
- Wrong, late or missing EODHD data. Those are covered in [docs/caveats.md](docs/caveats.md); if you find a new one, open a normal issue, without pasting data.

## How the key is handled

- **Source.** The key comes from `EODHD_API_KEY` or from the file named by `EODHD_API_KEY_FILE`. It is never a tool argument, so the model cannot read it or set it. The settings object masks it in its representation, and `eodhd-guard-mcp --check` shows only whether a key is set.
- **Destination.** The key is sent only to the EODHD API base URL, as the `api_token` query parameter. `EODHD_GUARD_BASE_URL` can change that URL, and only the person who runs the server sets it. It must be an `https` URL, or `http` for localhost only, and it cannot carry credentials, a query or a fragment. The client does not follow redirects, so an answer cannot send the key to another host.
- **Logs.** Log records pass through a redaction filter on the root handlers and on the `httpx2`, `httpcore2` and `mcp` loggers. It masks the key itself and the values of `api_token`, `apikey`, `api_key` and `token` query parameters. The HTTP client's own request logging, which would print the URL, is held at WARNING. Logs go to stderr, because stdout carries the protocol.
- **Errors.** Exceptions from the HTTP client never escape, because their text contains the URL. A network failure reports the exception class name only. Every error message passes through the same redaction.
- **Cache and ledger.** The cache key is a hash of the path and parameters with `api_token` removed, and the cache stores response bodies only. The usage ledger stores no key, no URL and no body.
- **Account data.** `get_usage` returns counters only. The name, email address, payment method and invite token that EODHD's account endpoint returns are dropped before anything reaches the model.
- **Requests.** Every path segment is percent-encoded, so a symbol such as `M&M.NSE` or a search query cannot change the shape of the request.
- **Network exposure.** The streamable-HTTP transport binds to `127.0.0.1` by default and prints a warning to stderr that it has no authentication.

## Local data

The cache in the state directory holds licensed EODHD responses and is not encrypted. Keep the directory out of repositories and shared folders. See the [data licence notice](README.md#data-licence-notice) and [Cache and state files](README.md#cache-and-state-files).
