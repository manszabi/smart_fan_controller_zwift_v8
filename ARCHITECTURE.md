# Smart Fan Controller v8 - Architektura

## Magas szintu attekintes

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          FO SZAL (MAIN THREAD)                          │
│                  (Qt esemenyhurok + jelkezeles)                          │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                    HUD Ablak (PySide6)                            │  │
│  │                  Star Trek LCARS Tema                             │  │
│  │          500ms frissites, hangeffektek, mindig felul              │  │
│  └──────────────────────────┬────────────────────────────────────────┘  │
│                             │ UISnapshot olvasas (szalbiztos)           │
└─────────────────────────────┼───────────────────────────────────────────┘
                              │
┌─────────────────────────────┼───────────────────────────────────────────┐
│                    ASYNCIO SZAL (daemon)                                 │
│                                                                         │
│  ┌──────────────────────────┴────────────────────────────────────────┐  │
│  │                    FanController.run()                             │  │
│  │                   (fo vezerloelemem)                               │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌───────────────── BEMENETI KEZELOK ─────────────────────────────┐   │
│  │                                                                  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │   │
│  │  │  ANT+ Bemenet│  │  BLE Bemenet │  │   Zwift Bemenet       │  │   │
│  │  │  (Szal)      │  │  (async)     │  │   (async)             │  │   │
│  │  │              │  │              │  │                       │  │   │
│  │  │ ┌──────────┐ │  │ ┌──────────┐ │  │  ┌─────────────────┐ │  │   │
│  │  │ │Teljesit- │ │  │ │Teljesit- │ │  │  │ ZwiftAuth       │ │  │   │
│  │  │ │menymero  │ │  │ │meny      │ │  │  │ (OAuth2)        │ │  │   │
│  │  │ │(openant) │ │  │ │(0x1818)  │ │  │  ├─────────────────┤ │  │   │
│  │  │ └──────────┘ │  │ └──────────┘ │  │  │ ZwiftAPIClient  │ │  │   │
│  │  │ ┌──────────┐ │  │ ┌──────────┐ │  │  │ (HTTPS polling) │ │  │   │
│  │  │ │Pulzus-   │ │  │ │Pulzus    │ │  │  ├─────────────────┤ │  │   │
│  │  │ │mero      │ │  │ │(0x180D)  │ │  │  │ProtobufDecoder  │ │  │   │
│  │  │ │(openant) │ │  │ └──────────┘ │  │  │ → queue         │ │  │   │
│  │  │ └──────────┘ │  │              │  │  └─────────────────┘ │  │   │
│  │  └──────┬───────┘  └──────┬───────┘  └───────────┬───────────┘  │   │
│  │         │                 │                       │              │   │
│  └─────────┼─────────────────┼───────────────────────┼──────────────┘   │
│            │                 │                       │                   │
│            ▼                 ▼                       ▼                   │
│      raw_power_queue   raw_power_queue        raw_power_queue           │
│      raw_hr_queue      raw_hr_queue           raw_hr_queue              │
│            │                 │                       │                   │
│  ┌─────────┼─────────────────┼───────────────────────┼──────────────┐   │
│  │         ▼                 ▼                       ▼              │   │
│  │  ┌─────────────────────────────────────────────────────────┐    │   │
│  │  │              FELDOLGOZASI FOLYAMAT                       │    │   │
│  │  │                                                          │    │   │
│  │  │  ┌──────────────────┐     ┌──────────────────┐          │    │   │
│  │  │  │ Teljesitmeny-    │     │  Pulzus-          │          │    │   │
│  │  │  │ feldolgozo       │     │  feldolgozo       │          │    │   │
│  │  │  │                  │     │                   │          │    │   │
│  │  │  │ PowerAverager    │     │  HRAverager       │          │    │   │
│  │  │  │ (gorditett atlag)│     │  (gorditett atlag)│          │    │   │
│  │  │  │       │          │     │       │           │          │    │   │
│  │  │  │       ▼          │     │       ▼           │          │    │   │
│  │  │  │ zone_for_power() │     │ zone_for_hr()    │          │    │   │
│  │  │  │       │          │     │       │           │          │    │   │
│  │  │  └───────┼──────────┘     └───────┼───────────┘          │    │   │
│  │  │          │                        │                      │    │   │
│  │  │          ▼                        ▼                      │    │   │
│  │  │  ┌────────────────────────────────────────────┐          │    │   │
│  │  │  │         apply_zone_mode()                  │          │    │   │
│  │  │  │  (csak_telj / csak_pulzus / magasabb_nyer) │          │    │   │
│  │  │  └────────────────────┬───────────────────────┘          │    │   │
│  │  │                       │                                  │    │   │
│  │  │                       ▼                                  │    │   │
│  │  │  ┌────────────────────────────────────────────┐          │    │   │
│  │  │  │         CooldownController                 │          │    │   │
│  │  │  │  Zona FEL   → azonnali                     │          │    │   │
│  │  │  │  Zona LE    → varakozasi ido               │          │    │   │
│  │  │  │  Adaptiv: felezes (nagy eses) / duplazas   │          │    │   │
│  │  │  └────────────────────┬───────────────────────┘          │    │   │
│  │  │                       │                                  │    │   │
│  │  │  ┌────────────────────┴───────────────────────┐          │    │   │
│  │  │  │         DropoutChecker                     │          │    │   │
│  │  │  │  Nincs adat > timeout → Z0 + atlag reset   │          │    │   │
│  │  │  └────────────────────┬───────────────────────┘          │    │   │
│  │  └───────────────────────┼──────────────────────────────────┘    │   │
│  │                          │                                       │   │
│  └──────── FELDOLGOZAS ─────┼───────────────────────────────────────┘   │
│                             │                                           │
│                             ▼                                           │
│                       zone_queue (0-3)                                  │
│                             │                                           │
│  ┌──────────────────────────┼───────────────────────────────────────┐   │
│  │               KIMENET    ▼                                       │   │
│  │  ┌────────────────────────────────────────────┐                 │   │
│  │  │       BLEFanOutputController               │                 │   │
│  │  │                                            │                 │   │
│  │  │  kereses → csatlakozas → hitlesites (PIN)  │                 │   │
│  │  │  → "LEVEL:N" iras GATT-ra (FFE0/FFE1)     │                 │   │
│  │  │  → automatikus ujracsatlakozas             │                 │   │
│  │  └────────────────────┬───────────────────────┘                 │   │
│  └───────────────────────┼─────────────────────────────────────────┘   │
│                          │ BLE                                          │
└──────────────────────────┼──────────────────────────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │   ESP32-C3 Firmware    │
              │   (Xiao SEEED Studio)  │
              │                        │
              │  BLE Szerver (FFE0)    │
              │  "LEVEL:N" → Relek     │
              │                        │
              │  Z0: Mind KI           │
              │  Z1: VENT1 (33%)       │
              │  Z2: VENT1+VENT2 (66%) │
              │  Z3: VENT1+2+3 (100%)  │
              │                        │
              │  + OTA, WebSerial      │
              │  + WiFi AP/STA         │
              │  + Melyal. (30perc)    │
              │  + Kezi gomb           │
              └────────────────────────┘
