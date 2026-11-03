from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from eodhd_guard_mcp.config import DEFAULT_BASE_URL, Settings, default_state_dir
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from tests.helpers import TEST_KEY


def from_env(tmp_path: Path, **env: str) -> Settings:
    """Settings from a clean environment whose state dir is under ``tmp_path``."""
    return Settings.from_env({"XDG_STATE_HOME": str(tmp_path), **env})


def test_defaults(tmp_path: Path) -> None:
    settings = from_env(tmp_path)
    assert settings.api_key is None
    assert settings.base_url == DEFAULT_BASE_URL == "https://eodhd.com/api"
    assert settings.state_dir == tmp_path / "eodhd-guard-mcp"
    assert settings.daily_budget == 1000
    assert settings.cache_enabled is True
    assert settings.stale_sessions == 3
    assert settings.timeout_s == 30.0
    assert settings.max_retries == 2
    assert settings.allow_uncovered is False
    assert settings.log_level == "WARNING"


def test_every_variable_is_read(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "EODHD_API_KEY": TEST_KEY,
            "EODHD_GUARD_DAILY_BUDGET": "250",
            "EODHD_GUARD_STATE_DIR": str(tmp_path / "state"),
            "EODHD_GUARD_CACHE": "off",
            "EODHD_GUARD_STALE_SESSIONS": "5",
            "EODHD_GUARD_TIMEOUT": "12.5",
            "EODHD_GUARD_MAX_RETRIES": "4",
            "EODHD_GUARD_ALLOW_UNCOVERED": "yes",
            "EODHD_GUARD_BASE_URL": "https://proxy.example.test/eodhd/",
            "EODHD_GUARD_LOG_LEVEL": "debug",
        }
    )
    assert settings == Settings(
        api_key=TEST_KEY,
        base_url="https://proxy.example.test/eodhd",
        state_dir=tmp_path / "state",
        daily_budget=250,
        cache_enabled=False,
        stale_sessions=5,
        timeout_s=12.5,
        max_retries=4,
        allow_uncovered=True,
        log_level="DEBUG",
    )


def test_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EODHD_API_KEY", TEST_KEY)
    monkeypatch.setenv("EODHD_GUARD_DAILY_BUDGET", "7")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    settings = Settings.from_env()
    assert settings.api_key == TEST_KEY
    assert settings.daily_budget == 7
    assert settings.state_dir == tmp_path / "eodhd-guard-mcp"


def test_blank_values_count_as_unset(tmp_path: Path) -> None:
    settings = from_env(tmp_path, EODHD_API_KEY="  ", EODHD_GUARD_DAILY_BUDGET="", EODHD_GUARD_CACHE=" ")
    assert settings.api_key is None
    assert settings.daily_budget == 1000
    assert settings.cache_enabled is True


def test_zero_budget_means_unlimited(tmp_path: Path) -> None:
    assert from_env(tmp_path, EODHD_GUARD_DAILY_BUDGET="0").daily_budget == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("on", True),
        ("ON", True),
        ("1", True),
        ("true", True),
        ("Yes", True),
        ("off", False),
        ("0", False),
        ("False", False),
        ("no", False),
    ],
)
def test_boolean_spellings(tmp_path: Path, raw: str, expected: bool) -> None:
    assert from_env(tmp_path, EODHD_GUARD_CACHE=raw).cache_enabled is expected
    assert from_env(tmp_path, EODHD_GUARD_ALLOW_UNCOVERED=raw).allow_uncovered is expected


