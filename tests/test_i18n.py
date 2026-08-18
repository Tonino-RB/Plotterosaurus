"""Every string the UI asks for must exist, in every language.

Written because 74 keys referenced by the markup existed in no catalog at all —
the entire camera modal, the notifications panel, the pen and motor buttons.
Nothing broke visibly: `I18N.applyStatic` only writes when a key resolves, so
the inline English in index.html survived and the gap was invisible in English.
Every other language just quietly stayed English in those panels.

That is exactly the kind of defect a test catches and a person does not, so the
check is mechanical: parse the markup for the keys it references, parse app.js
for the keys it looks up, and require all of them everywhere.
"""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
I18N_DIR = STATIC / "i18n"
FALLBACK = "en"

# t()/tn() calls whose key is built at runtime — `t("apierror." + code)` and
# friends. The prefix is checked instead of the literal, since the suffix comes
# from the server.
DYNAMIC_PREFIXES = ("status.", "apierror.", "layer_type.", "stage_status.")


def _catalogs() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(I18N_DIR.glob("*.json"))}


def _markup_keys() -> set[str]:
    """Keys referenced by data-i18n* attributes in index.html."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', html))


def _script_keys() -> set[str]:
    """Keys passed as a literal to t() in app.js.

    The negative lookbehind keeps `document.createElement("li")` out — it ends
    in `t("li")` and would otherwise register as a translation key.
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'(?<![A-Za-z0-9_$.])t\(\s*"([^"]+)"', js))
    return {k for k in keys if not k.startswith(DYNAMIC_PREFIXES)}


def _plural_keys() -> set[str]:
    """Keys passed to tn(), which resolves `<key>.<plural category>` rather
    than `<key>` itself (see i18n.js). `tn("job.layers", n)` is satisfied by
    job.layers.one / job.layers.other, so requiring the bare key would be
    wrong."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    return set(re.findall(r'(?<![A-Za-z0-9_$.])tn\(\s*"([^"]+)"', js))


def test_catalogs_exist():
    catalogs = _catalogs()
    assert FALLBACK in catalogs
    # The set the UI offers, per i18n.js SUPPORTED.
    assert len(catalogs) == 10, sorted(catalogs)


def test_every_key_the_ui_asks_for_is_in_english():
    """The fallback catalog has to be complete: a key missing here renders as
    the raw key from JS, and silently stays untranslated forever from markup."""
    en = _catalogs()[FALLBACK]
    missing = sorted(k for k in _markup_keys() | _script_keys() if k not in en)
    assert not missing, f"referenced by the UI but absent from en.json: {missing}"


def test_every_plural_key_has_its_forms():
    """A tn() key needs at least an `.other` form — that is what i18n.js falls
    back to for any plural category the active language does not define."""
    en = _catalogs()[FALLBACK]
    for key in sorted(_plural_keys()):
        assert f"{key}.other" in en, f"tn(\"{key}\") has no {key}.other in en.json"


@pytest.mark.parametrize("lang", sorted(p.stem for p in I18N_DIR.glob("*.json")))
def test_catalog_matches_english_exactly(lang):
    """Same keys in every language — no gaps, and no leftovers either.

    An extra key is as much a defect as a missing one: it means a string was
    renamed in en.json and the translations were left pointing at the old name.
    """
    catalogs = _catalogs()
    en, other = catalogs[FALLBACK], catalogs[lang]
    assert not (set(en) - set(other)), \
        f"{lang}.json is missing: {sorted(set(en) - set(other))}"
    assert not (set(other) - set(en)), \
        f"{lang}.json has keys en.json does not: {sorted(set(other) - set(en))}"


@pytest.mark.parametrize("lang", sorted(p.stem for p in I18N_DIR.glob("*.json")))
def test_placeholders_survive_translation(lang):
    """`{count}`, `{dx}`, `{message}` and friends are interpolated by name (see
    i18n.js), so a translation that drops or renames one loses the value —
    silently, since the literal `{name}` is what the user ends up reading."""
    catalogs = _catalogs()
    en, other = catalogs[FALLBACK], catalogs[lang]
    for key, english in en.items():
        expected = set(re.findall(r"\{(\w+)\}", english))
        actual = set(re.findall(r"\{(\w+)\}", other[key]))
        assert expected == actual, (
            f"{lang}.json[{key}] placeholders {sorted(actual)} "
            f"!= en {sorted(expected)}")


@pytest.mark.parametrize("lang", sorted(p.stem for p in I18N_DIR.glob("*.json")))
def test_no_empty_strings(lang):
    blank = sorted(k for k, v in _catalogs()[lang].items() if not str(v).strip())
    assert not blank, f"{lang}.json has empty values: {blank}"
