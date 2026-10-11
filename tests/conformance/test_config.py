import pytest

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError


def test_defaults_need_no_environment():
    assert Settings.from_env({}).data_dir.name == "data"


def test_prefixed_variables_win():
    s = Settings.from_env({"CROCODILE_DATA_DIR": "/tmp/lake"})
    assert s.data_dir.as_posix() == "/tmp/lake"


def test_legacy_names_are_accepted_with_a_warning():
    with pytest.warns(DeprecationWarning, match="CRYPCODILE_DATA_DIR"):
        s = Settings.from_env({"CRYPCODILE_DATA_DIR": "/tmp/old"})
    assert s.data_dir.as_posix() == "/tmp/old"


def test_prefixed_name_beats_legacy_without_warning(recwarn):
    s = Settings.from_env({"CROCODILE_DATA_DIR": "/tmp/new", "STOCKODILE_DATA_DIR": "/tmp/old"})
    assert s.data_dir.as_posix() == "/tmp/new"
    assert not [w for w in recwarn if issubclass(w.category, DeprecationWarning)]


def test_conflicting_legacy_names_are_an_error():
    with pytest.raises(ConfigError, match="conflicting"):
        Settings.from_env({"CRYPCODILE_DATA_DIR": "/a", "STOCKODILE_DATA_DIR": "/b"})


def test_agreeing_legacy_names_are_not_an_error():
    with pytest.warns(DeprecationWarning):
        s = Settings.from_env({"CRYPCODILE_DATA_DIR": "/same", "STOCKODILE_DATA_DIR": "/same"})
    assert s.data_dir.as_posix() == "/same"


def test_known_names_are_enumerable():
    names = Settings.known_names()
    assert "CROCODILE_DATA_DIR" in names
    assert all(n.startswith("CROCODILE_") for n in names)


def test_secrets_do_not_leak_through_repr():
    s = Settings.from_env({"CROCODILE_ALPACA_API_SECRET": "hunter2"})
    assert "hunter2" not in repr(s)
    assert s.alpaca_api_secret == "hunter2"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "  true  "])
def test_every_truthy_spelling_parses(raw):
    assert Settings.from_env({"CROCODILE_TRUST_FORWARDED_FOR": raw}).trust_forwarded_for is True


@pytest.mark.parametrize(
    "raw", ["0", "false", "FALSE", "False", "no", "NO", "off", "OFF", "  false  "]
)
def test_every_falsy_spelling_parses(raw):
    assert Settings.from_env({"CROCODILE_TRUST_FORWARDED_FOR": raw}).trust_forwarded_for is False


def test_boolean_defaults_are_bools_not_strings():
    s = Settings.from_env({})
    assert s.trust_forwarded_for is False
    assert s.finnhub_free_tier is True


def test_an_unparseable_boolean_is_an_error_not_a_silent_false():
    with pytest.raises(ConfigError, match=r"CROCODILE_TRUST_FORWARDED_FOR.*maybe"):
        Settings.from_env({"CROCODILE_TRUST_FORWARDED_FOR": "maybe"})


def test_a_bad_boolean_names_the_spelling_that_supplied_it():
    with pytest.raises(ConfigError, match=r"CRYPCODILE_FINNHUB_FREE_TIER"):
        Settings.from_env({"CRYPCODILE_FINNHUB_FREE_TIER": "sometimes"})


def test_sec_user_agent_defaults_to_none_rather_than_a_fabricated_contact():
    assert Settings.from_env({}).sec_user_agent is None


def test_sec_user_agent_passes_through_when_set():
    s = Settings.from_env({"CROCODILE_SEC_USER_AGENT": "Acme/1.0 (ops@acme.example)"})
    assert s.sec_user_agent == "Acme/1.0 (ops@acme.example)"


def test_binance_sbe_settings_and_key_redaction():
    settings = Settings.from_env(
        {
            "CROCODILE_BINANCE_MARKET": "spot",
            "CROCODILE_BINANCE_WIRE_FORMAT": "sbe",
            "CROCODILE_BINANCE_SBE_API_KEY": "SENTINEL_KEY",
        }
    )
    assert settings.binance_wire_format == "sbe"
    assert settings.binance_market == "spot"
    assert settings.binance_sbe_api_key == "SENTINEL_KEY"
    assert "SENTINEL_KEY" not in repr(settings)
