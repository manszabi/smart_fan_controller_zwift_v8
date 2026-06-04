"""Throttle-olt konzol kiírás – nincs Qt/BLE/IO függőség.

A ConsolePrinter kezeli az üzeneteket: ugyanaz az üzenet nem jelenhet meg
túl sűrűn. Üzenetkulcsonként külön időzítőt tart, és csak akkor ír ki, ha
az interval eltelt az utolsó kiírás óta.
"""
from __future__ import annotations

import logging
import time
from typing import Dict

user_logger = logging.getLogger("user")


class ConsolePrinter:
    """Throttle-olt konzol kiírás – ugyanaz az üzenet nem jelenhet meg túl sűrűn.

    Minden üzenettípushoz (key) külön időzítőt tart. Az üzenet csak
    akkor kerül kiírásra, ha az utolsó kiírás óta legalább interval
    másodperc telt el.

    Megjegyzés: a metódus neve 'emit', hogy ne fedje el a beépített print()-et.

    Attribútumok:
        _last_times: Utolsó kiírás ideje üzenetkulcsonként.
    """

    def __init__(self) -> None:
        self._last_times: Dict[str, float] = {}

    def emit(self, key: str, message: str, interval: float = 1.0) -> bool:
        """Kiírja az üzenetet, ha az interval eltelt.

        Args:
            key: Egyedi kulcs az üzenet azonosításához (pl. "power_raw").
            message: A kiírandó szöveg.
            interval: Minimális másodpercek száma két azonos kulcsú kiírás között.

        Returns:
            True, ha az üzenet kiírásra kerül; False, ha throttle-olt.
        """
        now = time.monotonic()
        if now - self._last_times.get(key, 0.0) >= interval:
            user_logger.info(message)
            self._last_times[key] = now
            return True
        return False
