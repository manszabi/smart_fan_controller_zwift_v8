"""Zwift API polling helper process – entry point.

Launched by the main app (FanController) as a subprocess, receiving the
settings.json path via ``--settings``. Reads the credentials, polling
interval and broadcast target from settings.json:

  - zwift_api.username / password / poll_interval   → login, cadence
  - datasource.zwift_udp_host / zwift_udp_port       → UDP broadcast target
  - global_settings.logging / log_directory          → logging

Credential priority: CLI (--username/--password) > environment variables
(ZWIFT_USERNAME/ZWIFT_PASSWORD) > the settings.json zwift_api section >
interactive prompt (with a separate window).

Standalone run: ``python -m smart_fan_controller.zwift_api --settings <path>``
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

__version__ = "1.2.0"


def _default_settings_path() -> str:
    """Default path of settings.json when no --settings is given."""
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
      3. The settings.json zwift_api section
      4. Interactive prompt (only with an interactive stdin; result saved)

    When the data is missing AND stdin is not interactive (e.g. the main
    app launched it with a DEVNULL stdin, or there is no separate console
    window), it logs a clean error and returns an empty ("", "") pair –
    main() understands that and exits without a traceback.
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
    if not username or not password:
        # Interactive prompt only on a real TTY. The main app launches the
        # subprocess with a DEVNULL stdin, and the separate console window
        # (CREATE_NEW_CONSOLE) only exists on Windows – in every other case
        # input() would raise EOFError.
        if not sys.stdin or not sys.stdin.isatty():
            log.error(
                "❌ Hiányzó Zwift bejelentkezési adat / Missing Zwift credentials. "
                "Töltsd ki a settings.json 'zwift_api' szekciójában a username és "
                "password mezőket, vagy add meg a ZWIFT_USERNAME / ZWIFT_PASSWORD "
                "környezeti változókat."
            )
            return "", ""
        try:
            if not username:
                username = input("Zwift felhasználónév / Username: ").strip()
                from_prompt = True
            if not password:
                password = getpass.getpass("Zwift jelszó / Password: ")
                from_prompt = True
        except EOFError:
            log.error(
                "❌ A bejelentkezési adat bekérése megszakadt (nincs interaktív "
                "bemenet). Add meg a username/password mezőket a settings.json "
                "'zwift_api' szekciójában."
            )
            return "", ""

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
    # The default directory of the log files is the settings.json folder.
    logsetup.set_base_dir(os.path.dirname(os.path.abspath(settings_path)))

    # Early logging: logs before the settings load are buffered in memory
    # because the "logging" flag is not known yet (same as the main app).
    logsetup.setup_early_logging()

    log.info("=" * 60)
    log.info(f" Zwift API Polling Monitor v{__version__}")
    log.info(" HTTPS API lekérdezés + UDP broadcast")
    log.info("=" * 60)

    # Load the settings from settings.json (shared with the main app).
    settings = load_settings(settings_path)
    cfg: ZwiftApiConfig = settings["zwift_api"]
    gs = settings["global_settings"]
    ds = settings["datasource"]

    # Configure logging per global_settings (consistent with the main app).
    if gs.logging:
        logsetup.setup_logging(gs.log_directory, enabled=True, debug=args.debug)
        logsetup.flush_early_logging()
    else:
        logsetup.setup_logging(enabled=False)
        logsetup.discard_early_logging()

    # Poll interval: CLI > settings.json zwift_api section > hard-coded default
    poll_interval: float = float(
        args.poll_interval if args.poll_interval is not None
        else (cfg.poll_interval or DEFAULT_POLL_INTERVAL)
    )

    # Broadcast target: the datasource UDP host/port (no duplication).
    broadcast_host = ds.zwift_udp_host
    broadcast_port = ds.zwift_udp_port

    username, password = resolve_credentials(args, cfg, settings_path)
    if not username or not password:
        # resolve_credentials already logged the cause – clean exit.
        return 1

    auth = ZwiftAuth(username, password, debug=args.debug)
    log.info("Bejelentkezés folyamatban / Logging in …")
    try:
        auth.login()
    except requests.exceptions.HTTPError as exc:
        log.error(f"❌ Bejelentkezés sikertelen / Login failed: {exc}")
        return 1
    except (requests.RequestException, ValueError) as exc:
        # RequestException also covers the ConnectionError/Timeout/
        # JSONDecodeError cases (e.g. a captive portal serving HTML with
        # 200); ValueError is the token-less auth response. A clean error
        # message instead of a traceback.
        log.error(f"❌ Bejelentkezési hiba / Login error: {exc}")
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
        auth.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
