"""Recompile locale/{lang}/LC_MESSAGES/messages.po -> messages.mo after hand-editing a .po
file. The compiled .mo is what babel.support.Translations actually loads at runtime (see
web.py's i18n section) -- .po is just the human-editable source, and the two can drift if you
forget this step. Run after any .po edit:

    uv run python scripts/compile_locale.py
"""
import os

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "pumice_server", "locale")
DOMAIN = "messages"


def main():
    for lang in sorted(os.listdir(LOCALE_DIR)):
        po_path = os.path.join(LOCALE_DIR, lang, "LC_MESSAGES", f"{DOMAIN}.po")
        if not os.path.isfile(po_path):
            continue
        with open(po_path, "r", encoding="utf-8") as f:
            catalog = read_po(f, locale=lang, domain=DOMAIN)
        mo_path = os.path.join(LOCALE_DIR, lang, "LC_MESSAGES", f"{DOMAIN}.mo")
        with open(mo_path, "wb") as f:
            write_mo(f, catalog)
        print(f"compiled {po_path} -> {mo_path}")


if __name__ == "__main__":
    main()
