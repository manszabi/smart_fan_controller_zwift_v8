"""Zwift API polling segédprocessz – belépési pont.

A fő app (FanController) indítja subprocess-ként, a settings.json útvonalát
``--settings`` paraméterrel átadva. A bejelentkezési adatokat, a lekérdezési
gyakoriságot és a broadcast célt a settings.json-ból olvassa:

  - zwift_api.username / password / poll_interval   → bejelentkezés, gyakoriság
  - datasource.zwift_udp_host / zwift_udp_port       → UDP broadcast cél
  - global_settings.logging / log_directory          → loggolás

Credential prioritás: CLI (--username/--password) > környezeti változó
(ZWIFT_USERNAME/ZWIFT_PASSWORD) > settings.json zwift_api szekció > interaktív
bekérés (külön ablak esetén).

Önállóan is futtatható: ``python -m smart_fan_controller.zwift_api --settings <path>``
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
from typing import Any

import requests

from smart_fan_controller.config import ZwiftApiConfig, load_settings
from smart_fan_controller.config.loader import save_zwift_api_credentials

from . import logsetup
from .api import ZwiftAPIClient, ZwiftAuth
from .logsetup import log
from .runtime import (
    DEFAULT_POLL_INTERVAL,
    UDPBroadcaster,
    ZwiftDataStore,
    run_polling_loop,
)

__version__ = "1.1.0"


def _default_settings_path() -> str:
    """A settings.json alapértelmezett útvonala, ha nincs --settings megadva."""
    base = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.getcwd()
    )
    return os.path.join(base, "settings.json")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Zwift API Polling Monitor – polls Zwift HTTPS API and "
            "broadcasts to smart_fan_controller via UDP"
        )
    )
    parser.add_argument(
        "--settings",
        default=None,
        metavar="PATH",
        help="A settings.json elérési útja (alapértelmezett: a program könyvtárában)",
    )
    parser.add_argument("--username", default="", help="Zwift username / e-mail")
    parser.add_argument("--password", default="", help="Zwift password")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Polling interval in seconds (felülírja a settings.json értékét)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug output",
    )
    return parser


def resolve_credentials(
    args: argparse.Namespace,
    cfg: ZwiftApiConfig,
    settings_path: str,
) -> tuple[str, str]:
    """Return (username, password) from CLI args, env vars, settings, or prompt.

    Priority:
      1. CLI args (--username / --password)
      2. Environment variables (ZWIFT_USERNAME / ZWIFT_PASSWORD)
      3. settings.json zwift_api szekció
      4. Interaktív bekérés (az eredmény a settings.json-ba mentve)
    """
    username = (
        args.username
        or os.environ.get("ZWIFT_USERNAME", "")
        or cfg.username
    )
    password = (
        args.password
        or os.environ.get("ZWIFT_PASSWORD", "")
        or cfg.password
    )

    from_prompt = False
    if not username:
        username = input("Zwift felhasználónév / Username: ").strip()
        from_prompt = True
    if not password:
        password = getpass.getpass("Zwift jelszó / Password: ")
        from_prompt = True

    if from_prompt:
        if save_zwift_api_credentials(settings_path, username, password):
            log.info(f"✅ Bejelentkezési adatok mentve / Credentials saved to {settings_path}")
            log.warning(
                f"⚠️  A jelszó titkosítatlanul van mentve! / "
                f"Password is stored in plaintext in {settings_path}"
            )

    return username, password


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    settings_path = args.settings or _default_settings_path()
    # A log fájlok alapértelmezett könyvtára a settings.json mappája legyen.
    logsetup.set_base_dir(os.path.dirname(os.path.abspath(settings_path)))

    # Korai logging: a settings betöltése előtti logokat memóriába puffereljük,
    # mert a "logging" flag még nem ismert (mint a fő appban).
    logsetup.setup_early_logging()

    log.info("=" * 60)
    log.info(f" Zwift API Polling Monitor v{__version__}")
    log.info(" HTTPS API lekérdezés + UDP broadcast")
    log.info("=" * 60)

    # Beállítások betöltése a settings.json-ból (a fő apppal közös fájl).
    settings = load_settings(settings_path)
    cfg: ZwiftApiConfig = settings["zwift_api"]
    gs = settings["global_settings"]
    ds = settings["datasource"]

    # Loggolás konfigurálása a global_settings szerint (egységes a fő appal).
    if gs.logging:
        logsetup.setup_logging(gs.log_directory, enabled=True, debug=args.debug)
        logsetup.flush_early_logging()
    else:
        logsetup.setup_logging(enabled=False)
        logsetup.discard_early_logging()

    # Poll interval: CLI > settings.json zwift_api szekció > hard-coded default
    poll_interval: float = float(
        args.poll_interval if args.poll_interval is not None
        else (cfg.poll_interval or DEFAULT_POLL_INTERVAL)
    )

    # Broadcast cél: a datasource UDP host/port (nincs duplikáció).
    broadcast_host = ds.zwift_udp_host
    broadcast_port = ds.zwift_udp_port

    username, password = resolve_credentials(args, cfg, settings_path)

    auth = ZwiftAuth(username, password, debug=args.debug)
    log.info("Bejelentkezés folyamatban / Logging in …")
    try:
        auth.login()
    except requests.exceptions.HTTPError as exc:
        log.error(f"❌ Bejelentkezés sikertelen / Login failed: {exc}")
        return 1
    except requests.exceptions.ConnectionError as exc:
        log.error(f"❌ Hálózati hiba / Network error: {exc}")
        return 1

    client = ZwiftAPIClient(auth, debug=args.debug)
    try:
        log.info("Profil lekérése / Fetching profile …")
        profile: dict[str, Any] = client.get_profile()
    except Exception as exc:  # noqa: BLE001
        log.error(f"❌ Profil lekérése sikertelen / Failed to fetch profile: {exc}")
        client.close()
        return 1

    rider_id: int = int(profile.get("id", 0))
    if not rider_id:
        log.error("❌ Rider ID nem található a profilban / Rider ID not found in profile")
        client.close()
        return 1

    log.info(f"✅ Rider ID: {rider_id}")
    log.info(
        f"🔄 Lekérdezési intervallum / Poll interval: {poll_interval}s  |  "
        f"📡 UDP cél: {broadcast_host}:{broadcast_port}  |  Press Ctrl+C to stop."
    )

    store = ZwiftDataStore()
    broadcaster = UDPBroadcaster(host=str(broadcast_host), port=int(broadcast_port))
    stop_event = threading.Event()

    try:
        run_polling_loop(
            client,
            auth,
            store,
            broadcaster,
            stop_event,
            rider_id,
            poll_interval=poll_interval,
            debug=args.debug,
        )
    except KeyboardInterrupt:
        log.info("Leállítás / Stopping …")
    finally:
        stop_event.set()
        broadcaster.close()
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
