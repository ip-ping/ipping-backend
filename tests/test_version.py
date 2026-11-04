from __future__ import annotations

import tomllib
from pathlib import Path

from ipping_backend import __version__


def test_package_version_matches_pyproject() -> None:
    """The release workflow checks the tag against pyproject.toml, and `--version` and the OpenAPI
    document report __version__. They must never drift apart."""
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    )
    assert pyproject["project"]["version"] == __version__
