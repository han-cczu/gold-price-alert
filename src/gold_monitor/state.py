"""Compatibility import for the former shared-state module.

Resources now belong to app.state.runtime. Importing this module creates no
resources or database tables.
"""

from .dependencies import require_admin_dep

__all__ = ["require_admin_dep"]
