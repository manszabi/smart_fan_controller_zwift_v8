"""Aszinkron processzorok – zóna-számítás, cooldown és dropout detektálás.

Ez a csomag az asyncio task-okat tartalmazza, amelyek:
- Power adatok feldolgozása (validálás, átlagolás, zóna számítás)
- HR adatok feldolgozása (validálás, átlagolás, zóna számítás)
- Zóna kombinálás (power + HR), cooldown alkalmazása
- Dropout detektálás és Z0 küldése

Modulok:
- processors.py: Az 5 async task (power, HR, zóna, dropout) + guarded task wrapper
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