class TestApiKey:
    def test_is_stripped(self, tmp_path: Path) -> None:
        assert from_env(tmp_path, EODHD_API_KEY=f"  {TEST_KEY}\n").api_key == TEST_KEY

    def test_comes_from_the_key_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(f"\n{TEST_KEY}\n", encoding="utf-8")
        assert from_env(tmp_path, EODHD_API_KEY_FILE=str(key_file)).api_key == TEST_KEY

    def test_a_byte_order_mark_is_ignored(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_bytes(b"\xef\xbb\xbf" + TEST_KEY.encode())
        assert from_env(tmp_path, EODHD_API_KEY_FILE=str(key_file)).api_key == TEST_KEY

    def test_the_variable_wins_over_the_file(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text("file-key-0123456789", encoding="utf-8")
        settings = from_env(tmp_path, EODHD_API_KEY=TEST_KEY, EODHD_API_KEY_FILE=str(key_file))
        assert settings.api_key == TEST_KEY

    def test_the_file_is_not_read_when_the_variable_is_set(self, tmp_path: Path) -> None:
        settings = from_env(tmp_path, EODHD_API_KEY=TEST_KEY, EODHD_API_KEY_FILE=str(tmp_path / "absent"))
        assert settings.api_key == TEST_KEY

    def test_home_is_expanded_in_the_file_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "k").write_text(TEST_KEY, encoding="utf-8")
        assert from_env(tmp_path, EODHD_API_KEY_FILE="~/k").api_key == TEST_KEY

    def test_a_missing_file_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(GuardError) as caught:
            from_env(tmp_path, EODHD_API_KEY_FILE=str(tmp_path / "absent"))
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert "EODHD_API_KEY_FILE" in caught.value.message
        assert "cannot be read" in caught.value.message

    def test_a_directory_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(GuardError, match="EODHD_API_KEY_FILE"):
            from_env(tmp_path, EODHD_API_KEY_FILE=str(tmp_path))

    def test_an_empty_file_is_a_config_error(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.txt"
        key_file.write_text(" \n", encoding="utf-8")
        with pytest.raises(GuardError, match="is empty"):
            from_env(tmp_path, EODHD_API_KEY_FILE=str(key_file))

    def test_a_binary_file_is_a_config_error_that_does_not_quote_it(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.bin"
        key_file.write_bytes(b"\xff\xfe\x00 not text \xff")
        with pytest.raises(GuardError) as caught:
            from_env(tmp_path, EODHD_API_KEY_FILE=str(key_file))
        assert "not text" not in str(caught.value)
        assert "UnicodeDecodeError" in caught.value.message

    def test_no_exception_that_holds_the_file_content_is_chained(self, tmp_path: Path) -> None:
        key_file = tmp_path / "key.bin"
        key_file.write_bytes(b"\xff\xfe secret-material")
        with pytest.raises(GuardError) as caught:
            from_env(tmp_path, EODHD_API_KEY_FILE=str(key_file))
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


class TestRequireKey:
    def test_returns_the_key(self) -> None:
        assert Settings(api_key=TEST_KEY).require_key() == TEST_KEY

    @pytest.mark.parametrize("missing", [None, ""])
    def test_raises_a_config_error_without_one(self, missing: str | None) -> None:
        with pytest.raises(GuardError) as caught:
            Settings(api_key=missing).require_key()
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert "EODHD_API_KEY" in (caught.value.hint or "")


INVALID = [
    ("EODHD_GUARD_DAILY_BUDGET", "abc"),
    ("EODHD_GUARD_DAILY_BUDGET", "-1"),
    ("EODHD_GUARD_DAILY_BUDGET", "1.5"),
    ("EODHD_GUARD_STALE_SESSIONS", "0"),
    ("EODHD_GUARD_STALE_SESSIONS", "three"),
    ("EODHD_GUARD_TIMEOUT", "0"),
    ("EODHD_GUARD_TIMEOUT", "-3"),
    ("EODHD_GUARD_TIMEOUT", "soon"),
    ("EODHD_GUARD_TIMEOUT", "nan"),
    ("EODHD_GUARD_TIMEOUT", "inf"),
    ("EODHD_GUARD_MAX_RETRIES", "6"),
    ("EODHD_GUARD_MAX_RETRIES", "-1"),
    ("EODHD_GUARD_MAX_RETRIES", "two"),
    ("EODHD_GUARD_CACHE", "maybe"),
    ("EODHD_GUARD_ALLOW_UNCOVERED", "2"),
    ("EODHD_GUARD_LOG_LEVEL", "LOUD"),
]


@pytest.mark.parametrize(("name", "value"), INVALID)
def test_an_invalid_value_names_the_variable_and_what_is_accepted(
    tmp_path: Path, name: str, value: str
) -> None:
    with pytest.raises(GuardError) as caught:
        from_env(tmp_path, **{name: value})
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert caught.value.message.startswith(f"{name} must be ")
    assert repr(value) in caught.value.message


@pytest.mark.parametrize(
    "url",
    [
        "ftp://eodhd.com/api",
        "http://eodhd.com/api",
        "https://",
        "eodhd.com/api",
        "https://user:pass@eodhd.com/api",
        "https://eodhd.com/api?x=1",
        "https://eodhd.com/api#frag",
        "http://[::1/api",
    ],
)
def test_an_unsafe_base_url_is_rejected_without_being_repeated(tmp_path: Path, url: str) -> None:
    with pytest.raises(GuardError) as caught:
        from_env(tmp_path, EODHD_GUARD_BASE_URL=url)
    assert caught.value.message.startswith("EODHD_GUARD_BASE_URL must be an https URL")
    assert "; got" not in caught.value.message
    assert "user:pass" not in caught.value.message


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://eodhd.com/api/", "https://eodhd.com/api"),
        ("http://localhost:8080/api", "http://localhost:8080/api"),
        ("http://127.0.0.1:9000/api/", "http://127.0.0.1:9000/api"),
        ("http://[::1]:9000/api", "http://[::1]:9000/api"),
    ],
)
def test_base_url_forms_that_are_accepted(tmp_path: Path, url: str, expected: str) -> None:
    assert from_env(tmp_path, EODHD_GUARD_BASE_URL=url).base_url == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EODHD_GUARD_DAILY_BUDGET", "abc"),
        ("EODHD_GUARD_TIMEOUT", "soon"),
        ("EODHD_GUARD_BASE_URL", "http://[::1/api"),
    ],
)
def test_an_unparsable_value_chains_no_exception(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(GuardError) as caught:
        from_env(tmp_path, **{name: value})
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_an_invalid_value_never_repeats_the_key(tmp_path: Path) -> None:
    with pytest.raises(GuardError) as caught:
        from_env(tmp_path, EODHD_API_KEY=TEST_KEY, EODHD_GUARD_DAILY_BUDGET=TEST_KEY)
    assert TEST_KEY not in str(caught.value)
    assert "'***'" in caught.value.message


def test_log_level_is_normalised(tmp_path: Path) -> None:
    assert from_env(tmp_path, EODHD_GUARD_LOG_LEVEL="error").log_level == "ERROR"


class TestStateDir:
    def test_the_variable_wins_and_is_expanded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert from_env(tmp_path, EODHD_GUARD_STATE_DIR="~/state").state_dir == tmp_path / "state"

    def test_a_relative_path_is_made_absolute(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        assert from_env(tmp_path, EODHD_GUARD_STATE_DIR="rel").state_dir == tmp_path / "rel"

    def test_a_missing_home_is_a_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_home() -> Path:
            raise RuntimeError("no home")

        monkeypatch.setattr(Path, "home", no_home)
        with pytest.raises(GuardError, match="home directory") as caught:
            Settings.from_env({})
        assert "EODHD_GUARD_STATE_DIR" in (caught.value.hint or "")

    def test_a_default_settings_object_follows_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert Settings().state_dir == tmp_path / "eodhd-guard-mcp"


class TestDefaultStateDir:
    def test_xdg_state_home(self, tmp_path: Path) -> None:
        assert default_state_dir({"XDG_STATE_HOME": str(tmp_path)}, platform="linux") == (
            tmp_path / "eodhd-guard-mcp"
        )

    @pytest.mark.parametrize("xdg", ["", "   ", "relative/dir"])
    def test_an_empty_or_relative_xdg_value_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, xdg: str
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        expected = tmp_path / ".local" / "state" / "eodhd-guard-mcp"
        assert default_state_dir({"XDG_STATE_HOME": xdg}, platform="linux") == expected

    def test_home_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_state_dir({}, platform="darwin") == tmp_path / ".local" / "state" / "eodhd-guard-mcp"

    def test_windows_uses_local_app_data(self) -> None:
        path = default_state_dir({"LOCALAPPDATA": "C:/Users/ada/AppData/Local"}, platform="win32")
        assert path == Path("C:/Users/ada/AppData/Local") / "eodhd-guard-mcp"

    def test_windows_without_local_app_data(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        path = default_state_dir({}, platform="win32")
        assert path == tmp_path / "AppData" / "Local" / "eodhd-guard-mcp"

    def test_the_running_platform_is_the_default(self, tmp_path: Path) -> None:
        assert default_state_dir(
            {"XDG_STATE_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "w")}
        ).name == ("eodhd-guard-mcp")


class TestRepr:
    def test_the_key_is_masked(self) -> None:
        settings = Settings(api_key=TEST_KEY)
        for text in (repr(settings), str(settings), f"{settings!r}", f"{settings}"):
            assert TEST_KEY not in text
            assert "api_key='***'" in text

    def test_no_key_shows_none(self) -> None:
        assert "api_key=None" in repr(Settings(api_key=None))

    def test_the_other_fields_are_shown(self, tmp_path: Path) -> None:
        text = repr(Settings(api_key=TEST_KEY, state_dir=tmp_path, daily_budget=42))
        assert "daily_budget=42" in text
        assert str(tmp_path) in text


def test_settings_are_immutable() -> None:
    settings = Settings(api_key=TEST_KEY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.daily_budget = 1  # type: ignore[misc]
