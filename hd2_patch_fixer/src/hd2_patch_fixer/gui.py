"""Compatibility entry point for callers using the previous GUI module."""

from .qt_gui import PatchFixerWindow, run

__all__ = ["PatchFixerWindow", "run"]
