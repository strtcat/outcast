# =============================================================================
# i18n.py — Outcast · lightweight translation engine
# -----------------------------------------------------------------------------
# A tiny, dependency-free localization layer. Translations live in JSON
# catalogs (one file per language) under a "locales" directory. English ("en")
# is the base language and the fallback for any missing key.
#
# Adding a new language is intentionally trivial and requires no code changes:
#   1. Copy locales/en.json to locales/<code>.json (e.g. locales/fr.json).
#   2. Translate the values and set the "_meta" block ("code" and "name").
#   3. The new language shows up automatically in Settings → Language.
#
# Strings are looked up by key and formatted with str.format(**kwargs), so
# placeholders use the {name} style, e.g. tr("status.deleted", title=t).
# =============================================================================

import json
import os
from pathlib import Path

# Base / fallback language. Always expected to be present and complete.
BASE_LANGUAGE = "en"

# Candidate directories that may hold the locale catalogs, in priority order.
# The first one that exists wins. This lets the app run straight from a source
# checkout (locales next to this file) as well as from a system install.
_LOCALE_SEARCH_PATHS = [
    Path(os.environ["OUTCAST_LOCALES"]) if os.environ.get("OUTCAST_LOCALES") else None,
    Path(__file__).resolve().parent / "locales",
    Path("/usr/share/outcast/locales"),
    Path("/usr/lib/outcast/locales"),
]


def _find_locales_dir() -> Path | None:
    for cand in _LOCALE_SEARCH_PATHS:
        if cand and cand.is_dir():
            return cand
    return None


class Translator:
    """Holds the loaded catalogs and the active language."""

    def __init__(self):
        self._locales_dir = _find_locales_dir()
        self._catalogs: dict[str, dict] = {}      # code -> {key: value}
        self._meta: dict[str, str] = {}           # code -> display name
        self._language = BASE_LANGUAGE
        self._load_all()

    # -- loading -----------------------------------------------------------
    def _load_all(self):
        if not self._locales_dir:
            # No catalogs found: fall back to returning keys verbatim.
            return
        for path in sorted(self._locales_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            meta = data.get("_meta", {})
            code = meta.get("code") or path.stem
            name = meta.get("name") or code
            self._catalogs[code] = data
            self._meta[code] = name

    # -- language management ----------------------------------------------
    def available_languages(self) -> list[tuple[str, str]]:
        """Returns [(code, display_name), …] sorted with the base language first."""
        items = sorted(self._meta.items(), key=lambda kv: (kv[0] != BASE_LANGUAGE, kv[1]))
        return items

    def has_language(self, code: str) -> bool:
        return code in self._catalogs

    def set_language(self, code: str):
        if code in self._catalogs:
            self._language = code

    def get_language(self) -> str:
        return self._language

    def language_name(self, code: str) -> str:
        return self._meta.get(code, code)

    # -- lookup ------------------------------------------------------------
    def tr(self, key: str, **kwargs) -> str:
        """Translate `key` into the active language.

        Falls back to the base language, then to the key itself. Any {named}
        placeholders are filled from kwargs; formatting errors degrade
        gracefully to the unformatted template so the UI never crashes.
        """
        template = (
            self._catalogs.get(self._language, {}).get(key)
            or self._catalogs.get(BASE_LANGUAGE, {}).get(key)
            or key
        )
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template


# Module-level singleton plus convenience wrappers, so callers can simply do
# `from i18n import tr`.
_TRANSLATOR = Translator()


def tr(key: str, **kwargs) -> str:
    return _TRANSLATOR.tr(key, **kwargs)


def set_language(code: str):
    _TRANSLATOR.set_language(code)


def get_language() -> str:
    return _TRANSLATOR.get_language()


def available_languages() -> list[tuple[str, str]]:
    return _TRANSLATOR.available_languages()


def language_name(code: str) -> str:
    return _TRANSLATOR.language_name(code)


def has_language(code: str) -> bool:
    return _TRANSLATOR.has_language(code)
