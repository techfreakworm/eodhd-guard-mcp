from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator
from urllib.parse import quote, quote_plus

import httpx2
import pytest

from eodhd_guard_mcp.redact import MASK, MIN_SECRET_LENGTH, RedactingFilter, install_log_redaction, redact
from tests.helpers import TEST_KEY


class TestRedact:
    def test_masks_the_secret_everywhere(self) -> None:
        text = f"first {TEST_KEY}, again {TEST_KEY}."
        assert redact(text, [TEST_KEY]) == f"first {MASK}, again {MASK}."

    def test_text_without_a_secret_is_unchanged(self) -> None:
        text = "Ticker Not Found. See https://eodhd.com/api/eod/ACME.US?from=2026-01-01"
        assert redact(text, [TEST_KEY]) == text

    def test_none_and_empty_secrets_are_ignored(self) -> None:
        assert redact("plain", [None, ""]) == "plain"

    def test_a_secret_shorter_than_the_minimum_is_ignored(self) -> None:
        short = "x" * (MIN_SECRET_LENGTH - 1)
        assert redact(f"a {short} b", [short]) == f"a {short} b"

    def test_a_secret_of_the_minimum_length_is_masked(self) -> None:
        exact = "x" * MIN_SECRET_LENGTH
        assert redact(f"a {exact} b", [exact]) == f"a {MASK} b"

    def test_the_longer_of_two_overlapping_secrets_wins(self) -> None:
        assert redact("abcdefghij", ["abcdef", "abcdefghij"]) == MASK
        assert redact("abcdefghij", ["abcdefghij", "abcdef"]) == MASK

    def test_the_percent_encoded_forms_are_masked_too(self) -> None:
        secret = "ab c+d/ef=gh"
        for form in (secret, quote(secret, safe=""), quote_plus(secret)):
            assert redact(f"https://x/?k={form}&y=1", [secret]) == f"https://x/?k={MASK}&y=1"

    @pytest.mark.parametrize("name", ["api_token", "apikey", "api_key", "token", "API_TOKEN", "ApiKey"])
    def test_a_key_parameter_in_a_url_is_masked_without_knowing_the_secret(self, name: str) -> None:
        url = f"https://eodhd.com/api/eod/ACME.US?from=2026-01-01&{name}=abc123XYZ.98&fmt=json"
        assert redact(url, []) == f"https://eodhd.com/api/eod/ACME.US?from=2026-01-01&{name}={MASK}&fmt=json"

    def test_a_key_parameter_at_the_start_or_end_of_the_text(self) -> None:
        assert redact("api_token=abc123", []) == f"api_token={MASK}"
        assert redact("(see ?token=abc123)", []) == f"(see ?token={MASK})"

    @pytest.mark.parametrize("name", ["my_token", "x-api_token", "tokenizer", "stoken"])
    def test_similar_looking_parameters_are_left_alone(self, name: str) -> None:
        assert redact(f"{name}=abc123", []) == f"{name}=abc123"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("{'api_token': 'abc123', 'fmt': 'json'}", "{'api_token': '***', 'fmt': 'json'}"),
            ('{"token": "abc123"}', '{"token": "***"}'),
            ('{"apikey":"abc123"}', '{"apikey":"***"}'),
        ],
    )
    def test_a_quoted_key_in_a_dict_or_json_is_masked(self, text: str, expected: str) -> None:
        assert redact(text, []) == expected

    def test_it_is_idempotent(self) -> None:
        once = redact(f"https://x/?api_token={TEST_KEY}&a={TEST_KEY}", [TEST_KEY])
        assert redact(once, [TEST_KEY]) == once

    def test_secrets_can_be_any_iterable(self) -> None:
        assert redact(f"a {TEST_KEY}", iter([TEST_KEY])) == f"a {MASK}"
        assert redact(f"a {TEST_KEY}", {TEST_KEY}) == f"a {MASK}"


def make_record(msg: object, args: object = None, exc_info: object = None) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, exc_info)  # type: ignore[arg-type]


class TestRedactingFilter:
    def test_it_masks_the_message_and_the_arguments(self) -> None:
        record = make_record("fetch %s with %s", ("ACME.US", TEST_KEY))
        assert RedactingFilter([TEST_KEY]).filter(record) is True
        assert record.getMessage() == f"fetch ACME.US with {MASK}"

    def test_it_masks_an_argument_that_is_not_a_string(self) -> None:
        url = httpx2.URL(f"https://eodhd.test/api/eod/ACME.US?api_token={TEST_KEY}")
        record = make_record('HTTP Request: %s %s "%s"', ("GET", url, "HTTP/1.1 200 OK"))
        RedactingFilter([TEST_KEY]).filter(record)
        assert TEST_KEY not in record.getMessage()
        assert "api_token=***" in record.getMessage()

    def test_it_masks_a_message_that_is_not_a_string(self) -> None:
        record = make_record(ValueError(f"bad {TEST_KEY}"))
        RedactingFilter([TEST_KEY]).filter(record)
        assert record.getMessage() == f"bad {MASK}"

    def test_it_masks_a_mapping_argument(self) -> None:
        record = make_record("%(k)s", ({"k": TEST_KEY},))
        RedactingFilter([TEST_KEY]).filter(record)
        assert record.getMessage() == MASK

    def test_a_broken_format_string_falls_back_to_the_masked_message(self) -> None:
        record = make_record(f"{TEST_KEY} %s %s", ("only one",))
        RedactingFilter([TEST_KEY]).filter(record)
        assert TEST_KEY not in record.getMessage()
        assert "only one" not in record.getMessage()

    def test_it_masks_a_traceback(self) -> None:
        try:
            raise httpx2.ConnectError(f"failed: https://eodhd.test/api/x?api_token={TEST_KEY}")
        except httpx2.ConnectError:
            record = make_record("request failed", exc_info=sys.exc_info())
        RedactingFilter([TEST_KEY]).filter(record)
        assert record.exc_text is not None
        assert "ConnectError" in record.exc_text
        assert TEST_KEY not in record.exc_text
        assert TEST_KEY not in logging.Formatter().format(record)

    def test_it_masks_stack_info(self) -> None:
        record = make_record("here")
        record.stack_info = f"Stack: call(api_token={TEST_KEY})"
        RedactingFilter([TEST_KEY]).filter(record)
        assert record.stack_info == f"Stack: call(api_token={MASK})"

    def test_secrets_can_be_added_later_and_short_ones_are_ignored(self) -> None:
        log_filter = RedactingFilter()
        log_filter.add_secret(None)
        log_filter.add_secret("abc")
        log_filter.add_secret(TEST_KEY)
        record = make_record("%s abc", (TEST_KEY,))
        log_filter.filter(record)
        assert record.getMessage() == f"{MASK} abc"


