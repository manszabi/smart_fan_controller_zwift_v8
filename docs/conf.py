"""Sphinx konfiguráció – Smart Fan Controller API-referencia.

Generálás a projekt gyökeréből:
    pip install sphinx
    sphinx-build -b html docs docs/_build/html
"""
import os
import sys

# A csomag a repó gyökerében van (docs/ egy szinttel lejjebb)
sys.path.insert(0, os.path.abspath(".."))

from smart_fan_controller import __version__  # noqa: E402

# -- Projekt információk -----------------------------------------------------

project = "Smart Fan Controller"
author = "manszabi"
release = __version__
version = __version__
copyright = "2026, manszabi (MIT licenc)"

# -- Általános beállítások ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",      # docstringekből generált API-doksi
    "sphinx.ext.napoleon",     # Google-stílusú docstringek (Args/Returns)
    "sphinx.ext.viewcode",     # [source] linkek a forráskódra
    "sphinx.ext.intersphinx",  # linkek a Python stdlib doksijára
]

language = "hu"

# Az opcionális futásidejű függőségek nélkül is generálható legyen a doksi:
# az autodoc importáláskor mockolja őket. A 'requests' szándékosan nincs itt
# (kicsi, telepítése elvárható); a többiek nagyok vagy platformfüggőek.
autodoc_mock_imports = [
    "PySide6",
    "bleak",
    "openant",
    "pywinauto",
]

autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
# A modulok docstringje magyarázza a kontextust – kerüljön az oldal tetejére.
autodoc_class_signature = "mixed"

napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

templates_path = []
exclude_patterns = ["_build"]

# -- HTML kimenet ------------------------------------------------------------

html_theme = "alabaster"
html_theme_options = {
    "description": "Zwift-integrált okos ventilátor-vezérlő – API-referencia",
    "fixed_sidebar": True,
    "page_width": "1100px",
}
html_static_path = []
