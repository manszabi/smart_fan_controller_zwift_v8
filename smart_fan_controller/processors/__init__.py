"""Async processors – zone calculation, cooldown and dropout detection.

This package contains the asyncio tasks that:
- process power data (validation, averaging, zone calculation)
- process HR data (validation, averaging, zone calculation)
- combine zones (power + HR) and apply the cooldown
- detect dropouts and send Z0

Modules:
- processors.py: the async tasks (power, HR, zone, dropout) + guarded task wrapper
"""
from __future__ import annotations

from .processors import (
    power_processor_task,
    hr_processor_task,
    zone_controller_task,
    dropout_checker_task,
    _guarded_task,
)

__all__ = [
    "power_processor_task",
    "hr_processor_task",
    "zone_controller_task",
    "dropout_checker_task",
    "_guarded_task",
]
