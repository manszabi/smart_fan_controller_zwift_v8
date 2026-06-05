"""Zwift HTTPS API – OAuth2 token-életciklus és REST hívások."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, cast

import requests

from .decoder import _parse_protobuf_player_state

log = logging.getLogger("zwift_api_polling")

ZWIFT_AUTH_URL = (
    "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
)
ZWIFT_API_BASE = "https://us-or-rly101.zwift.com"
ZWIFT_CLIENT_ID = "Zwift_Mobile_Link"

# How many seconds before expiry to proactively refresh the token
TOKEN_REFRESH_BUFFER = 30  # seconds


class RateLimitError(Exception):
    """Raised when the Zwift API returns HTTP 429."""


class ZwiftAuth:
    """Authenticates with Zwift and manages access/refresh tokens in memory."""

    def __init__(self, username: str, password: str, *, debug: bool = False):
        self._username = username
        self._password = password
        self._debug = debug
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._expires_at: float = 0.0  # Unix timestamp when access_token expires

    def login(self) -> None:
        """Perform initial username/password authentication."""
        data = {
            "client_id": ZWIFT_CLIENT_ID,
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
        }
        resp = requests.post(ZWIFT_AUTH_URL, data=data, timeout=15)
        resp.raise_for_status()
        self._store_tokens(resp.json())
        log.debug("Bejelentkezés sikeres / Login successful")

    def ensure_valid_token(self) -> None:
        """Refresh the access token proactively if it is close to expiry."""
        if time.time() >= self._expires_at - TOKEN_REFRESH_BUFFER:
            self._refresh()

    @property
    def access_token(self) -> str:
        return self._access_token

    def _refresh(self) -> None:
        """Attempt a token refresh; re-authenticates on failure."""
        log.debug("Token frissítése / Refreshing token …")
        try:
            data = {
                "client_id": ZWIFT_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }
            resp = requests.post(ZWIFT_AUTH_URL, data=data, timeout=15)
            resp.raise_for_status()
            self._store_tokens(resp.json())
            log.debug("Token frissítve / Token refreshed")
        except requests.RequestException as exc:  # broad but not BaseException
            log.warning(
                f"⚠️  Token frissítés sikertelen, újra bejelentkezés / "
                f"Token refresh failed, re-logging in: {exc}"
            )
            self.login()

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", "")
        expires_in = int(payload.get("expires_in", 3600))
        self._expires_at = time.time() + expires_in


class ZwiftAPIClient:
    """Thin wrapper around the Zwift HTTPS REST API."""

    def __init__(self, auth: ZwiftAuth, *, debug: bool = False):
        self._auth = auth
        self._debug = debug
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        """Base headers without Accept – callers add their own Accept if needed."""
        return {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Zwift-Api-Version": "2.6",
        }

    def _json_headers(self) -> dict[str, str]:
        """Headers for endpoints that support JSON responses."""
        h = self._headers()
        h["Accept"] = "application/json"
        return h

    def get_profile(self) -> dict[str, Any]:
        """Return the authenticated user's profile (contains ``id``)."""
        url = f"{ZWIFT_API_BASE}/api/profiles/me"
        resp = self._session.get(url, headers=self._json_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_player_state(self, world_id: int, rider_id: int) -> dict[str, Any] | None:
        """Return the latest player state dict or *None* if not riding.

        Tries the relay/worlds endpoint which returns real-time protobuf data.
        Falls back gracefully when the player is not in a world.
        The relay endpoint only supports protobuf; JSON is never requested.
        """
        url = f"{ZWIFT_API_BASE}/relay/worlds/{world_id}/players/{rider_id}"
        resp = self._session.get(url, headers=self._headers(), timeout=10)
        if resp.status_code == 404:
            return None  # player not in this world
        if resp.status_code == 406:
            return None  # relay endpoint does not support the requested format
        if resp.status_code == 429:
            raise RateLimitError("Rate limited (429)")
        resp.raise_for_status()

        if log.isEnabledFor(logging.DEBUG):
            content_type = resp.headers.get("Content-Type", "")
            log.debug(
                f"Player state response | Content-Type: {content_type!r} | "
                f"bytes[:64]: {resp.content[:64]!r}"
            )
        return _parse_protobuf_player_state(resp.content)

    def get_active_world(self, rider_id: int) -> int | None:
        """Try to determine the world the rider is currently in (1=Watopia etc.).

        Queries the activities endpoint; returns the worldId of the most recent
        in-progress activity, or *None* if the rider is not online.
        Falls back to the profile endpoint when the activities response is not
        valid JSON (e.g. protobuf) or contains no worldId.
        """
        url = f"{ZWIFT_API_BASE}/api/profiles/{rider_id}/activities"
        params = {"limit": 1}
        resp = self._session.get(
            url, headers=self._json_headers(), params=params, timeout=10
        )
        if resp.status_code in (404, 204):
            return None
        if resp.status_code == 429:
            raise RateLimitError("Rate limited (429)")
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        activities = None
        if "application/json" in content_type:
            try:
                activities = resp.json()
            except json.JSONDecodeError as exc:
                log.debug(
                    f"JSON decode error on activities: {exc} | "
                    f"Content-Type: {content_type} | "
                    f"bytes[:64]: {resp.content[:64]!r}"
                )
        else:
            log.debug(
                f"Non-JSON activities response | Content-Type: {content_type!r} | "
                f"bytes[:64]: {resp.content[:64]!r}"
            )

        if activities:
            latest = cast(Any, activities[0] if isinstance(activities, list) else activities)
            world_id = latest.get("worldId") or latest.get("world_id")
            if world_id:
                return world_id

        # Fallback: try the profile endpoint which may carry a current worldId
        return self._get_world_from_profile(rider_id)

    def _get_world_from_profile(self, rider_id: int) -> int | None:
        """Return the worldId from the rider's profile endpoint, or *None*."""
        url = f"{ZWIFT_API_BASE}/api/profiles/{rider_id}"
        try:
            resp = self._session.get(url, headers=self._json_headers(), timeout=10)
            if resp.status_code != 200:
                return None
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                log.debug(
                    f"Non-JSON profile response | Content-Type: {content_type!r} | "
                    f"bytes[:64]: {resp.content[:64]!r}"
                )
                return None
            profile: Any = resp.json()
            if not isinstance(profile, dict):
                return None
            prof = cast(dict[str, Any], profile)
            return prof.get("worldId") or prof.get("world_id") or None
        except (json.JSONDecodeError, requests.RequestException):
            return None

    def close(self) -> None:
        self._session.close()
