"""Application error types with one HTTP mapping each (see web.py)."""


class InvalidInput(ValueError):
    """A caller supplied a value the application cannot act on (HTTP 400)."""


class ConfigurationError(ValueError):
    """Persisted or environment configuration cannot be used (HTTP 503)."""
