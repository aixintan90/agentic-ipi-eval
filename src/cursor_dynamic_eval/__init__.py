"""Cursor V3 dynamic call-chain depth evaluator."""

from .core.depth_judge import judge
from .core.events import Event
from .core.run_context import RunContext

__all__ = ["Event", "RunContext", "judge"]
__version__ = "1.0.4"
