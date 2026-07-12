"""Zwift API polling helper process – modular breakdown.

Polls the Zwift HTTPS API and forwards the data over UDP to the main
app. Runs as a separate process (optionally in its own window),
configured from the ``zwift_api`` section of settings.json.

Submodules:
  - decoder:  ProtobufDecoder + PlayerState decoding
  - api:      ZwiftAuth (OAuth2) + ZwiftAPIClient (REST)
  - runtime:  ZwiftDataStore, UDPBroadcaster, run_polling_loop
  - logsetup: its own logging (zwift_api_polling.log)
  - __main__: entry point (settings.json loading, CLI)
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