```

---

## Adatfolyam (Szekvencia)

```
Szenzor/Zwift → Bemeneti kezelo → nyers queue → Feldolgozo → Atlagolo → Zona szamitas
    → Zona mod → Cooldown → zone_queue → BLE Kimenet → ESP32 → Ventilatorok
                                              ↓
                                         HUD (kijelzo)
```

1. **Bemenet**: ANT+/BLE/Zwift nyers teljesitmenyt (W) es pulzust (bpm) ad
2. **Atlagolas**: Gorditett atlag puffer simitas (forrasankent konfiguralhato)
3. **Zona szamitas**: Teljesitmeny/Pulzus lekepezes 0-3 zonara FTP/max pulzus alapjan
4. **Zona mod**: Teljesitmeny + pulzus zonakat kombinalja (`csak_teljesitmeny`, `csak_pulzus`, `magasabb_nyer`)
5. **Cooldown**: Zona FEL = azonnali, Zona LE = konfiguralhato kesleltetes adaptiv logikaval
6. **Dropout**: Nincs adat N masodpercig → Z0 kenyszerites, atlagolok reset
7. **Kimenet**: `LEVEL:N` kuldes BLE-n keresztul az ESP32-nek
8. **Megjelenit**: HUD elo adatokat mutat 500ms-enkent hangeffektekkel

---

## Zona definiciok

| Zona | Ventilator szint | Teljesitmeny tartomany     | Pulzus tartomany            |
|------|------------------|----------------------------|-----------------------------|
| Z0   | KI               | 0W (nem teker)             | < nyugalmi pulzus           |
| Z1   | Alacsony (33%)   | 1W → z1_max% FTP-bol      | nyugalmi → z1_max% pulzus   |
| Z2   | Kozepes (66%)    | z1%+1 → z2_max% FTP       | z1%+1 → z2_max% pulzus      |
| Z3   | Maximum (100%)   | > z2_max% FTP              | > z2_max% max pulzus         |

---

## Cooldown allapotgep

```
                    uj_zona > jelenlegi
