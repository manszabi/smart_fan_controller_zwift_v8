# Changelog – Smart Fan Controller

Minden lényeges változás ebben a fájlban van dokumentálva, verziónként.

A formátum a [Keep a Changelog](https://keepachangelog.com/) ajánlását követi,
a verziószámozás a [Semantic Versioning](https://semver.org/) sémát
(MAJOR.MINOR.PATCH). A verzió egyetlen forrása:
`smart_fan_controller/__init__.py` → `__version__`.

> A v8 előtti verziók (v1–v7) története nem része ennek a repónak – a v8 a
> korábbi monolitikus szkript teljes újraszervezésével indult.

---

## [8.1.1] – 2026-07-23

Teljes projekt-átvilágítás utáni karbantartó kiadás: kis hibajavítások,
CPU-/energiatakarékossági optimalizálások és teljes dokumentációs csomag.
A program működése és kinézete változatlan.

### Javítva

- **Időtartam-mérés fali óra helyett monotonic órával** – a HUD Zwift
  grace-period figyelése (`ui/window.py`) és a Zwift-lekérdező ütemezése
  (`zwift_api/runtime.py`) `time.time()` helyett `time.monotonic()`-ot
  használ, így az óraátállítás/NTP-korrekció nem okozhat hibás időzítést.
  (A `ZwiftDataStore` adat-időbélyegei szándékosan fali órán maradtak.)
- Docstring-elírás a csomag `__init__.py`-ában (`swift_` → `zwift_fan_controller.py`).
- Sosem olvasott `_zwift_was_running` attribútum eltávolítva (`ui/window.py`).
- Apró duplikáció a `hr_processor_task`-ban (settings újraolvasás helyett
  a meglévő lokális változó).

### Optimalizálva

- **Futó összegű gördülő átlag** (`core/averaging.py`): mintánkénti teljes
  buffer-összegzés (O(n)) helyett O(1) összeg-karbantartás – egész mintákkal
  bitre azonos eredmény.
- **Esemény-vezérelt BLE szenzor kapcsolat-figyelés** (`handlers/_ble.py`):
  az 1 Hz-es `is_connected` polling helyett a bleak hivatalos
  `disconnected_callback` + `asyncio.Event` mintája, 10 mp-es biztonsági
  ellenőrzéssel – kevesebb ébredés, azonnali szétkapcsolás-észlelés.

### Hozzáadva

- **HUD modernizálás és UI refaktor** (2026-07-13, a 8.1.0 után):
  lebegő kártya ablak, zóna-sáv, telemetria-mérősávok, LCARS hangeffektek,
  a UI réteg modulokra bontása (`theme` / `widgets` / `sound` / `window`),
  automata UI tesztek, robusztus többmonitoros pozíció-visszaállítás,
  simább skálázás.
- 3 új regressziós teszt a futó összegű átlagolásra (evikció, `clear()`,
  hosszú futás) – összesen **346 teszt**.
- Dokumentációs csomag: `mukodes.odt` (részletes működési leírás),
  `manual.odt` (felhasználói kézikönyv), `CHANGELOG.md`, `DEVELOPMENT.md`
  (fejlesztői útmutató), Sphinx API-referencia (`docs/`).
- `esp32_firmware/`: az ESP32 firmware-projekt (FanController_OTA_debug,
  v7.14.7) beillesztve magyarázó `BEVEZETO.md`-vel; a firmware átvilágítása
  során talált javítások ([FIX-ESP-49] wrap-safe zónaváltás-időzítés,
  [MOD-14..16]) a kanonikus firmware-repóba is bekerültek. A README elavult
  firmware-szekciója (v5.2.0 → v7.14.7) frissítve.

---

## [8.1.0] – 2026-07-10

Teljes átvilágítás: hibajavítások, Python 3.11+ modernizálás, kibővített
tesztkészlet.

### Javítva

- swift/zwift névtörés javítása (tesztek, pyproject, spec, run.bat, doksik).
- BLE fan: AUTH-bukásnál nyitva ragadó kapcsolat + `is_connected` szinkron.
- ANT+: szálbiztos node-bontás, megszakítható várakozások, queue-híd csere
  (`call_soon_threadsafe`).
- Cross-thread `Task.cancel` a loopra ütemezve.
- Kilépés: futó loop bezárásának védelme, `to_thread` várakozások megszakítása.
- Logging: handler-szivárgás javítása (Windows log-rotáció), logger-név elírás.
- Config: NaN / nem-dict JSON nem dönti be a betöltést; atomikus mentés
  (temp fájl + `os.replace`).
- zwift_api: backoff-túlcsordulás, login-hibautak, `SIO_UDP_CONNRESET`.
- HUD: többmonitoros pozíció-sorrend, debounce-olt automata geometria-mentés.

### Modernizálva (Python 3.11+)

- `enum.StrEnum`, `slots=True` dataclassok, `X | None` típusjelölés,
  lazy logging.
- bleak 3.x kompatibilitás, `find_device_by_name` (gyorsabb csatlakozás).
- Natív ablakmozgatás/átméretezés (`startSystemMove`/`Resize`),
  palette-alapú színezés.
- pyproject: dinamikus verzió, `build_meta` backend, explicit csomaglista.

### Hozzáadva

- 343 teszt (új: async adatsík, UDP fogadó, protobuf dekóder, backoff).
- README / ARCHITECTURE / CONFIGURATION frissítve + hibaelhárítási fejezet.

---

## [8.0.0] – 2026-06-05

A v8-as generáció alapkiadása: a korábbi monolitikus szkript teljes
újraszervezése moduláris csomaggá, keményített konfig-validációval és új
funkciókkal. (A 2026-05-29 – 2026-06-05 közötti fejlesztési sorozat
összefoglalója.)

### Architektúra

- A teljes logika a `smart_fan_controller/` csomagba szervezve:
  `config` (settings-modellek + betöltő), `core` (tiszta domain-logika:
  zónák, átlagolás, cooldown, állapot, logging), `handlers` (ANT+ / BLE /
  Zwift UDP), `processors` (async feldolgozó taskok), `ui` (LCARS HUD),
  `zwift_api` (Zwift-lekérdező segédprocessz), `controller` (orchestrátor),
  `app` (belépőpont).
- A fő szkript (`zwift_fan_controller.py`) vékony belépővé alakítva,
  visszafelé kompatibilis re-exportokkal.
- A `zwift_api_polling` a fő struktúrába integrálva; konfigurációja a közös
  `settings.json` `zwift_api` szekciójába került (külön beállításfájl
  megszűnt).
- A fontok és hangok a csomagba kerültek (PyInstaller-kompatibilisen).

### Konfiguráció

- Minden szekcióhoz type-safe dataclass modellek, mezőnkénti tartomány- és
  típusvalidációval; hibás érték → figyelmeztetés + alapértelmezés.
- Kereszt-validációk (`minimum_samples` ≤ `buffer_seconds × buffer_rate_hz`,
  zónahatár-sorrendek, HR-tartományok).
- `ble` szekció átnevezése `ble_fan`-ra (a régi kulcs deprecation-figyelmeztetéssel
  továbbra is működik).
- `logging` be/ki kapcsoló + korai log-pufferelés a settings betöltése előtt;
  `logging: false` az eszköz-logokat is letiltja.
- `"null"` / `"none"` stringek egységes kezelése auto-discoveryként.
- `cooldown_seconds: 0` visszaengedése (azonnali váltás).

### Funkciók

- BLE fan: időzített, nem-blokkoló háttér-újracsatlakozás (a zónaparancs-
  feldolgozást soha nem akasztja meg) + regressziós tesztek.
- BLE: a csatlakozott eszköz GATT characteristic UUID-jainak kiírása
  (konzol + `ble_devices.log`).
- ANT+: induláskori átmeneti USB-hibák halkítása + grace-delay; célzott
  WinUSB/Zadig tanács tartós meghajtóhiba esetén.
- Indítási info-zaj elnémítása (pywinauto warning, Qt ffmpeg log).
- Log fájlok alapértelmezett helye a belépő szkript könyvtára.

### Egyéb

- Szálbiztonsági javítások (`CooldownController.__repr__`, `ConsolePrinter`).
- Windows batch fájlok CRLF sorvégekkel; `.gitignore` jelszó-védelem
  (`settings.json` soha nem kerülhet a repóba).
