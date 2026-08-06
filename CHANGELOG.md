# Changelog – Smart Fan Controller

Minden lényeges változás ebben a fájlban van dokumentálva, verziónként.

A formátum a [Keep a Changelog](https://keepachangelog.com/) ajánlását követi,
a verziószámozás a [Semantic Versioning](https://semver.org/) sémát
(MAJOR.MINOR.PATCH). A verzió egyetlen forrása:
`smart_fan_controller/__init__.py` → `__version__`.

> A v8 előtti verziók (v1–v7) története nem része ennek a repónak – a v8 a
> korábbi monolitikus szkript teljes újraszervezésével indult.

---

## [Unreleased]

### Javítva

Teljes kód-átvilágítás utáni javítási csomag (config, core, BLE/ANT+/Zwift
kezelők, processzorok, controller, app, zwift_api).

**Hibák**

- **ANT+ USB handle szivárgás node-init hibánál** (`handlers/_ant.py`): a
  `Node()` már lefoglalta a sticket, de ha a `set_network_key()` vagy az
  eszköz-regisztráció elhasalt, a node sosem került a lock alá – így a
  `_stop_node()` már `None`-t talált, és a nyitott USB handle bennragadt.
  Ez maga okozta a következő próbálkozás „could not claim interface
  (resource busy)" hibáját, vagyis a hiba önmagát táplálta. A félkész node
  mostantól hibaágon is leáll (`_release_node`).
- **A Zwift UDP adatforrás véglegesen leállt foglalt portnál**
  (`handlers/zwift_udp.py`): a `run()` elnyelte az `OSError`-t és normálisan
  visszatért, amit a `_guarded_task` sikeres befejezésnek látott – így az
  újrapróbálkozás sem futott le, és a HUD-on egy logsor után örökre
  `ZWIFT P:FAIL` maradt. A fogadó mostantól – a BLE/ANT+ kezelőkhöz
  hasonlóan – saját újrakötési ciklussal próbálkozik (5s-től 60s-ig növekvő
  várakozással), és jelzi a felhasználónak az első hibát.
- **HUD-mentés kivétellel elszállt hibás settings.json-nél**
  (`config/loader.py`): a `save_hud_settings_only` `isinstance` ellenőrzés
  nélkül írt a betöltött adatba, így egy nem-objektum (pl. lista) tartalmú
  fájlnál `TypeError`-t dobott – a HUD debounce-olt automata mentéséből,
  azaz a Qt eseményhurokból.
- **Minden HUD-mentés átnevezte a `close_at_zwiftapp_exe` kulcsot**
  (`config/schemas.py`): a betöltő az aláhúzásos nevet preferálja, a
  `to_dict()` viszont a régi, pontos `close_at_zwiftapp.exe` nevet írta ki.
  Mostantól az aktuális kulcsnevet menti; a régit olvasáskor továbbra is
  elfogadja.
- **Rossz típusú szekció csendben elveszett** (`config/loader.py`): az
  elgépelt szekció-*névre* figyelmeztetett a program, de ha egy létező
  szekció *értéke* nem objektum (pl. `"power_zones": 42`), az egész szekció
  némán az alapértelmezésre esett vissza. Most ez is figyelmeztetést kap.

**Robusztusság**

- `_write_json_atomic`: `flush()` + `os.fsync()` az `os.replace` előtt – e
  nélkül az adat még az OS write cache-ben ülhetett, miközben az átnevezés
  már megtörtént (a docstring épp áramszünet ellen ígért védelmet).
- A forrás-specifikus `minimum_samples` tartománya 1–100-ról 1–600-ra
  módosult, egységesen a globális mezőével.
- A HR-feldolgozó a power-ágnál megszokott sorrendben validál (előbb
  ellenőriz, utána konvertál), és az érvénytelen HR is kap felhasználói
  figyelmeztetést – korábban némán eldobódott.
- `controller.stop()`: `wait()` a `kill()` után – enélkül a Zwift
  segédprocessz zombiként maradhatott POSIX rendszereken.
- Az ANT+ watchdog lock alatt olvassa a node-referenciát.
- A Zwift segédprocessz korai hibakilépésein is lezárul a HTTP session.
- `resolve_log_dir`: az írhatóság-teszt fájljának sikertelen törlése többé
  nem minősíti írhatatlannak a könyvtárat.

### Módosítva

- **Az átlagolási ablak időalapú lett** (`core/averaging.py`): a
  `buffer_seconds` mostantól valós másodperceket jelent – egy minta akkor
  esik ki, ha ennél régebbi –, a `buffer_rate_hz` pedig csak a puffer
  méretkorlátját adja. Korábban a puffer kizárólag mintaszámra vágott, így a
  beállítottnál lassabb forrásnál az ablak jóval hosszabb lett: a Zwift
  HTTPS API 3 másodpercnél sűrűbben nem kérdezhető le (~0.33 Hz), a
  `10s × 3Hz = 30` mintás puffer tehát **90 másodpercnyi** adatot tartott,
  vagyis a ventilátor másfél perces átlagot követett. A BLE/ANT+ forrásoknál
  (4 Hz, egyezik a beállítottal) a viselkedés változatlan. A vártnál is
  lassabb forrásnál a program mindig megtartja az `minimum_samples`-nyi
  legfrissebb mintát, hogy soha ne maradjon átlag – és így vezérlés – nélkül.
- **`zwift_api.poll_interval` alsó határa 1.0-ról 3.0-ra** nőtt: a Zwift API
  ennél sűrűbben nem szolgál ki, kisebb érték csak rate-limit (429)
  válaszokat eredményezne. A `--poll-interval` CLI kapcsoló is korlátozódik.

### Karbantartás (2026-os korszerűsítés)

- **`asyncio.set_event_loop()` kivezetve** (`app.py`): a 3.14-ben elavult, a
  3.16-ban megszűnő hívás nem kell – az event loop objektum expliciten
  utazik, a benne futó kód pedig `asyncio.get_running_loop()`-ot használ.
- **Függőség-alsóhatárok felhúzva** a 2026-os kiadásokhoz: `bleak>=1.0.0`,
  `openant>=1.3.0`, `requests>=2.32.0`, `PySide6>=6.7.0`,
  `pywinauto>=0.6.9`. (A bleak 1.0/2.0/3.0 törő változásai – notification
  callback típusa, `connect()` visszatérési értéke, `write_gatt_char`
  `response` paramétere, `BLEDevice.name` – nem érintik a kódot.)
- **Célzott üzenet kikapcsolt Bluetooth esetén**: a bleak 2.0 új
  `BleakBluetoothNotAvailableError` kivételét (és a régebbi bleak
  megfelelő hibaszövegét) felismerve egyszeri, cselekvésre váltható
  figyelmeztetés jön a nyers hibaszöveg helyett – az ANT+ libusb-tipp
  mintájára.
- **Python-támogatás dokumentálva**: 3.11–3.14 (a HUD-hoz kellő PySide6
  6.11 még nem támogatja a 3.15-öt); `classifiers` a `pyproject.toml`-ban.
- **Teszt-függőségek és pytest-konfiguráció deklarálva**:
  `pip install -e ".[dev]"`, illetve `[tool.pytest.ini_options]` – benne a
  saját kódból érkező elavulás-figyelmeztetés hibává léptetése, hogy a
  következő ilyen (pl. egy 3.16-os változás) időben kiderüljön.

Új regressziós tesztek minden fenti ponthoz (összesen 384 teszt).

### Korábbi javítások

- **HUD: kitakart feliratok átméretezéskor** (`ui/window.py`, `ui/widgets.py`):
  bizonyos ablakméreteknél a ZONE, a POWER, a HEART RATE és a STARFLEET
  CYCLING DIV felirat (és a hozzájuk tartozó értékek) nem látszottak
  teljesen – a sorok magassága a szöveg alá szorult. Négy ok volt:
  1. **A panelek minimuma beragadt.** A Qt alapértelmezett
     `SetDefaultConstraint`-je a layout minimumát a widget *explicit*
     `minimumSize`-ába írja, amit utána már csak emelni lehet. Így minden
     panel a valaha volt legnagyobb skálán mért minimumán maradt: az
     ablak kicsinyítésekor a sorok nem tudtak összébb menni, csak a
     szövegük szorult ki. A belső layoutok mostantól `SetNoConstraint`-tel
     dolgoznak, az ablak minimumát pedig a HUD maga tartja karban.
  2. **A dobozméretek nem skálázódtak.** Csak a betűméret követte a
     skálát, a padding, a margó, a sorköz, az osztóvonal és a csúszka
     mérete fix pixelben állt. Kis ablakban ez az állandó overhead
     kiszorította a szöveget, nagyban pedig aránytalanul vékony volt.
     Mostantól minden dobozméret a skálával megy.
  3. **A minimum a pillanatnyi ablakmérethez volt kötve** (az előző
     javítás a visszacsatolási hurok ellen), így az ablak a tartalom
     olvasható mérete alá is mehetett. Az új `_calibrate_sizing()`
     induláskor bemért egy alapméretet és egy legkisebb skálát – a
     minimum ettől kezdve állandó, csak a tartalomtól függ, tehát
     átméretezés nem hathat rá vissza (nincs ugrálás, nincs kitakarás).
     A mérés a lehető legszélesebb értékekkel (`WIDEST_TEXTS`) fut, így
     egy később megjelenő hosszabb szöveg (pl. `P:FAIL  HR:FAIL`) sem
     tud kilógni.
  4. **A fix szélességű címkék a pillanatnyi szövegükre méreteződtek**,
     ezért pl. a `100%` kilógott a `92%`-ra szabott dobozból.
  A skála ezentúl 5%-os létrán mozog (`SCALE_STEP`): a betűméret és a
  kerekített padding úgyis egész pixel, így a tartalom csak olyan
  méreteket vehet fel, amiket a kalibráció ténylegesen bemért.
  Következmény: az eddigi 340×460-as ablakban a HUD kb. 0.90-es skálán
  áll (a tartalom teljes egészében ennyinél fér el az Antonio fonttal) –
  a régi betűmérethez az ablakot kb. 10%-kal nagyobbra kell húzni.
  Új regressziós tesztek: `tests/test_hud_ui.py` (összesen 368 teszt).

- **HUD egeres átméretezés ugrálása** (`ui/window.py`): a tartalom-alapú
  minimumméret frissítése visszacsatolási hurkot indíthatott – a
  pillanatnyi ablakméretnél nagyobb minimum megnövelte az ablakot, amitől
  nőtt a skála és vele a tartalom minimuma is, így az ablak húzás után
  200 ms-onként, lépcsőzve magától tovább nőtt. Három javítás:
  1. `_update_min_size` a minimumot az aktuális ablakméretnél nagyobbra
     nem emeli, így sosem nagyíthatja az ablakot (a hurok megszakad);
  2. amíg a bal egérgomb le van nyomva (natív átméretezés közben a
     debounce a húzás szünetében is elsülhet), a minimum-emelés helyett a
     timer újra-élesíti magát – kicsinyítés közben nem "ragad be" az ablak;
  3. a sarok megfogása önmagában már nem indítja a debounce-t, csak a
     tényleges méretváltozás – 200 ms-nál hosszabb nyomva tartásnál sem
     ugrik vissza a tartalom-minimum a húzás megkezdése előtt.
  Új regressziós tesztek: `tests/test_hud_ui.py` (összesen 365 teszt).

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
