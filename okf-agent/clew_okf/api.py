"""Compatibility import; implementation: clew_okf.interfaces.api."""

import sys

from clew_okf.interfaces import api as _implementation

sys.modules[__name__] = _implementation
