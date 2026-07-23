# Smart Fan Controller

Kerékpáros edzés ventilátor vezérlő – ANT+, BLE és Zwift API szenzor adatok alapján automatikusan szabályozza a BLE ventilátort (ESP32).

## Működés

A program valós időben fogadja a teljesítmény (watt) és szívfrekvencia (bpm) adatokat, gördülő átlagot számít, meghatározza a ventilátor zónát (0–3), és BLE-n keresztül elküldi a `LEVEL:N` parancsot az ESP32 vezérlőnek.

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────┐
│  ANT+ Power ├────►│                      ├────►│             │
│  ANT+ HR    │     │   Smart Fan          │     │  ESP32 BLE  │
├─────────────┤     │   Controller         │     │  Ventilátor │
│  BLE Power  ├────►│                      ├────►│  Vezérlő    │
│  BLE HR     │     │  ┌────────────────┐  │     │             │
├─────────────┤     │  │ Gördülő átlag  │  │     │  LEVEL:0–3  │
│  Zwift API  ├────►│  │ Zóna számítás  │  │     └─────────────┘
│  (polling)  │     │  │ Cooldown       │  │
└─────────────┘     │  │ Higher Wins    │  │
                    │  └────────────────┘  │
                    └──────────────────────┘
```

## Fő jellemzők

- **Három adatforrás:** ANT+ (USB dongle), BLE (Bluetooth Low Energy), Zwift API polling – szabadon kombinálhatók, bármelyik `null`-ra állítható a kikapcsoláshoz
- **Három zóna mód:** power_only, hr_only, higher_wins (a magasabb zóna nyer)
- **Adaptív cooldown:** zóna csökkentésnél várakozás (felezés nagy esésnél, duplázás visszaemelkedésnél)
- **Auto-discovery:** BLE és ANT+ eszközök automatikus felderítése és logolása
- **Watchdog:** ANT+ USB dongle kihúzás/lemerülés automatikus detektálása és reconnect
- **Zwift auto-launch:** automatikusan elindítja a Zwift-et ha nem fut (ZwiftLauncher.exe + "Let's Go" gomb – pywinauto)
- **HUD:** Star Trek LCARS stílusú lebegő ablak (PySide6 / Qt6) – valós idejű zóna, watt, HR kijelzés
- **LCARS hangeffektek:** tricorder indítás/leállás, zónaváltás, szenzor események – hangerő és ki/be a jobb klikk menüből
- **Zwift exit figyelés:** ZwiftApp.exe kilépésekor automatikusan leáll (beállítható)
- **Headless mód:** PySide6 nélkül is fut (pl. Raspberry Pi terminálban)

## Telepítés

### Követelmények

- Python 3.11+
- ANT+ USB dongle (pl. Garmin ANT+ Stick) – ha ANT+ forrást használsz
- Bluetooth adapter – ha BLE forrást/kimenetet használsz

### Függőségek

```bash
pip install -r requirements.txt
```

| Csomag | Szükséges? | Funkció |
|--------|-----------|---------|
| `bleak` | Opcionális | BLE kommunikáció (ventilátor + BLE szenzorok) |
| `openant` | Opcionális | ANT+ kommunikáció (power meter, HR monitor) |
| `requests` | Opcionális | Zwift API polling (`zwift_api_polling.py`, ha power/hr forrás `"zwiftudp"`) |
| `pywinauto` | Opcionális | Zwift automatikus indítás (Windows – "Let's Go" gomb megnyomása) |
| `PySide6` | Opcionális | HUD ablak (Star Trek LCARS stílusú Qt6 megjelenítő) |

A program a rendelkezésre álló könyvtárak alapján automatikusan engedélyezi/letiltja az adatforrásokat. Nem kötelező mindet telepíteni.

## Indítás

```bash
# Alapértelmezett settings.json-nal
python zwift_fan_controller.py