INAKTIV ──────────────────────────────────→ azonnali alkalmazas
    │
    │ uj_zona < jelenlegi
    ▼
AKTIV (idozito fut)
    │
    ├── eses ≥2 zona vagy zona→0  → FELEZES cooldown ido
    ├── fuggoben levo zona emelkedik → DUPLAZAS cooldown ido
    │
    └── idozito lejart            → fuggo zona alkalmazasa → INAKTIV
```

---

## Szalkezeles (Threading) modell

| Szal             | Tipus      | Cel                                              |
|------------------|------------|--------------------------------------------------|
| Fo szal          | -          | Qt esemenyhurok (HUD), jelkezeles                |
| AsyncioThread    | daemon     | Osszes async feladat (BLE, feldolgozas, vezerles)|
| ANT+ szal        | daemon     | openant blokkolo ciklus (queue-n keresztul hidal) |

**Szinkronizacio:**
- `asyncio.Queue` - adatfolyam bemeneti kezelok es feldolgozok kozott
- `asyncio.Lock` - megosztott vezerlo allapot vedelem
- `threading.Lock` - UISnapshot vedelem (HUD ↔ async szal), ANT+ node bontas
- `threading.Event` - leallas koordinacio (megszakithato varakozasok: ANT+ retry, Zwift-varas)
- `loop.call_soon_threadsafe` - task cancel es queue-iras masik szalbol (a `Task.cancel()`
  onmagaban nem szalbiztos)

---

## Jelenlegi fajl struktura

| Fajl | Cel |
|------|-----|
| `zwift_fan_controller.py` | Fo belepo (vekony): az `smart_fan_controller` csomag `app.main()`-jet hivja |
| `zwift_api_polling.py` | Vekony belepo a Zwift API polling segedprocesszhez (logika: `smart_fan_controller/zwift_api/`) |
| `tests/` | Tesztkeszlet (346 teszt): `test_core.py` (domain/config/logging/BLE), `test_pipeline.py` (async adatsik, UDP fogado, protobuf dekoder) |
| `settings.json` | Felhasznaloi konfiguracio (automatikusan letrejon alapertelmezettekkel) |
| `settings.example.json` / `.jsonc` | Konfiguracios sablonok |
| `CONFIGURATION.md` | Beallitasok dokumentacioja |

---

## smart_fan_controller csomag-struktura

A korabbi monolitikus fo fajl es a kulon alfolyamat teljes logikaja a
`smart_fan_controller` csomagba szervezodott; a `zwift_fan_controller.py` mar
csak vekony belepo, ami az `app.main()`-t hivja.

```
smart_fan_controller/
├── app.py               # Belepopont: asyncio event loop + PySide6 HUD osszehangolasa, jelkezeles
├── controller.py        # FanController orchestrator (komponensek + eletciklus)
│
├── config/
│   ├── loader.py        # load_settings(), validacio, save_hud/zwift helperek
│   ├── schemas.py       # Beallitas dataclass-ok + DEFAULT_SETTINGS
│   └── settings.default.json
│
├── core/                # Tiszta domain-logika (PySide6/BLE-fuggetlen, unit-tesztelheto)
│   ├── zones.py         # zone_for_power/hr, calculate_*, apply_zone_mode, is_valid_*
│   ├── averaging.py     # PowerAverager, HRAverager, compute_average
│   ├── cooldown.py      # CooldownController allapotgep
│   ├── printers.py      # ConsolePrinter (throttle-olt)
│   ├── state.py         # ControllerState, UISnapshot (szalbiztos HUD-csere)
│   ├── helpers.py       # resolve_log_dir, generate_tone (hang-ujrageneralas)
│   └── logging_setup.py # logger/user_logger, setup_logging, korai pufferelo
│
├── handlers/            # Be- es kimeneti adatkezelok
│   ├── _ant.py          # ANTPlusInputHandler (daemon szal + asyncio hid)
│   ├── _ble.py          # BLEFanOutputController, BLE szenzor handlerek, send_zone
│   └── zwift_udp.py     # ZwiftUDPInputHandler (a subprocess UDP csomagjait fogadja)
│
├── processors/
│   └── processors.py    # power/hr_processor_task, zone_controller_task, dropout_checker_task
│
├── sounds/              # LCARS hangeffektek (WAV; tools/generate_lcars_sounds.py)
│
├── ui/
│   ├── theme.py         # LCARS szinpaletta + cache-elt QColor/QBrush helperek
│   ├── widgets.py       # QPainter-rel rajzolt LCARS widgetek (header, footer, meterek)
│   ├── sound.py         # LCARSSoundManager (fajl alapu hangeffektek)
│   ├── window.py        # HUDWindow (fo lebego ablak)
│   └── hud.py           # visszafele kompatibilis aggregator (re-export)
│
├── zwift_api/           # Zwift HTTPS API polling segedprocessz (kulon processz)
│   ├── __main__.py      # belepo: settings.json betoltes, CLI, credential feloldas
│   ├── api.py           # ZwiftAuth (OAuth2) + ZwiftAPIClient (REST)
│   ├── decoder.py       # ProtobufDecoder + PlayerState dekodolas
│   ├── runtime.py       # ZwiftDataStore, UDPBroadcaster, run_polling_loop
│   └── logsetup.py      # sajat loggolas (zwift_api_polling.log)
│
└── fonts/               # LCARS Antonio fontok (.ttf)
```

### Fo tervezesi dontesek
- **Vekony belepo**: a `zwift_fan_controller.py` (~76 sor) csak az `app.main()`-t
  hivja, es nehany szimbolumot re-exportal a tesztek/visszafelekompatibilitas miatt.
- **Tiszta mag**: a `core/` csomag PySide6- es BLE-fuggetlen, igy a domain-logika
  (zonazas, atlagolas, cooldown) izolaltan, fuggosegek nelkul unit-tesztelheto.
- **Zwift polling kulon processzben (subprocess + UDP)**: a HTTPS lekerdezes
  (blokkolo `requests`, OAuth2 login, protobuf dekodolas) a fo asyncio loop-tol
  elkulonitve, sajat processzben fut (`smart_fan_controller.zwift_api`), es UDP-n
  (`127.0.0.1:7878`) tovabbitja az adatokat a `ZwiftUDPInputHandler`-nek. Igy a
  blokkolo halozati hivasok es egy esetleges osszeomlas nem zavarja a HUD-ot, a
  bejelentkezes pedig kulon ablakban lathato. A subprocess a kozos `settings.json`
  `zwift_api` szekciojabol olvas (a fo app a `--settings` kapcsoloval inditja).
- **Egyseges queue minta**: minden bemeneti forras (ANT+, BLE, Zwift UDP) ugyanabba
  a `raw_power_queue` / `raw_hr_queue`-ba ir; a feldolgozok forrasfuggetlenek.
- **Feldolgozo task-ok egy helyen**: a `processors/processors.py` tartalmazza a 4
  async task-ot (teljesitmeny/pulzus feldolgozo, zona vezerlo, dropout figyelo).
- **Atomikus settings-mentes**: a `settings.json` frissitese temp fajl + `os.replace`
  parossal tortenik – iras kozbeni leallas (aramszunet) nem hagyhat csonka fajlt.
- **Modern alap**: Python 3.11+ (`StrEnum`, beepitett `TimeoutError`), bleak 3.x,
  PySide6 6.5+ (nativ ablakmozgatas/atmeretezes: `startSystemMove`/`startSystemResize`);
  a config dataclass-ok `slots=True`-val futnak.

### Elonyok
- **Tesztelhetoseg**: a tiszta fuggvenyek (zonazas, cooldown, atlagolas) a `core/`
  csomagbol fuggosegek nelkul, kozvetlenul unit-tesztelhetok.
- **Olvashatosag**: minden modul egyetlen felelosseggel rendelkezik.
- **Karbantarthatosag**: a valtoztatasok az adott modulra korlatozodnak.
- **Izolacio**: a blokkolo Zwift-lekerdezes kulon processzben fut, nem veszelyezteti
  a HUD valaszkeszseget.
- **Egyseges kommunikacio**: minden adatforras ugyanazt az `asyncio.Queue` mintat hasznalja.
