"""Szálbiztos cooldown vezérlő – zóna csökkentés késleltetésre.

A CooldownController kezeli a cooldown logikát: amikor a zóna csökken,
nem vált azonnal, hanem cooldown_seconds másodpercig vár. Zóna növelésekor
azonnal vált. Adaptív módosítások: nagy esés vagy 0W → felezés, pending
emelkedés → duplázás.

Ez az osztály szálbiztos: threading.Lock védi az állapotot. Nincs Qt/BLE/IO
függőség, csak time és threading.

Megjegyzés: a logging "user" szinten történik, amelyet az alkalmazás
setup_logging() függvénye konfigurál.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

user_logger = logging.getLogger("user")


class CooldownController:
    """Cooldown logika kezelője zóna csökkentés esetén.

    Zóna csökkentésekor nem vált azonnal, hanem cooldown_seconds
    másodpercig vár. Zóna növelésekor azonnal vált, cooldown nélkül.

    Adaptív cooldown módosítások:
        - Nagy zónaesés (>= 2 szint) vagy 0W → cooldown felezés (gyorsabb leállás)
        - Pending zóna emelkedik → cooldown duplázás (lassabb emelkedés)

    Attribútumok:
        cooldown_seconds: A cooldown időtartama másodpercben.
        active: True, ha a cooldown timer fut.
        start_time: A cooldown indítási ideje (time.monotonic()).
        pending_zone: A cooldown lejárta után alkalmazandó zóna.
        can_halve: True, ha a cooldown felezés még elvégezhető.
        can_double: True, ha a cooldown duplázás még elvégezhető.
    """

    PRINT_INTERVAL = 10.0

    def __init__(self, cooldown_seconds: int) -> None:
        self._lock = threading.Lock()
        self.cooldown_seconds = cooldown_seconds
        self.active = False
        self.start_time = 0.0
        self.pending_zone: Optional[int] = None
        self.can_halve = True
        self.can_double = False
        self._last_print = 0.0

    def process(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Feldolgozza az új zóna javaslatot és alkalmazza a cooldown logikát.

        Args:
            current_zone: Az aktuális zóna (None = még nincs döntés).
            new_zone: Az új javasolt zóna (0–3).
            zero_immediate: True, ha 0W esetén azonnali leállás szükséges.

        Returns:
            A küldendő zóna szintje, ha változás szükséges; None egyébként.
        """
        with self._lock:
            return self._process_locked(current_zone, new_zone, zero_immediate)

    def _process_locked(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Belső process logika – lock alatt hívandó."""
        now = time.monotonic()

        # Első döntés – nincs előző zóna
        if current_zone is None:
            self._reset_locked()
            return new_zone

        # 0W azonnali leállás (zero_power_immediate=True)
        if new_zone == 0 and zero_immediate:
            if current_zone != 0:
                self._reset_locked()
                user_logger.info("✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                return 0
            return None

        # Aktív cooldown kezelése
        if self.active:
            return self._handle_active(current_zone, new_zone, now)

        # Nincs cooldown – normál zónaváltás logika
        if new_zone == current_zone:
            return None
        if new_zone > current_zone:
            return new_zone
        # cooldown_seconds == 0 → azonnali váltás, nincs cooldown
        if self.cooldown_seconds == 0:
            return new_zone
        # Zóna csökkentés → cooldown indul
        return self._start(current_zone, new_zone, now)

    def _start(self, current_zone: int, new_zone: int, now: float) -> Optional[int]:
        """Cooldown indítása zóna csökkentésnél."""
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
    ) -> Optional[int]:
        """Aktív cooldown feldolgozása – lock alatt hívandó."""
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
        """Felezi a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = remaining / 2
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_halve = False
        self.can_double = True
        user_logger.info(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s")

    def _double(self, now: float) -> None:
        """Duplázza a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = min(remaining * 2, float(self.cooldown_seconds))
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_double = False
        self.can_halve = True
        user_logger.info(f"🕐 Cooldown duplázva: {remaining:.0f}s → {new_remaining:.0f}s")

    def reset(self) -> None:
        """Törli a cooldown állapotát (publikus API, szálbiztos)."""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """Törli a cooldown állapotát – lock alatt hívandó."""
        self.active = False
        self.pending_zone = None
        self.can_halve = True
        self.can_double = False

    def snapshot(self) -> Tuple[bool, float]:
        """Szálbiztos pillanatfelvétel a HUD számára.

        Returns:
            (active, remaining_seconds) tuple.
        """
        with self._lock:
            if not self.active:
                return False, 0.0
            remaining = max(0.0, self.cooldown_seconds - (time.monotonic() - self.start_time))
            return True, remaining

    def __repr__(self) -> str:
        active, remaining = self.snapshot()
        return (
            f"CooldownController(active={active}, remaining={remaining:.1f}s, "
            f"pending_zone={self.pending_zone}, cooldown={self.cooldown_seconds}s)"
        )
