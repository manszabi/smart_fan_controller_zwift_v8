"""Minimal protobuf decoder for the binary responses of the Zwift relay API.

The relay/worlds endpoint returns protobuf; this decoder handles wire
types 0 (varint), 1 (64-bit fixed), 2 (length-delimited) and 5 (32-bit
fixed) – no .proto compilation needed.
"""
from __future__ import annotations

import logging
import struct
from collections.abc import Generator
from typing import Any

log = logging.getLogger("zwift_api_polling")

# Unit conversion factors (confirmed from zwift_messages.proto)
_MICROHERTZ_TO_RPM = 60 / 1_000_000   # cadenceUHz: µHz → RPM
_MM_PER_HOUR_TO_KM_PER_HOUR = 1 / 1_000_000  # speed: mm/h → km/h

# PlayerState protobuf field numbers (from zwift_messages.proto)
_PS_FIELD_ID = 1
_PS_FIELD_SPEED = 6
_PS_FIELD_CADENCE_UHZ = 9
_PS_FIELD_HEARTRATE = 11
_PS_FIELD_POWER = 12


class ProtobufDecoder:
    """Minimal protobuf decoder supporting wire types 0, 1, 2, and 5."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def _read_varint(self) -> int:
        result = 0
        shift = 0
        while self._pos < len(self._data):
            if shift >= 70:  # max 10 bytes (70 bits) per varint
                raise ValueError("Varint too long (corrupted data)")
            byte = self._data[self._pos]
            self._pos += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return result
            shift += 7
        raise ValueError("Truncated varint")

    def _read_bytes(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ValueError(
                f"Not enough data: need {n}, have {len(self._data) - self._pos}"
            )
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def fields(self) -> Generator[tuple[int, int, int | bytes], None, None]:
        """Yield (field_number, wire_type, value) tuples."""
        while self._pos < len(self._data):
            tag = self._read_varint()
            field_number = tag >> 3
            wire_type = tag & 0x07
            value: int | bytes
            if wire_type == 0:
                value = self._read_varint()
            elif wire_type == 1:
                value = self._read_bytes(8)
            elif wire_type == 2:
                length = self._read_varint()
                value = self._read_bytes(length)
            elif wire_type == 5:
                value = self._read_bytes(4)
            else:
                break
            yield field_number, wire_type, value

    @classmethod
    def parse_fields(cls, data: bytes) -> dict[int, int | bytes]:
        """Return {field_number: value} keeping the last value per field."""
        result: dict[int, int | bytes] = {}
        try:
            for field_number, _wt, value in cls(data).fields():
                result[field_number] = value  # type: ignore[assignment]
        except (ValueError, struct.error):
            pass
        return result


def _proto_to_int(value: int | bytes | None, default: int = 0) -> int:
    """Convert a protobuf field value (varint or fixed bytes) to int."""
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        if len(value) == 4:
            return struct.unpack("<I", value)[0]
        if len(value) == 8:
            return struct.unpack("<Q", value)[0]
    return default


def _parse_protobuf_player_state(data: bytes) -> dict[str, Any] | None:
    """Decode a raw PlayerState protobuf blob into a ZwiftDataStore-compatible dict.

    Returns *None* if the blob contains no meaningful data (all zeros).
    """
    fields = ProtobufDecoder.parse_fields(data)
    if not fields:
        return None
    speed_mmh = _proto_to_int(fields.get(_PS_FIELD_SPEED, 0))
    cadence_uhz = _proto_to_int(fields.get(_PS_FIELD_CADENCE_UHZ, 0))
    state: dict[str, Any] = {
        "riderId": _proto_to_int(fields.get(_PS_FIELD_ID, 0)),
        "power": _proto_to_int(fields.get(_PS_FIELD_POWER, 0)),
        "heartrate": _proto_to_int(fields.get(_PS_FIELD_HEARTRATE, 0)),
        "cadence": round(cadence_uhz * _MICROHERTZ_TO_RPM) if cadence_uhz else 0,
        "speed_kmh": round(speed_mmh * _MM_PER_HOUR_TO_KM_PER_HOUR, 1) if speed_mmh else 0.0,
    }
    # Return None when riderId is zero; an active rider always has a valid ID
    if not state["riderId"]:
        return None
    return state