# Vagy a példa beállítások másolása után
cp settings.example.json settings.json
# ... settings.json szerkesztése ...
python zwift_fan_controller.py
```

## Konfiguráció

A részletes beállítási leírást lásd: [CONFIGURATION.md](CONFIGURATION.md)

Röviden a `settings.json` fő szekciói:

| Szekció | Tartalom |
|---------|----------|
| `global_settings` | cooldown, buffer, dropout timeout, loggolás (be/ki + könyvtár) |
| `power_zones` | FTP, watt tartomány, zóna százalékok, 0W azonnali leállás |
| `heart_rate_zones` | HR zónák, zone_mode (power_only/hr_only/higher_wins) |
| `ble_fan` | ESP32 ventilátor vezérlő (kimenet) |
| `datasource` | Adatforrás kiválasztás (antplus/ble/zwiftudp/null), ANT+/BLE/Zwift specifikus beállítások |
| `hud` | LCARS hang be/ki, hangerő, Zwift exit figyelés |

Kommentezett referencia: `settings.example.jsonc`

## Típuspéldák

### ANT+ power meter + ANT+ HR → BLE ventilátor

```json
{
  "power_zones": { "ftp": 200 },
  "ble_fan": { "device_name": "FanController" },
  "datasource": {
    "power_source": "antplus",
    "hr_source": "antplus"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### BLE power meter + BLE HR → BLE ventilátor (auto-discovery)

```json
{
  "power_zones": { "ftp": 250 },
  "ble_fan": { "device_name": null },
  "datasource": {
    "power_source": "ble",
    "hr_source": "ble"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### Zwift power + BLE HR → BLE ventilátor (API polling)

```json
{
  "power_zones": { "ftp": 180 },
  "ble_fan": { "device_name": "FanController", "pin_code": 123456 },
  "datasource": {
    "power_source": "zwiftudp",
    "hr_source": "ble"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### Csak power (ANT+), HR nélkül

```json
{
  "power_zones": { "ftp": 200 },
  "datasource": {
    "power_source": "antplus",
    "hr_source": null
  },
  "heart_rate_zones": {
    "zone_mode": "power_only"
  }
}
```

### Csak HR (BLE), power nélkül

```json
{
  "heart_rate_zones": {
    "enabled": true,
    "max_hr": 185,
    "zone_mode": "hr_only"
  },
  "datasource": {
    "power_source": null,
    "hr_source": "ble"
  }
}
```

## Zóna logika

| Zóna | Ventilátor | Power (FTP=200, z1=60%, z2=89%) | HR (max=185, z1=70%, z2=80%) |
|------|------------|------|-------|
| Z0 | Ki | 0W | < 60 bpm |
| Z1 | Alacsony | 1–120W | 60–129 bpm |
| Z2 | Közepes | 121–178W | 130–148 bpm |
| Z3 | Maximum | 179W+ | 149+ bpm |

## Architektúra

```
main()
├── AsyncioThread (daemon)
│   ├── BLEFanOutput         – zone_queue → LEVEL:N BLE GATT write
│   ├── BLEPowerInput*       – BLE notification → raw_power_queue
│   ├── BLEHRInput*          – BLE notification → raw_hr_queue
│   ├── ZwiftUDPInput*       – UDP JSON → raw_power_queue / raw_hr_queue
│   ├── PowerProcessor       – raw_power_queue → átlag → zóna → zone_event
│   ├── HRProcessor          – raw_hr_queue → átlag → zóna → zone_event
│   ├── ZoneController       – zone_event → cooldown → zone_queue
│   └── DropoutChecker       – timeout → Z0
├── ANTPlus-Thread* (daemon)
│   ├── openant Node.start() – blokkoló ANT+ loop
│   └── ANTPlus-Watchdog     – USB disconnect detektálás
└── PySide6 HUD (fő szál)
    └── 500ms polling → UISnapshot

* = opcionális, beállítástól függően
```

## Fájlok

| Fájl | Leírás |
|------|--------|
| `zwift_fan_controller.py` | Fő program belépő (vékony – a logika a `smart_fan_controller/` csomagban) |
| `zwift_api_polling.py` | Vékony belépő a Zwift API polling segédprocesszhez (a logika a `smart_fan_controller/zwift_api/` csomagban) |
| `smart_fan_controller/zwift_api/` | Zwift HTTPS API polling csomag (automatikusan indul, ha power/hr forrás `"zwiftudp"`) |
| `tests/` | Tesztkészlet (346 teszt): domain-logika, config, logging, BLE reconnect, async adatsík – `pytest tests/` |
| `run.bat` / `setup_windows.bat` / `build_exe.bat` | Indítás / venv-telepítés / PyInstaller exe build (Windows) |
| `settings.json` | Aktív beállítások (a Zwift fiók is itt, a `zwift_api` szekcióban) |
| `settings.example.json` | Példa beállítások (alapértelmezett értékek) |
| `settings.example.jsonc` | Kommentezett beállítás referencia |
| `smart_fan_controller.log` | Fő program log (automatikusan generált, ha `logging: true`) |
| `zwift_api_polling.log` | Zwift polling log (automatikusan generált, ha `logging: true`) |
| `ble_devices.log` | Talált BLE eszközök (automatikusan generált) |
| `ant_devices.log` | Talált ANT+ eszközök (automatikusan generált) |
| `CONFIGURATION.md` | Részletes konfigurációs dokumentáció |
| `DEVELOPMENT.md` | Fejlesztői útmutató (környezet, tesztek, konvenciók, bővítési receptek) |
| `CHANGELOG.md` | Verziónkénti változáslista |
| `docs/` | Sphinx API-referencia forrása (`sphinx-build -b html docs docs/_build/html`) |
| `mukodes.odt` | Részletes működési leírás (magyar, felhasználóbarát) |
| `manual.odt` | Felhasználói kézikönyv (telepítés, beállítás, hibaelhárítás) |
| `esp32_firmware/` | Az ESP32 ventilátor-vezérlő firmware másolata + OTA/diagnosztikai eszközök (kanonikus forrás: a FanController_OTA_debug repó; lásd `esp32_firmware/BEVEZETO.md`) |

## ESP32 firmware

A BLE ventilátor vezérlő firmware-je (`FanController_OTA_debug.ino`, **Seeed Studio Xiao ESP32-C3/C6**) az [`esp32_firmware/`](esp32_firmware/) könyvtárban található – a kanonikus forrása a külön [manszabi/FanController_OTA_debug](https://github.com/manszabi/FanController_OTA_debug) repó (a két példányt szinkronban kell tartani; magyarázat: `esp32_firmware/BEVEZETO.md`). A Python program ezzel kommunikál BLE-n keresztül; az alábbi paraméterek a kompatibilitáshoz szükségesek.

**Firmware v7.14.7** – főbb jellemzők:

| Paraméter | Érték |
|-----------|-------|
| BLE device neve | `FanController` |
| Service UUID | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Characteristic UUID | `0000ffe1-0000-1000-8000-00805f9b34fb` |
| Alapértelmezett PIN | `123456` |
| Parancsformátum | `AUTH:<pin>`, `ROLLER:0/1`, `LEVEL:0` – `LEVEL:3`, `DIAG?`, `DIAGCLR` |
| Deep sleep timeout | 1 óra inaktivitás után |
| BLE zóna timeout | 12 perc BLE kapcsolat nélkül → minden lekapcsol (biztonsági) |
| OTA frissítés | BLE-n, CRC32-vel + health-checkkel (`esp32_firmware/sender/ota.py`) |

**Zóna–relé megfeleltetés** (egyszerre mindig csak egy fan-relé aktív):

| Zóna | Ventilátor | Aktív relé |
|------|------------|-------------|
| 0 | Ki | – |
| 1 | 33% | FAN1 |
| 2 | 66% | FAN2 |
| 3 | 100% | FAN3 |

**Szükséges Arduino könyvtár:** OneButton (a többi: ESP32 Arduino core 3.1.3 beépített részei). Fordítás: `esp32_firmware/build.sh`.

## Leállítás

`Ctrl+C` vagy ablak bezárás. A program gondoskodik a tiszta leállításról: BLE disconnect, ANT+ node stop, subprocess terminate. Mindhárom esetben (HUD bezárás, Ctrl+C, ZwiftApp.exe kilépés) tricorder becsukás hang szól a leállás előtt.

## Hibaelhárítás

| Tünet | Ok / megoldás |
|-------|---------------|
| ANT+: „No backend available" vagy libusb hiba (a program célzott figyelmeztetést is ír) | Az ANT+ stick meghajtója hiányzik. Windows 11: telepíts **WinUSB** meghajtót a stickre (pl. a [Zadig](https://zadig.akeo.ie/) eszközzel), majd húzd ki és dugd vissza. |
| Minden beállítás váratlanul alapértelmezett | A `settings.json` szintaxis- vagy típushibás. A program a hibás fájlt `settings.json.incorrect` néven félreteszi – javítsd ki (a log megadja a sort/oszlopot), és nevezd vissza. |
| Egy beállításod „nem érvényesül" | Elgépelt szekció- vagy mezőnév. A program a logban ⚠-tel jelzi az ismeretlen szekciókat és az érvénytelen értékeket. |
| Zwift login hiba a polling ablakban | A hibaüzenet megadja az okot (pl. „Invalid user credentials" = rossz jelszó). A hitelesítés a `settings.json` `zwift_api` szekciójából vagy a `ZWIFT_USERNAME`/`ZWIFT_PASSWORD` környezeti változókból jön. |
| BLE eszköz lassan csatlakozik | Név alapú keresésnél a program azonnal csatlakozik, amint az eszköz hirdet – ha mégis lassú, az eszköz nincs hatótávon belül vagy nem hirdet. |

## Tesztek

```bash
pip install pytest
pytest tests/
```

A készlet 346 tesztet tartalmaz: tiszta domain-logika (zónák, cooldown, átlagolás), config-validáció, logging, BLE reconnect forgatókönyvek, valamint a teljes async adatsík (power minta → zóna parancs → dropout) és a Zwift protobuf dekóder.
