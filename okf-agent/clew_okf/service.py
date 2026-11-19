"""Compatibility import; implementation: clew_okf.runtime.service."""

import sys

from clew_okf.runtime import service as _implementation

sys.modules[__name__] = _implementation
