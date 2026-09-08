"""Console script entry point.

The application itself lives in :mod:`opsmith.cli.app`; this module keeps the
``opsmith = "opsmith.main:app"`` entry point in ``pyproject.toml`` resolving.
"""

from opsmith.cli.app import app

__all__ = ["app"]
