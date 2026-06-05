"""Zwift API polling segédprocessz – moduláris felbontás.

A Zwift HTTPS API-t kérdezi le és az adatokat UDP-n továbbítja a fő appnak.
Külön processzként (akár külön ablakban) fut, a settings.json ``zwift_api``
szekciójából konfigurálva.

Almodulok:
  - decoder:  ProtobufDecoder + PlayerState dekódolás
  - api:      ZwiftAuth (OAuth2) + ZwiftAPIClient (REST)
  - runtime:  ZwiftDataStore, UDPBroadcaster, run_polling_loop
  - logsetup: saját loggolás (zwift_api_polling.log)
  - __main__: belépési pont (settings.json betöltés, CLI)
"""
from __future__ import annotations

from .api import RateLimitError, ZwiftAPIClient, ZwiftAuth
from .decoder import ProtobufDecoder, _parse_protobuf_player_state
from .runtime import (
    BROADCAST_HOST,
    BROADCAST_PORT,
    DEFAULT_POLL_INTERVAL,
    UDPBroadcaster,
    ZwiftDataStore,
    run_polling_loop,
)

__all__ = [
    "ProtobufDecoder",
    "_parse_protobuf_player_state",
    "ZwiftAuth",
    "ZwiftAPIClient",
    "RateLimitError",
    "ZwiftDataStore",
    "UDPBroadcaster",
    "run_polling_loop",
    "BROADCAST_HOST",
    "BROADCAST_PORT",
    "DEFAULT_POLL_INTERVAL",
]
