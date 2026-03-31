"""Sphinx configuration for sensors2mqtt documentation."""

from importlib.metadata import version as get_version

project = "sensors2mqtt"
author = "Tim Ansell"
release = get_version("sensors2mqtt")
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
]

myst_enable_extensions = [
    "colon_fence",
    "fieldlist",
]

templates_path = ["_templates"]
exclude_patterns = ["_build"]

html_theme = "sphinx_rtd_theme"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}
