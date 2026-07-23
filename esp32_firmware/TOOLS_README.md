# FanController Diagnosztikai & Teszt Eszközök

## Installáció

Szükséges Python 3.8+ és a `bleak` BLE könyvtár:

```bash
pip install bleak
```

## Eszközök

### 1. `diag_client.py` — Diag napló lekérdezése BLE-n

A `/diag.log` fájlt olvassa le az eszközről. A napló **csak hibákat/diagnosztikát**
tárol (a rutin „sikeres/info" sorok nem), és az **első sora** mindig a stabil
firmware-verzió (`[ver]`, sticky):

```
[ver] 7.14.0
[boot]  CRC32 self-test FAIL -> OTA off. Just serial update!
[boot]  reason=BROWNOUT(11) heap=... min=...
[boot]  loop-break idle n=...
[lowmem] heap=... min=... t=...s
[relay] 1 2 ACTIVE ST zone=...
[relay] 3 STUCK zone=...
[relay] main stuck!
[ota]   bad magic=0x.. size=...
[ota]   crc retry part=... try=...
```

**Használat:**

```bash
# Parancssorból / PowerShell-ből:
python3 diag_client.py

# Paraméterekkel:
python3 diag_client.py --pin 123456 --address AA:BB:CC:DD:EE:FF --clear
```

**Windows SmartScreen blokk:** Ha az inteligens alkalmazáskezelés nem engedi futtatni, használj **PowerShell**-t (Win+X → PowerShell) vagy **cmd**-t, ne kettős kattintást.

**Opciók:**
- `--pin PIN` — BLE AUTH PIN (alapért: 123456)
- `--address MAC` — BLE cím (ha nincs, név alapján keres)
- `--clear` — napló törlése az lekérés után
- `--timeout N` — lekérés timeout másodpercben (alapért: 15)

**Tip:** Futtatás közben időnként ellenőrizve (fejlesztéskor/edzéskor) azonnal látható az új reset/lowmem eset.

---

### 2. `fan_stress.py` — Fokozat-edzés / stressz-teszt

Folyamatosan váltogatja a ventilátor fokozatokat BLE-n, hogy gyorsabban reprodukálható legyen a "30-40 perc után leáll" hiba:

- Relé ki/be kapcsolgatás + fokozat-váltás = a legdurvább terhelés (induktív surge + BLE TX)
- Figyeli a BLE kapcsolat-leállás **idejét** és a **váltásszámot**
- Opcionálisan percenkénti diag-napló ellenőrzés az új reset-ek után

**Használat:**

```bash
# Parancssorból / PowerShell-ből — végtelen edzés, 1→2→3 fokozat, 3 mp-enként:
python3 fan_stress.py

# 45 perc, durva relé-stressz, percenkénti reset-ellenőrzés, CSV naplóval:
python3 fan_stress.py --duration 2700 --roller-toggle --check-interval 60 --log stress.csv

# Gyors edzés újracsatlakozással (leállás után folytatja):
python3 fan_stress.py --dwell 1 --reconnect

# Egyedi fokozat-sorrend:
python3 fan_stress.py --levels 1,1,2,2,3,3
```

**Opciók:**
- `--pin PIN` — BLE AUTH PIN (alapért: 123456)
- `--address MAC` — BLE cím
- `--levels L1,L2,...` — fokozatok sorrendje (alapért: 1,2,3; 0 = ki)
- `--dwell N` — másodperc fokozatonként (alapért: 3)
- `--roller-toggle` — ROLLER ki/be a ciklusok között (durvább stressz)
- `--off-dwell N` — OFF állapot hossza (alapért: 1)
- `--cycles N` — ciklusok száma (0 = végtelen)
- `--duration N` — max futásidő másodpercben (0 = korlátlan)
- `--check-interval N` — DIAG napló-ellenőrzés N mp-enként (0 = ki)
- `--reconnect` — leállás után újracsatlakozás
- `--reconnect-wait N` — várakozás újracsatlakozás előtt (alapért: 5)
- `--log FÁJL` — naplózás CSV/szöveges fájlba

**Tipikus eredmények:**

| Kimenet | Jelentés |
|---|---|
| `KAPCSOLAT MEGSZAKADT 32.1 perc után` | reprodukáltad a leállást |
| `ÚJ RESET a diag naplóban! [boot]` + `reason=BROWNOUT` | tápoldali tüske (relé surge) |
| `ÚJ [lowmem]` | memóriaszivárgás |
| nincs szakadás 1+ órán | a javítások működnek |

---

### 3. `ota_diagnostic.py` — OTA firmware.bin diagnózis

A firmware fájl első byte-jait ellenőrzi (0xE9 = OK, más = rossz fájl → "Decryption error" az eszközön):

```bash
# Parancssorból:
python3 ota_diagnostic.py firmware.bin

# Windows:
ota_diagnostic.bat firmware.bin
```

**Miért kell:** Az OTA "Decryption error" gyakran azt jelenti, hogy rossz fájlt küldtél (pl. `.merged.bin`, `.partitions.bin`, vagy gzip-elt), nem a titkosítással van baj.

---

## Workflow: a "30-40 perc után leáll" hibát keresve

1. **Jelenlegi állapot ellenőrzése:**
   ```bash
   python3 diag_client.py
   ```
   Nézd meg, van-e `[boot] reason=BROWNOUT` → tápoldali instabilitás
   vagy `[lowmem]` → memóriaszivárgás.

2. **Edzés indítása (15-60 perc):**
   ```bash
   python3 fan_stress.py --duration 3600 --check-interval 60 --log stress.log
   ```
   Várd meg, hogy "leálljon", vagy akár 1 órán át fut (akkor jó a javítás).

3. **Eredmény elemzése:**
   - Mikor állt le? Hány váltás után?
   - `stress.log` és `diag_client.py` output → új `[boot] reason=...` vagy `[lowmem]`?

4. **OTA frissítés előtt:**
   ```bash
   python3 ota_diagnostic.py FanController_OTA_debug.ino.bin
   ```
   Ha "magic=0xE9", akkor OK. Ha nem, rossz fájlt választottál.

---

## Gyakori Problémák

### "ModuleNotFoundError: No module named 'bleak'"
```bash
pip install bleak
```

### Windows: SmartScreen blokkolja a futtatást
Az "inteligens alkalmazáskezelés" (Windows Defender SmartScreen) blokkolja a letöltött .py és .bat fájlokat.

**Megoldás: PowerShell vagy cmd parancssor használata**

1. **PowerShell megnyitása:**
   - Win+X → PowerShell
   - vagy: Start Menu → "PowerShell" keresés → jobb klikk → "Run as administrator"

2. **Mappához navigálás és futtatás:**
   ```powershell
   cd C:\Users\[felhasználó]\Desktop\FanController_OTA_debug
   python3 diag_client.py
   ```

3. **Alternatíva: cmd parancssor**
   ```cmd
   C:\>
   cd Documents\FanController_OTA_debug
   python3 fan_stress.py --duration 2700
   ```

**Megjegyzés:** Ha az ablak még így is bezáródik, a Python scriptek már pauzálnak az Enterre: nyomj Entert az ablak bezárásához.

### "Nem található a FanController"
- Az eszköz BLE advertising-ja ki van-e kapcsolva?
- `--address` megadása: `python3 diag_client.py --address AA:BB:CC:DD:EE:FF`

### "AUTH sikertelen"
- Rossz PIN? Alapért: `123456`. Próbáld `--pin`-nel.
- Auth lockout (5 sikertelen kísérlet után) → reset gombbal térj vissza, vagy várd meg a ~5 perces feloldást.

---

## Videó / Közvetlen Monitorozás (fejlesztéskor)

Ha USB-soros hozzáféréssel rendelkezel (nem csak BLE), a serial output többet mutat:

```bash
# Arduino IDE Series Monitor, 115200 baud
# Vagy (Linux): cat /dev/ttyUSB0
# Vagy (Windows): com port -> Putty, 115200
```

Figyeld az `otaLoop()` vagy `stateMachineStep()` sorát, ha "leállást" suspectzalsz.

> **v7.13.0-tól — a soros kimenet feltételhez kötött:** a `Serial.begin(115200)` csak
> akkor fut le, ha a firmware forrásának elején a `DEBUG`, `OTA_DEBUG` vagy `BOOT_DIAG`
> kapcsolók **valamelyike** `1` (alapból `DEBUG=1`, `BOOT_DIAG=1`). Ha mindhárom `0`,
> **nincs soros kimenet** (és a `Serial` el sem indul) — ekkor a diagnózist BLE-n a
> `diag_client.py`-vel kell lekérni. Az OTA per-csomag részletekhez `OTA_DEBUG=1` kell.

---

## Verzió / Firmware

- **v7.6.3**: Diag napló bevezetése (reset ok, lowmem, sleep source)
- **v7.6.4**: OTA magic-byte ellenőrzés a félrevezető "Decryption error" helyett
- **v7.13.0**: AC-érzékelés a relé **bontó (NC) érintkezőjére** került (soros fan-tekercsek miatt), bekötés-leképezés a `FAN_SENSE_AC_MEANS_ENGAGED=0` makróval; a detektálás **LOW-alapú**, így az opto-kimeneti RC-szűrő kiesése sem ad téves STUCK-ot. A STUCK/NOAC diag-naplózás változatlan. A **soros kimenet egységesítve**: `Serial.begin` csak `DEBUG`/`OTA_DEBUG`/`BOOT_DIAG` valamelyikénél.
- **v7.14.0**: `RELAY_ROLLER`→`RELAY_MAIN` (görgő + ventilátor táp); **bootkori relé-önteszt** beragadt fő relé detektálással (`[relay] main stuck!` → failsafe); **CRC32 önteszt → OTA letiltás** FAIL-nél; **diag.log csak hibák** + sticky `[ver]` verziósor + `[relay]` cimkék (a `[sleep]`/health-check-OK info-sorok kivéve).

Frissítés után a diag napló `[ver]`, `[boot]`, `[lowmem]`, `[relay]`, `[ota]` sorokat fog rögzíteni (info-sorok, pl. `[sleep]`, nincsenek).
