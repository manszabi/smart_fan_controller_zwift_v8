# esp32_firmware/ – a ventilátor-vezérlő firmware (magyarázat)

Ez a könyvtár a Smart Fan Controllerhez tartozó **ESP32 firmware-projekt**
másolatát tartalmazza. A firmware a hardveres oldal: ez fut a Seeed Studio
XIAO ESP32-C3/C6 lapkán, ez kapcsolja a reléket, és ez fogadja BLE-n a fő
program parancsait.

> **A kanonikus (elsődleges) forrás:** a
> [manszabi/FanController_OTA_debug](https://github.com/manszabi/FanController_OTA_debug)
> repó. Az itteni másolat kényelmi célú, hogy a szoftver és a firmware egy
> helyen legyen áttekinthető. Módosítást mindig **mindkét helyre** vezess át
> (ez a másolat a firmware-repó `v7.14.7` állapotát tükrözi).

## Hogyan kapcsolódik a fő programhoz?

A Python-oldali `smart_fan_controller` BLE-n ezekkel a parancsokkal vezérli
a firmware-t (service `0000ffe0-…`, characteristic `0000ffe1-…`):

| Parancs | Küldi | Jelentés |
|---|---|---|
| `AUTH:<pin>` | csatlakozás után | PIN-azonosítás (válasz: `AUTH_OK` / `AUTH_FAIL` / `AUTH_LOCKED`) |
| `ROLLER:1` / `ROLLER:0` | csatlakozáskor / leállításkor | fő relé (görgő + ventilátor-táp) be/ki |
| `LEVEL:0` … `LEVEL:3` | zónaváltáskor | ventilátor-fokozat (0=ki, 1/2/3 = FAN1/FAN2/FAN3 relé) |
| `DIAG?` / `DIAGCLR` | kézi diagnosztika | hibanapló lekérése / törlése |

A Python-oldal beállításai (`settings.json` → `ble_fan`) és a firmware
konstansai összetartoznak: azonos PIN (`123456` gyárilag), azonos UUID-k,
eszköznév `FanController`.

## Mi mit csinál ebben a könyvtárban?

| Fájl | Szerep |
|---|---|
| `FanController_OTA_debug.ino` | **A firmware maga** (Arduino vázlat, ~2700 sor): BLE vezérlés + PIN-auth, relé-állapotgép break-before-make váltással, kézi (gombos) mód, failsafe-védelem, relé-visszajelzés figyelés (H11AA1M optocsatoló), fokozat-mentés áramszünetre (RTC+NVS), BLE OTA frissítés CRC-vel és health-checkkel, deep sleep, diagnosztikai napló. |
| `README.md` | A firmware saját, részletes (magyar) dokumentációja: hardver/pinkiosztás, üzemmódok, gombvezérlés, OTA, hibaelhárítás. **Ezt olvasd először.** |
| `verhistory.md` | A firmware teljes verziótörténete (v7.0.0 → v7.14.7). |
| `partitions_custom.csv` | Egyedi flash-partíciós tábla (két OTA app-partíció + SPIFFS az OTA-átmenethez). |
| `build.sh` | Fordítás arduino-cli-vel (XIAO ESP32-C3 alapból; `TARGET=c6` a C6-hoz). |
| `sender/ota.py` | **BLE OTA feltöltő**: az új firmware `.bin` feltöltése vezeték nélkül (részenkénti CRC32-vel, újraküldéssel). `sender/discover.py`: BLE-eszközök listázása; `sender/run.bat`: Windows-indító. |
| `diag_client.py` | A készülék hibanaplójának (diag.log) lekérése BLE-n (`DIAG?`). |
| `fan_stress.py` | Stressz-teszt: fokozatok folyamatos váltogatása a ritka, terhelés alatti hibák (pl. brownout) kiprovokálására. |
| `serial_monitor.py` | Egyszerű soros monitor a fejlesztéshez/debughoz. |
| `ota_diagnostic.py` | Firmware `.bin` gyors ellenőrzése OTA előtt (magic byte, partíció-méret). |
| `OTA_TROUBLESHOOTING.md` | OTA-hibaelhárítási jegyzetek. |
| `TOOLS_README.md` | A fenti Python-eszközök részletes használati leírása. |

## Fordítás és telepítés dióhéjban

1. **Fordítás:** `./build.sh` (arduino-cli + esp32 core 3.1.3 + OneButton
   könyvtár szükséges; részletek a `build.sh` fejlécében). Arduino IDE-ből is
   fordítható – board: *XIAO_ESP32C3*, partíció: `partitions_custom.csv`.
2. **Első feltöltés:** USB-n (utána már mehet vezeték nélkül).
3. **További frissítések OTA-val:**
   `python sender/ota.py "<MAC-cím>" "FanController_OTA_debug.ino.bin"`
4. **Ellenőrzés:** a HUD-ból vagy a `diag_client.py`-jal lekérdezhető a
   diag.log – az első sora a futó stabil verzió (`[ver] 7.14.7`).

## A 2026-07-23-i átvilágítás eredménye (v7.14.7)

A firmware-t teljes egészében átnéztem; a kód kiforrott (40+ dokumentált
korábbi javításon van túl). A talált és javított apróságok – normál
működésben mind viselkedés-azonosak:

- **[FIX-ESP-49]** `handleZoneChange`: a `millis()` túlcsordulásakor
  (~49,7 naponta egyszer) a relék közti 10 ms-os break-before-make védőidő
  kimaradhatott volna – wrap-biztos időzítésre javítva.
- **[MOD-14]** Halott `currentMillis` globális változó eltávolítva.
- **[MOD-15]** `rebootEspWithReason`: az újraindítás oka mostantól megjelenik
  a debug-kimenetben.
- **[MOD-16]** `ota_diagnostic.py`: beégetett abszolút útvonal helyett a
  szkript saját könyvtárát használja (hordozhatóság).

Mindezek a javítások a kanonikus firmware-repóba is bekerültek
(commit: v7.14.7).
