"""Bounded in-memory buffer for the log records emitted before setup.

Both the main application and the ``zwift_api`` helper process have to
log before they know whether logging is enabled at all – the
``global_settings.logging`` flag only becomes known once settings.json is
read. Those early records are held here until the real handlers exist,
then either replayed onto them or dropped.

Why not ``logging.handlers.MemoryHandler``: its capacity is not
enforceable in this configuration. ``MemoryHandler.flush()`` drains the
buffer only ``if self.target`` – with no target (there is none until the
real handlers are built) the buffer is never cleared, so reaching the
capacity re-triggers a no-op flush on every further record and the list
grows for as long as buffering lasts. The cap here is real.

Placement: a package-root leaf module importing nothing from the project,
so the ``zwift_api`` helper process can share it without pulling in the
``core`` domain layer.
"""
from __future__ import annotations

import logging

__all__ = ["EarlyLogBuffer", "DEFAULT_CAPACITY"]

# Settings validation emits at most one warning per field, and the schema
# has well under a hundred fields – a thousand records is far past any
# legitimate startup while still bounding the buffer to a few hundred KB.
DEFAULT_CAPACITY = 1000


class EarlyLogBuffer(logging.Handler):
    """Collects log records in memory, up to ``capacity`` of them.

    The OLDEST records are kept rather than the newest: this buffer holds
    startup/validation output, where the first messages name the first
    broken thing and the rest are usually more of the same. Records past
    the cap are counted, and :meth:`replay` reports the count, so a
    truncated buffer is visible instead of silently short.

    ``emit`` is called from ``logging.Handler.handle`` with the handler
    lock held, so the append needs no extra locking.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        # Level NOTSET: the owning logger decides what reaches the buffer,
        # exactly as the MemoryHandler it replaces did.
        super().__init__()
        self.capacity = max(1, int(capacity))
        self.records: list[logging.LogRecord] = []
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) < self.capacity:
            self.records.append(record)
        else:
            self.dropped += 1

    def replay(self, target: logging.Logger) -> None:
        """Replay the buffered records onto ``target``, then release them.

        The buffer detaches itself from the logger first: replaying
        through ``Logger.handle`` while still attached would feed every
        record straight back into this handler.
        """
        target.removeHandler(self)
        for record in self.records:
            target.handle(record)
        if self.dropped:
            target.warning(
                "⚠ További %d indulási naplóbejegyzés nem fért a korai "
                "pufferbe (max %d) – ezek elvesztek.",
                self.dropped, self.capacity,
            )
        self.discard(target)

    def discard(self, target: logging.Logger | None = None) -> None:
        """Drop the buffered records and close the handler."""
        if target is not None:
            target.removeHandler(self)
        self.records.clear()
        self.dropped = 0
        self.close()

    def __repr__(self) -> str:
        return (
            f"EarlyLogBuffer(records={len(self.records)}, "
            f"dropped={self.dropped}, capacity={self.capacity})"
        )
