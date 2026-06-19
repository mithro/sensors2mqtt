try:
    from sensors2mqtt._version import __version__, __version_tuple__
except ImportError:
    # _version.py is generated at build time by hatch-vcs (and is gitignored).
    # A raw source checkout run without a build step — e.g. the CI integration
    # job's `PYTHONPATH=src python -m pytest` — has no such file, so fall back to
    # a placeholder rather than failing to import the package. Built artifacts
    # (PyPI wheel, .deb) always ship a real _version.py.
    __version__ = "0+unknown"
    __version_tuple__ = (0, 0, "unknown")

__all__ = ["__version__", "__version_tuple__"]