@pytest.fixture
def stream_handler() -> Iterator[tuple[logging.Handler, io.StringIO]]:
    """A handler on the root logger that writes to a string, removed afterwards."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler, stream
    root.removeHandler(handler)


@pytest.mark.usefixtures("isolated_logging")
class TestInstall:
    def test_httpx2_and_httpcore2_are_set_to_warning(self) -> None:
        logging.getLogger("httpx2").setLevel(logging.DEBUG)
        logging.getLogger("httpcore2").setLevel(logging.DEBUG)
        install_log_redaction(TEST_KEY)
        assert logging.getLogger("httpx2").level == logging.WARNING
        assert logging.getLogger("httpcore2").level == logging.WARNING

    def test_the_filter_is_attached_to_the_root_handlers_and_the_named_loggers(
        self, stream_handler: tuple[logging.Handler, io.StringIO]
    ) -> None:
        handler, _ = stream_handler
        install_log_redaction(TEST_KEY)
        for target in (
            handler,
            logging.getLogger("httpx2"),
            logging.getLogger("httpcore2"),
            logging.getLogger("mcp"),
        ):
            assert any(isinstance(f, RedactingFilter) for f in target.filters)

    def test_handlers_of_the_named_loggers_get_it_too(self) -> None:
        own = logging.StreamHandler(io.StringIO())
        mcp_logger = logging.getLogger("mcp")
        mcp_logger.addHandler(own)
        try:
            install_log_redaction(TEST_KEY)
            assert any(isinstance(f, RedactingFilter) for f in own.filters)
        finally:
            mcp_logger.removeHandler(own)

    def test_a_logged_url_is_masked_end_to_end(
        self, stream_handler: tuple[logging.Handler, io.StringIO]
    ) -> None:
        _, stream = stream_handler
        install_log_redaction(TEST_KEY)
        logging.getLogger("some.module").warning(
            "GET https://eodhd.com/api/eod/ACME.US?api_token=%s", TEST_KEY
        )
        logging.getLogger("httpx2").warning("proxy said %s", TEST_KEY)
        assert TEST_KEY not in stream.getvalue()
        assert "api_token=***" in stream.getvalue()

    def test_an_info_line_from_httpx2_is_masked_even_if_the_level_is_lowered_again(
        self, stream_handler: tuple[logging.Handler, io.StringIO]
    ) -> None:
        _, stream = stream_handler
        install_log_redaction(TEST_KEY)
        httpx_logger = logging.getLogger("httpx2")
        httpx_logger.setLevel(logging.INFO)
        httpx_logger.info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            httpx2.URL(f"https://eodhd.test/api/eod/ACME.US?api_token={TEST_KEY}"),
            "HTTP/1.1",
            200,
            "OK",
        )
        output = stream.getvalue()
        assert "HTTP Request: GET" in output
        assert TEST_KEY not in output

    def test_a_traceback_is_masked_end_to_end(
        self, stream_handler: tuple[logging.Handler, io.StringIO]
    ) -> None:
        _, stream = stream_handler
        install_log_redaction(TEST_KEY)
        try:
            raise RuntimeError(f"url was https://eodhd.test/api/x?api_token={TEST_KEY}")
        except RuntimeError:
            logging.getLogger("mcp.server").exception("tool failed")
        assert "RuntimeError" in stream.getvalue()
        assert TEST_KEY not in stream.getvalue()

    def test_installing_twice_keeps_one_filter_that_knows_both_secrets(
        self, stream_handler: tuple[logging.Handler, io.StringIO]
    ) -> None:
        handler, stream = stream_handler
        install_log_redaction(TEST_KEY)
        install_log_redaction("second-key-9876543210")
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
        logging.getLogger("x").warning("%s and %s", TEST_KEY, "second-key-9876543210")
        assert stream.getvalue().strip().endswith(f"{MASK} and {MASK}")

    def test_it_works_without_a_key(self, stream_handler: tuple[logging.Handler, io.StringIO]) -> None:
        _, stream = stream_handler
        install_log_redaction(None)
        logging.getLogger("x").warning("url ?api_token=abc123 done")
        assert "abc123" not in stream.getvalue()
        assert "done" in stream.getvalue()
