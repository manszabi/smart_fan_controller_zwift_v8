# esp32_firmware/ – a ventilátor-vezérlő firmware (magyarázat)

Ez a könyvtár a Smart Fan Controllerhez tartozó **ESP32 firmware-projekt**
másolatát tartalmazza. A firmware a hardveres oldal: ez fut a Seeed Studio
XIAO ESP32-C3/C6 lapkán, ez kapcsolja a reléket, és ez fogadja BLE-n a fő
program parancsait.

> **A kanonikus (elsődleges) forrás:** a
> [manszabi/FanController_OTA_debug](https://github.com/manszabi/FanController_OTA_debug)
> repó. Az itteni másolat kényelmi célú, hogy a szoftver és a firmware egy
> helyen legyen áttekinthető. Módosítást mindig **mindkét helyre** vezess át
> (ez a másolat a firmware-repó `v7.18.0` állapotát tükrözi).

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
| `verhistory.md` | A firmware teljes verziótörténete (v7.0.0 → v7.18.0). |
| `partitions_custom.csv` | Egyedi flash-partíciós tábla (két OTA app-partíció + a `spiffs` címkéjű adatpartíció az OTA-átmenethez — v7.18.0 óta LittleFS fut rajta). |
| `build.sh` | Fordítás arduino-cli-vel (XIAO ESP32-C3 alapból; `TARGET=c6` a C6-hoz). |
| `sender/ota.py` | **BLE OTA feltöltő**: az új firmware `.bin` feltöltése vezeték nélkül (részenkénti CRC32-vel, újraküldéssel). `sender/discover.py`: BLE-eszközök listázása; `sender/run.bat`: Windows-indító. |
| `diag_client.py` | A készülék hibanaplójának (diag.log) lekérése BLE-n (`DIAG?`). |
| `fan_stress.py` | Stressz-teszt: fokozatok folyamatos váltogatása a ritka, terhelés alatti hibák (pl. brownout) kiprovokálására. |
| `serial_monitor.py` | Egyszerű soros monitor a fejlesztéshez/debughoz. |
| `ota_diagnostic.py` | Firmware `.bin` gyors ellenőrzése OTA előtt (magic byte, partíció-méret). |
| `OTA_TROUBLESHOOTING.md` | OTA-hibaelhárítási jegyzetek. |
| `TOOLS_README.md` | A fenti Python-eszközök részletes használati leírása. |

## Fordítás és telepítés dióhéjban

1. **Fordítás:** `./build.sh` (arduino-cli + esp32 core 3.3.11 + OneButton
   könyvtár szükséges; részletek a `build.sh` fejlécében). Arduino IDE-ből is
   fordítható – board: *XIAO_ESP32C3*, partíció: `partitions_custom.csv`.
2. **Első feltöltés:** USB-n (utána már mehet vezeték nélkül).
3. **További frissítések OTA-val:**
   `python sender/ota.py "<MAC-cím>" "FanController_OTA_debug.ino.bin"`
4. **Ellenőrzés:** a HUD-ból vagy a `diag_client.py`-jal lekérdezhető a
   diag.log – az első sora a futó stabil verzió (`[ver] 7.18.0`).

## Mi változott v7.14.9 óta (v7.15.0 – v7.18.0)

- **v7.15.0 – `[FIX-ESP-55]` deep sleep alatt beragadó görgő-relé.** Alvás közben a
  digitális IO tápdomain lekapcsol, a GPIO-k lebegnek, így szivárgó áram vagy zaj
  behúzhatta a `RELAY_MAIN`-t; ébredéskor a bootkori relé-önteszt „beragadt fő relét"
  jelzett → failsafe → leállás. Megoldás: **pad-hold** (`relayPadsHoldEnable()`) az
  always-on tápdomainben, feloldás a `setup()`-ban a lábak biztonságos szintre
  hajtása **után**. (C6-on a hold a bootloader alatt is él, ezért a feloldás kötelező.)
- **v7.16.0 – átvilágítás + toolchain a `esp32:esp32@3.3.11` core-ra** (IDF 5.5; a 3.3-as
  core-tól az alapértelmezett BLE stack **NimBLE**, nem Bluedroid). `[FIX-ESP-57]`: bukott
  OTA-telepítés után az eszköz **véglegesen OTA-módban ragadt** (nem futott a gomb, a
  failsafe, a relé-figyelés és az NVS-mentés sem) → a `performUpdate()` bukásakor
  kötelező `otaResetState()`.
- **v7.16.1 – `[FIX-ESP-63]`:** a boot-kori watchdog-konfiguráció **némán elbukhatott**,
  és a gyári 5000 ms maradt a szándékolt 15 000 ms helyett; a visszatérési értékek
  most ellenőrzöttek.
- **v7.16.2 – `[FIX-ESP-64]`:** a watchdog már csak a `loop()`-ot figyeli
  (`idle_core_mask = 0`), nem az idle taskot.
- **v7.17.0 – karbantarthatóság, viselkedés-semlegesen:** fordítási idejű `static_assert`
  védőhálók (pin-ütközés) és duplikációk kiemelése; a bináris kissé kisebb lett.
- **v7.18.0 – `[FIX-ESP-65]` SPIFFS → LittleFS** a `spiffs` partíción. **A partíciós tábla
  nem változik** (a `LittleFS.begin()` alapból ezt a címkét csatolja). Indok:
  áramszünet-biztonság (copy-on-write — a `diag.log` épp brownout/WDT reset után, bootkor
  íródik, az OTA pedig ~0,7 MB-ot stagel) és jobb viselkedés telített fájlrendszeren.
  > **Egyszeri hatás a frissítéskor:** az első boot a régi, SPIFFS-formátumú partíciót nem
  > tudja felcsatolni, ezért **megformázza** → a korábbi `diag.log` elveszik. Ez a naplóban
  > is látszik: `[fs] mount failed -> formatted`.

## A 2026-08-24-i átvilágítás eredménye (v7.14.8 – v7.14.9)

Két további kör, immár tényleges fordítással ellenőrizve (XIAO ESP32-C3 és C6,
`--warnings all` mellett 0 hiba / 0 figyelmeztetés).

**Kritikus regresszió – érdemes tudni róla:**

- **[FIX-ESP-50]** A 2026-06-27-i „5 gombnyomás" (relé-figyelés bypass) módosítás
  tévedésből a **hibás reset utáni boot-helyreállítást** is a bypass-kapcsoló alá
  tette. Mivel a bypass alapból **ki van kapcsolva**, ez gyári beállításban
  **teljesen kiiktatta** a fő relé + ventilátorfokozat automatikus visszaállítását
  BROWNOUT/WDT/panic reset után – vagyis visszahozta azt a „holtan marad" tünetet,
  amit a `[FIX-ESP-19/25/30/39/40]` sorozat korábban megszüntetett. Javítva:
  a visszaállítás ismét feltétel nélküli, a bypass csak a dokumentált hatáskörére
  (reléfigyelés + bootkori relé-önteszt) vonatkozik.

**További javított hibák:**

- **[FIX-ESP-51]** OTA **heap-túlolvasás**: a `0xFC` part-vége csomag ellenőrizetlen
  hosszmezője a 16 KB-os pufferen túlra olvastatott (sérült hosszmező vagy eltérő
  `PART`-méretű kliens esetén akár ~48 KB) → határellenőrzés + abort.
- **[FIX-ESP-52]** A `DIAGCLR` egy futó `DIAG?` stream közben csonkolta a naplót,
  miközben nyitott olvasó-handle volt rajta → a törlés most megvárja a stream végét.
- **[FIX-ESP-53]** További `millis()`-túlcsordulásra érzékeny határidők (OTA-reboot,
  OTA-telepítés, BLE-vs-gomb forrás-prioritás zárolás) wrap-biztosra javítva.

**Memória- és processzoridő-takarékosság:**

- **[MOD-23]** Az OTA forró útján csomagonként egy felesleges `String` heap-másolat
  született (~11 500 alkalommal egy 1,1 MB-os firmware-nél) – megszüntetve.
- **[MOD-24]** Mind az 5 BLE-parancság `String`-allokációval indult egy fordítási
  időben eldönthető feltételhez – `constexpr`-re cserélve.
- **[MOD-25…27]** Érték szerinti `String` paraméterek, halott globálisok/include,
  és egy bontáskor vissza nem álló OTA-flag rendezése.

A Python segédeszközök is kaptak javításokat (`serial_monitor.py` szál-szivárgás
újracsatlakozáskor, `fan_stress.py` Python 3.8-kompatibilitás, `ota_diagnostic.py`
hibás `0x0x…` kiírás). Részletek: `verhistory.md`.

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
