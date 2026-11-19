"""Compatibility import; implementation: clew_okf.providers.llm."""

import sys

from clew_okf.providers import llm as _implementation

sys.modules[__name__] = _implementation
