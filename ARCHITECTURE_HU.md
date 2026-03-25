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
│  │  │  ANT+ Bemenet│  │  BLE Bemenet │  │   Zwift UDP Bemenet   │  │   │
│  │  │  (Szal)      │  │  (async)     │  │   (async szerver)     │  │   │
│  │  │              │  │              │  │   127.0.0.1:7878       │  │   │
│  │  │ ┌──────────┐ │  │ ┌──────────┐ │  │                       │  │   │
│  │  │ │Teljesit- │ │  │ │Teljesit- │ │  │  ┌─────────────────┐  │  │   │
│  │  │ │menymero  │ │  │ │meny      │ │  │  │zwift_api_polling│  │  │   │
│  │  │ │(openant) │ │  │ │(0x1818)  │ │  │  │.py (alfolyamat) │  │  │   │
│  │  │ └──────────┘ │  │ └──────────┘ │  │  │ OAuth2 + Proto  │  │  │   │
│  │  │ ┌──────────┐ │  │ ┌──────────┐ │  │  │ → UDP JSON      │  │  │   │
│  │  │ │Pulzus-   │ │  │ │Pulzus    │ │  │  └─────────────────┘  │  │   │
│  │  │ │mero      │ │  │ │(0x180D)  │ │  │                       │  │   │
│  │  │ │(openant) │ │  │ └──────────┘ │  │                       │  │   │
│  │  │ └──────────┘ │  │              │  │                       │  │   │
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
| zwift_api_polling| alfolyamat | Zwift OAuth2 lekerdez → UDP kuldes               |

**Szinkronizacio:**
- `asyncio.Queue` - adatfolyam bemeneti kezelok es feldolgozok kozott
- `asyncio.Lock` - megosztott vezerlo allapot vedelem
- `threading.Lock` - UISnapshot vedelem (HUD ↔ async szal)
- `threading.Event` - leallas koordinacio

---

## Fajl struktura

| Fajl | Cel |
|------|-----|
| `swift_fan_controller_new_v8_PySide6.py` | Fo alkalmazas (~5300 sor): teljes logika, HUD, vezenyels |
| `zwift_api_polling.py` | Zwift API alfolyamat: OAuth2, protobuf dekodolas, UDP kuldes |
| `esp32_fan_controller.ino` | ESP32-C3 firmware: BLE szerver, rele vezerles, OTA |
| `settings.json` | Felhasznaloi konfiguracio (automatikusan letrejon alapertelmezettekkel) |
| `settings.example.json` / `.jsonc` | Konfiguracios sablonok |
| `CONFIGURATION.md` | Beallitasok dokumentacioja |

---

## Javasolt refaktoralasi struktura

A fo fajl (`swift_fan_controller_new_v8_PySide6.py`, ~5300 sor) felbonthato lenne:

```
smart_fan_controller/
├── __main__.py              # Belepesi pont, argumentum feldolgozas
├── config/
│   ├── loader.py            # load_settings(), validacio, alapertelmezettek
│   └── schemas.py           # Beallitas dataclass-ok/TypedDict-ek
├── core/
│   ├── controller.py        # FanController fo vezerloelemem
│   ├── zones.py             # zone_for_power(), zone_for_hr(), apply_zone_mode()
│   ├── cooldown.py          # CooldownController allapotgep
│   ├── averager.py          # PowerAverager, HRAverager
│   └── dropout.py           # Dropout (adatvesztes) eszleles logika
├── input/
│   ├── antplus.py           # ANTPlusInputHandler
│   ├── ble_power.py         # BLEPowerInputHandler
│   ├── ble_hr.py            # BLEHRInputHandler
│   └── zwift_udp.py         # ZwiftUDPInputHandler
├── output/
│   └── ble_fan.py           # BLEFanOutputController
├── hud/
│   ├── window.py            # HUDWindow (PySide6 LCARS felhasznaloi felulet)
│   ├── sounds.py            # Hang generaas es lejatszas
│   └── theme.py             # LCARS szinek, betutipusok, stilusok
└── zwift/
    └── api_polling.py       # Zwift API lekerdezs (alfolyamat)
```

### Elonyok
- **Tesztelhetoseg**: Tiszta fuggvenyek (zonazas, cooldown) konnyen unit-tesztelhetok
- **Olvashatosag**: Minden fajl egyetlen felelosseggel rendelkezik (~200-500 sor)
- **Karbantarthatosag**: Valtoztatasok az adott modulra korlatozodnak
- **Ujrafelhasznalhatosag**: Bemeneti kezelok, atlagolok, cooldown logika fuggetlenul hasznalhato
