from amadeus.cogs import (
    extension_to_module_name,
    get_configured_cog_extensions,
    is_static_extension,
    normalize_extension_name,
    parse_extension_list,
)


def test_normalize_extension_name_accepts_short_module_and_paths():
    assert normalize_extension_name("boost") == "cogs.boost"
    assert normalize_extension_name("cogs.boost") == "cogs.boost"
    assert normalize_extension_name("cogs/boost.py") == "cogs.boost"
    assert normalize_extension_name("") == ""


def test_parse_extension_list_splits_and_deduplicates():
    raw = "boost, cogs.bouncer; cogs/boost.py\nhoneypot"

    assert parse_extension_list(raw) == [
        "cogs.boost",
        "cogs.bouncer",
        "cogs.honeypot",
    ]


def test_static_extensions_are_detected_after_normalization():
    assert is_static_extension("cogs.amadeus_admin")
    assert is_static_extension("amadeus_owner")
    assert not is_static_extension("boost")


def test_get_configured_cog_extensions_empty_env_loads_no_dynamic_cogs(monkeypatch):
    monkeypatch.delenv("AMADEUS_COGS", raising=False)

    assert get_configured_cog_extensions() == []


def test_get_configured_cog_extensions_pairs_admin_cogs(monkeypatch):
    monkeypatch.setenv("AMADEUS_COGS", "boost activity")

    assert get_configured_cog_extensions() == [
        "cogs.boost",
        "cogs.boost_admin",
        "cogs.activity",
        "cogs.activity_admin",
    ]


def test_get_configured_cog_extensions_skips_static_cogs(monkeypatch):
    monkeypatch.setenv("AMADEUS_COGS", "amadeus_admin boost")

    assert get_configured_cog_extensions() == [
        "cogs.boost",
        "cogs.boost_admin",
    ]


def test_extension_to_module_name_returns_short_name():
    assert extension_to_module_name("cogs.bouncer") == "bouncer"
    assert extension_to_module_name("activity") == "activity"
