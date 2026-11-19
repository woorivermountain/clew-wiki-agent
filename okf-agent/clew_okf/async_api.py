"""Compatibility import; implementation: clew_okf.interfaces.async_api."""

import sys

from clew_okf.interfaces import async_api as _implementation

sys.modules[__name__] = _implementation
