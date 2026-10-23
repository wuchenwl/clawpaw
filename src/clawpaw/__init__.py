"""Windows desktop automation with a reusable Python API and JSONL transport."""

from .browser import Browser
from .desktop import Desktop

__all__ = ["Browser", "Desktop"]
__version__ = "0.2.5"
