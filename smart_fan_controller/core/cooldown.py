"""Thread-safe cooldown controller – delays zone decreases.

CooldownController implements the cooldown logic: when the zone drops it
does not switch immediately but waits cooldown_seconds. Zone increases
switch immediately. Adaptive tweaks: a big drop or 0 W → halving, a
rising pending zone → doubling.

The class is thread-safe: a threading.Lock guards the state. No
Qt/BLE/IO dependencies, only time and threading.

Note: logging happens on the "user" level, configured by the
application's setup_logging() function.
"""
from __future__ import annotations

import logging
import threading
import time

user_logger = logging.getLogger("user")


class CooldownController:
    """Handles the cooldown logic on zone decreases.

    On a zone decrease it does not switch immediately but waits
    cooldown_seconds. On a zone increase it switches at once, without
    cooldown.

    Adaptive cooldown tweaks:
        - Big zone drop (>= 2 levels) or 0 W → cooldown halved (faster stop)
        - Pending zone rising → cooldown doubled (slower ramp-down)

    Attributes:
        cooldown_seconds: Duration of the cooldown in seconds.
        active: True while the cooldown timer runs.
        start_time: Start time of the cooldown (time.monotonic()).
        pending_zone: Zone to apply once the cooldown expires.
        can_halve: True while halving the cooldown is still allowed.
        can_double: True while doubling the cooldown is still allowed.
    """

    PRINT_INTERVAL = 10.0

    def __init__(self, cooldown_seconds: int) -> None:
        self._lock = threading.Lock()
        self.cooldown_seconds = cooldown_seconds
        self.active = False
        self.start_time = 0.0
        self.pending_zone: int | None = None
        self.can_halve = True
        self.can_double = False
        self._last_print = 0.0

    def process(
        self,
        current_zone: int | None,
        new_zone: int,
        zero_immediate: bool,
    ) -> int | None:
        """Process the new zone proposal and apply the cooldown logic.

        Args:
            current_zone: The current zone (None = no decision yet).
            new_zone: The newly proposed zone (0–3).
            zero_immediate: True when 0 W requires an immediate stop.

        Returns:
            The zone level to send when a change is needed; None otherwise.
        """
        with self._lock:
            return self._process_locked(current_zone, new_zone, zero_immediate)

    def _process_locked(
        self,
        current_zone: int | None,
        new_zone: int,
        zero_immediate: bool,
    ) -> int | None:
        """Internal process logic – must be called with the lock held."""
        now = time.monotonic()

        # First decision – no previous zone
        if current_zone is None:
            self._reset_locked()
            return new_zone

        # 0 W immediate stop (zero_power_immediate=True)
        if new_zone == 0 and zero_immediate:
            if current_zone != 0:
                self._reset_locked()
                user_logger.info("✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                return 0
            return None

        # Handle an active cooldown
        if self.active:
            return self._handle_active(current_zone, new_zone, now)

        # No cooldown – normal zone switch logic
        if new_zone == current_zone:
            return None
        if new_zone > current_zone:
            return new_zone
        # cooldown_seconds == 0 → immediate switch, no cooldown
        if self.cooldown_seconds == 0:
            return new_zone
        # Zone decrease → cooldown starts
        return self._start(current_zone, new_zone, now)

    def _start(self, current_zone: int, new_zone: int, now: float) -> int | None:
        """Start the cooldown on a zone decrease."""
        self.active = True
        self.start_time = now
        self.pending_zone = new_zone
        self.can_halve = True
        self.can_double = False
        self._last_print = now
        user_logger.info(
            f"🕐 Cooldown indítva: {self.cooldown_seconds}s várakozás (cél: {new_zone})"
        )
        if new_zone == 0 or (current_zone - new_zone >= 2):
            self._halve(now)
        return None

    def _handle_active(
        self, current_zone: int, new_zone: int, now: float
    ) -> int | None:
        """Process an active cooldown – must be called with the lock held."""
        if new_zone >= current_zone:
            self._reset_locked()
            if new_zone > current_zone:
                user_logger.info(f"✓ Teljesítmény emelkedés: cooldown törölve → zóna: {new_zone}")
                return new_zone
            return None

        elapsed = now - self.start_time

        if elapsed >= self.cooldown_seconds:
            target = new_zone
            self._reset_locked()
            if target != current_zone:
                user_logger.info(f"✓ Cooldown lejárt! Zóna váltás: {current_zone} → {target}")
                return target
            user_logger.info("✓ Cooldown lejárt, nincs zónaváltás (már a célzónában)")
            return None

        remaining = self.cooldown_seconds - elapsed

        if new_zone != self.pending_zone:
            old_pending = self.pending_zone
            self.pending_zone = new_zone
            if old_pending is not None and new_zone > old_pending and self.can_double:
                self._double(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                user_logger.info(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            elif (new_zone == 0 or (current_zone - new_zone >= 2)) and self.can_halve:
                self._halve(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                user_logger.info(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            else:
                user_logger.info(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            self._last_print = now
        elif now - self._last_print >= self.PRINT_INTERVAL:
            user_logger.info(
                f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó: {self.pending_zone})"
            )
            self._last_print = now

        return None

    def _halve(self, now: float) -> None:
        """Halve the remaining cooldown time."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = remaining / 2
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_halve = False
        self.can_double = True
        user_logger.info(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s")

    def _double(self, now: float) -> None:
        """Double the remaining cooldown time."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = min(remaining * 2, float(self.cooldown_seconds))
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_double = False
        self.can_halve = True
        user_logger.info(f"🕐 Cooldown duplázva: {remaining:.0f}s → {new_remaining:.0f}s")

    def reset(self) -> None:
        """Reset the cooldown state (public API, thread-safe)."""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """Reset the cooldown state – must be called with the lock held."""
        self.active = False
        self.pending_zone = None
        self.can_halve = True
        self.can_double = False

    def snapshot(self) -> tuple[bool, float]:
        """Thread-safe snapshot for the HUD.

        Returns:
            (active, remaining_seconds) tuple.
        """
        with self._lock:
            if not self.active:
                return False, 0.0
            remaining = max(0.0, self.cooldown_seconds - (time.monotonic() - self.start_time))
            return True, remaining

    def __repr__(self) -> str:
        with self._lock:
            if not self.active:
                active, remaining = False, 0.0
            else:
                remaining = max(0.0, self.cooldown_seconds - (time.monotonic() - self.start_time))
                active = True
            pending = self.pending_zone
            cd_seconds = self.cooldown_seconds
        return (
            f"CooldownController(active={active}, remaining={remaining:.1f}s, "
            f"pending_zone={pending}, cooldown={cd_seconds}s)"
        )
