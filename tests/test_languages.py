"""The web interface's languages: which are on, the default, following the browser, and the public config that says so."""

from __future__ import annotations

from ipping_backend.runtime_settings import LANGUAGES

from .conftest import Api

SETTINGS = "/api/v1/admin/settings"


def test_every_language_is_on_by_default_and_the_public_config_says_so(api: Api) -> None:
    config = api.get("/api/v1/config")
    assert (
        config.status_code == 200
    )  # no session, no sign-in: the page needs it before anything else
    assert config.json() == {
        "languages": {"enabled": list(LANGUAGES), "default": "en", "auto_detect": True}
    }


def test_an_administrator_chooses_the_languages_the_default_and_whether_to_follow_the_browser(
    api: Api,
) -> None:
    api.become_admin()
    changed = api.patch(
        SETTINGS,
        {
            "languages_enabled": ["zh", "en", "es"],
            "default_language": "zh",
            "language_auto_detect": False,
        },
    )
    assert changed.status_code == 200, changed.text
    # kept once each, in the interface's own order
    assert changed.json()["languages_enabled"] == ["en", "zh", "es"]
    api.client.cookies.clear()
    assert api.get("/api/v1/config").json() == {
        "languages": {"enabled": ["en", "zh", "es"], "default": "zh", "auto_detect": False}
    }


def test_settings_that_contradict_each_other_are_refused_and_nothing_is_written(api: Api) -> None:
    api.become_admin()
    for body in (
        {"languages_enabled": []},
        {"languages_enabled": ["en", "klingon"]},
        {"default_language": "klingon"},
        {"languages_enabled": ["en", "fr"], "default_language": "de"},  # the default must be on
        {"languages_enabled": ["fr"]},  # and switching off the current default is the same mistake
    ):
        refused = api.patch(SETTINGS, body)
        assert refused.status_code == 422, body
    now = api.get(SETTINGS).json()
    assert now["languages_enabled"] == list(LANGUAGES) and now["default_language"] == "en"

    # a default that is on, in one request with the languages, is fine
    assert (
        api.patch(SETTINGS, {"languages_enabled": ["fr"], "default_language": "fr"}).status_code
        == 200
    )
    assert api.get("/api/v1/config").json()["languages"]["enabled"] == ["fr"]


def test_the_language_choice_is_in_the_audit_trail(api: Api) -> None:
    api.become_admin()
    api.patch(SETTINGS, {"language_auto_detect": False})
    events = [e for e in api.get("/api/v1/admin/audit").json() if e["action"] == "settings.updated"]
    assert events and "language_auto_detect" in events[0]["data"]["fields"]
