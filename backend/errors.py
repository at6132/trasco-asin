"""Shared exceptions for the process pipeline."""


class JobCancelled(Exception):
    """Raised when a background process job is cancelled (cooperative)."""
