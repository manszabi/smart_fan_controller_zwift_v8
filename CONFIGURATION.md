# Konfiguráció – Smart Fan Controller

A program a `settings.json` fájlból olvassa a beállításokat. Ha ez a fájl még nem létezik, az induláskor automatikusan létrejön a beépített **default sablonból** (`settings.default.json`) másolva. A kommentezett referenciát lásd: `settings.example.jsonc`.

---

## Konfigurációs fájlok

| Fájl | Szerep |
|------|--------|
| `settings.json` | **A te beállításaid.** Ezt szerkeszted; ezt olvassa a program. Első indításkor jön létre. |
| `smart_fan_controller/config/settings.default.json` | **Beépített default sablon** (package data, verziókövetett). Ebből készül a `settings.json` első indításkor. |
| `settings.example.json` | A default pontos, gép-olvasható másolata (kényelmi sablon). |
| `settings.example.jsonc` | Ugyanazok az értékek **kommentekkel** – ez a dokumentált referencia. |

**Betöltési logika (`load_settings`):**

1. Ha a `settings.json` **nem létezik**, a program megpróbálja átmásolni a default sablont:
   - előbb az aktuális könyvtárban (CWD) lévő `settings.default.json`-t (ha van saját felülíró sablonod),
   - majd a beépített package data sablont.
2. Beolvassa a `settings.json`-t, és minden mezőt validál. **Hibás mező esetén** az adott mező alapértelmezett értéke marad érvényben (figyelmeztetéssel), a többi beállítás betöltődik.
3. Ha semmilyen sablon nem érhető el, a beépített (hardcoded) alapértelmezések lépnek életbe.

> ℹ️ A `settings.example.json` és `.jsonc` mindig a `settings.default.json`-t tükrözi; ezt automatikus teszt is őrzi, így a sablonok nem csúsznak el a tényleges alapértelmezésektől.

### Kétféle hiba – kétféle hatókör

A program kétféleképpen reagál a hibákra, attól függően, hogy **érték-hiba** vagy **szintaxis-hiba** történt:

| | **Rossz ÉRTÉK** (érvényes JSON) | **Rossz JSON SZINTAXIS** |
|---|---|---|
| **Példa** | `"ftp": "kétszáz"` (szöveg szám helyett) | hiányzó vessző, zárójel, lezáratlan idézőjel |
| **JSON értelmezhető?** | ✅ Igen | ❌ Nem |
| **Hatókör** | Csak az érintett **mező** | A **teljes fájl** |
| **Jó értékek sorsa** | ✅ Megmaradnak | ❌ Mind elvész (teljes default) |
| **Log** | mezőnként figyelmeztetés | egyetlen "beolvasási hiba" + sor/oszlop |

> ⚠️ **Gyakorlati tanács:** ha JSON szintaxis hibát látsz a logban, és váratlanul **minden** alapértelmezett, akkor egyetlen elgépelés (hiányzó vessző, zárójel, lezáratlan idézőjel) az egész fájlt blokkolja. A hibaüzenet megadja a pontos sort és oszlopot (pl. `Expecting ',' delimiter: line 5 column 5`) – érdemes JSON-validátorral vagy a megadott sor/oszlop alapján ellenőrizni.

> 💾 **Automatikus mentés szintaxis-hibánál:** ha a `settings.json` JSON szintaxisa hibás, a program a default-okra váltás **előtt** félreteszi a hibás fájlt `settings.json.incorrect` néven. Így a sok kézi szerkesztésed **nem vész el** akkor sem, ha a program később (pl. HUD ablakpozíció mentésekor) felülírná a `settings.json`-t a default értékekkel. Teendő: nyisd meg a `settings.json.incorrect` fájlt, javítsd ki a hibát (a logban jelzett sor/oszlop alapján), majd nevezd vissza `settings.json`-ra. Megjegyzés: a `.incorrect` mindig a legutóbbi hibás verziót őrzi (felülíródik).

---

## Gyors kezdés

1. Indítsd el a programot egyszer – ekkor automatikusan létrejön a `settings.json` az alapértelmezésekkel. (Vagy másold kézzel a `settings.example.json`-t `settings.json` néven.)
2. Állítsd be az FTP értékedet (`power_zones.ftp`), és szükség esetén a cooldown-t (`global_settings.cooldown_seconds`).
3. Válaszd ki az adatforrást (`datasource.power_source`, `datasource.hr_source`).
4. Ha BLE ventilátort használsz, állítsd be a `ble.device_name` mezőt (vagy hagyd `null`-on az auto-discovery-hez).
5. Indítsd el: `python swift_fan_controller_new_v8_PySide6.py`

---

## Globális beállítások (`global_settings`)

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `cooldown_seconds` | int | 0–600 | 120 | Cooldown idő zóna csökkentésnél (másodperc). 0 = azonnali váltás (nincs cooldown). |
| `buffer_seconds` | int | 1–60 | 3 | Gördülő átlag ablak (fallback ha forrás-specifikus nincs). |
| `minimum_samples` | int | 1–600 | 6 | Minimum minta érvényes átlaghoz (fallback). |
| `buffer_rate_hz` | int | 1–60 | 4 | Várt mintavételi frekvencia Hz-ben (fallback). |
| `dropout_timeout` | int | 1–120 | 5 | Adatforrás kiesés timeout másodpercben (fallback). |
| `logging` | bool | – | true | Globális loggolás be/ki. Ha `false`, nincs sem fájl-, sem konzol-loggolás (teljes némaság) – csak az indítási összefoglaló jelenik meg. |
| `log_directory` | string\|null | – | null | Log fájlok könyvtára. `null` = a program könyvtára. Fájlok: `smart_fan_controller.log`, `ble_devices.log`, `ant_devices.log`. Ha a megadott könyvtár nem létezik vagy nem írható, automatikusan a program könyvtárát használja. |

### Validációs viselkedés

A beállítások betöltésekor minden mező típus- és tartomány-ellenőrzésen megy
keresztül. Érvénytelen érték esetén az **alapértelmezett marad**, és
figyelmeztetés (⚠) kerül a logba.

**Numerikus mezők (`_from_dict_int`):**

| Bemenet | Eredmény | Figyelmeztetés |
|---------|----------|----------------|
| Tartományon belüli egész (pl. `5`) | elfogadva | – |
| Egész értékű float (pl. `5.0`) | konvertálva (`5`) | – |
| Törtrészes float (pl. `5.7`) | default marad | ⚠ törtrész nem elfogadott |
| Tartományon kívül (pl. `9999`, `-1`) | default marad | ⚠ tartomány |
| Bool (`true`/`false`) | default marad | ⚠ (a bool nem egész) |
| String vagy egyéb típus | default marad | ⚠ tartomány |
| Hiányzó kulcs | default marad | – |

**Kereszt-validáció (`__post_init__`):** a `minimum_samples` nem lehet nagyobb
mint `buffer_seconds × buffer_rate_hz`. Ha mégis, akkor a maximumra
(`buffer_seconds × buffer_rate_hz`) csökken + ⚠. Ez minden példányosításnál
érvényesül, nem csak betöltéskor.
_Példa:_ `buffer_seconds=3`, `buffer_rate_hz=2` → max `6`; ha `minimum_samples=100`, akkor `6`-ra korrigálódik.

**`log_directory` kezelés:**

| Bemenet | Eredmény | Figyelmeztetés |
|---------|----------|----------------|
| `null`, `"null"` string (kis/nagybetű, trimmelt), vagy hiányzó kulcs | program könyvtár | – (csendes) |
| Valódi útvonal (pl. `"/var/log"`) | trimmelve elmentve | – |
| Üres / whitespace string (`""`, `"   "`) | program könyvtár | ⚠ üres érték |
| Rossz típus (`123`, `true`, lista, dict) | program könyvtár | ⚠ string vagy null kell |

**Runtime feloldás** (ha valódi útvonalat adtak meg):

| Helyzet | Eredmény | Figyelmeztetés |
|---------|----------|----------------|
| Könyvtár létezik, írható | használja | – |
| Nem létezik, de létrehozható | létrehozza (szülőkkel), használja | – |
| Nem hozható létre / nem írható | program könyvtár (fallback) | ⚠ nem elérhető |

---

## Teljesítmény zóna határok (`power_zones`)

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `ftp` | int | 100–500 | 200 | Funkcionális küszöbteljesítmény (watt). |
| `min_watt` | int | 0–9999 | 0 | Minimális érvényes pozitív watt. |
| `max_watt` | int | 1–100000 | 1000 | Maximális érvényes watt. |
| `z1_max_percent` | int | 1–100 | 60 | Z1 felső határ az FTP %-ában. |
| `z2_max_percent` | int | 1–100 | 89 | Z2 felső határ az FTP %-ában. |
| `zero_power_immediate` | bool | – | false | Ha true, 0W → azonnali LEVEL:0 (cooldown nélkül). |

**Zóna kiosztás:**

- **Z0:** 0W – ventilátor kikapcsolva
- **Z1:** 1W – FTP × z1_max_percent%
- **Z2:** FTP × z1_max_percent% + 1 – FTP × z2_max_percent%
- **Z3:** FTP × z2_max_percent% + 1 – max_watt

**Validáció:** 
- A program automatikusan javítja ha `min_watt >= max_watt` vagy `z1_max_percent >= z2_max_percent`.
- A `zero_power_immediate` mező szigorú típusellenőrzés alatt áll: csak a `true` és `false` logikai értékek (JSON boolean) elfogadottak. 
  - **Hibás érték:** `"true"` (string), `"tue"` (typo), `1` vagy `0` (egész szám), `null`, vagy bármilyen egyéb érték
  - **Viselkedés:** a hibás érték figyelmeztetéssel (`⚠ Érvénytelen 'zero_power_immediate' érték: ...`) az alapértelmezés (false) marad, és a többi beállítás betöltődik
  - **Helyes formátum:** `"zero_power_immediate": true` vagy `"zero_power_immediate": false` (JSON boolean, nem string)

---

## Szívfrekvencia zónák (`heart_rate_zones`)

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `enabled` | bool | – | true | Ha false, HR adatok csak megjelennek de nem befolyásolják a ventilátort. |
| `max_hr` | int | 100–220 | 185 | Maximális szívfrekvencia (bpm). |
| `resting_hr` | int | 30–100 | 60 | Pihenő szívfrekvencia (bpm). |
| `zone_mode` | string | lásd lent | `"higher_wins"` | Zóna kombináció módja. |
| `z1_max_percent` | int | 1–100 | 70 | Z1 felső határ a max_hr %-ában. |
| `z2_max_percent` | int | 1–100 | 80 | Z2 felső határ a max_hr %-ában. |
| `valid_min_hr` | int | 30–100 | 30 | Érvényes HR alsó határ szűréshez. |
| `valid_max_hr` | int | 150–300 | 220 | Érvényes HR felső határ szűréshez. |
| `zero_hr_immediate` | bool | – | false | Ha true, 0 HR zóna → azonnali LEVEL:0 (cooldown nélkül). |

**Zóna módok:**

- `"power_only"` – csak a teljesítmény zóna dönt (HR figyelmen kívül) – ilyenkor `hr_source` állítható `null`-ra
- `"hr_only"` – csak a HR zóna dönt (power figyelmen kívül) – ilyenkor `power_source` állítható `null`-ra
- `"higher_wins"` – a kettő közül a magasabb zóna érvényesül

**HR zóna kiosztás:**

- **Z0:** resting_hr alatt
- **Z1:** resting_hr – max_hr × z1_max_percent%
- **Z2:** max_hr × z1_max_percent% + 1 – max_hr × z2_max_percent%
- **Z3:** max_hr × z2_max_percent% felett

**Validáció:**
- Az `enabled` és `zero_hr_immediate` mezők szigorú típusellenőrzés alatt állnak: csak a `true` és `false` logikai értékek (JSON boolean) elfogadottak.
  - **Helyes formátum:** `"enabled": true` vagy `"zero_hr_immediate": false` (JSON boolean, nem string)
  - **Hibás érték:** `"true"` (string), `1` vagy `0` (egész szám), `null`, vagy bármilyen egyéb érték
  - **Viselkedés:** a hibás érték figyelmeztetéssel az alapértelmezés marad, és a többi beállítás betöltődik

---

## BLE ventilátor kimenet (`ble`)

Az ESP32 BLE vezérlőhöz való csatlakozás beállításai. A program `LEVEL:N` (N=0–3) parancsokat küld a GATT karakterisztikára.

> **Firmware:** a projekthez tartozó `esp32_fan_controller.ino` (Xiao ESP32-C3, v5.2.0) alapértelmezetten a lenti UUID-kat és `123456` PIN-t használja. Ha az alapértékeket megtartod, csak a `device_name` mezőt kell beállítani (vagy hagyni `null`-on az auto-discoveryhez).

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `device_name` | string/null | – | null | BLE eszköz neve. `null` vagy `""` → auto-discovery. |
| `scan_timeout` | int | 1–60 | 10 | BLE keresés timeout (mp). |
| `connection_timeout` | int | 1–60 | 15 | Csatlakozási timeout (mp). |
| `reconnect_interval` | int | 1–60 | 5 | Újracsatlakozás várakozási ideje (mp). |
| `max_retries` | int | 1–100 | 10 | Max újrapróbálkozás, utána 30s cooldown. |
| `command_timeout` | int | 1–30 | 3 | GATT write timeout (mp). |
| `service_uuid` | string | – | `0000ffe0-...` | BLE service UUID. |
| `characteristic_uuid` | string | – | `0000ffe1-...` | BLE characteristic UUID. |
| `pin_code` | int/string/null | – | null | Alkalmazás szintű PIN. `null` → nincs auth. |

**Auto-discovery:** ha `device_name` `null` vagy üres, a program automatikusan megkeresi a `service_uuid`-t hirdető eszközt. A talált eszközök a `ble_devices.log` fájlba kerülnek.

**PIN kód:** megadható int-ként (`123456`) vagy string-ként (`"012345"` ha vezető nulla szükséges). Max 20 karakter. Az ESP32 firmware alapértelmezett PIN-je `123456` – ha módosítod az `esp32_fan_controller.ino`-ban (`BLE_AUTH_PIN`), itt is frissítsd.

---

## Adatforrás beállítások (`datasource`)

### Forrás kiválasztása

| Mező | Értékek | Alapértelmezett | Leírás |
|------|---------|-----------------|--------|
| `power_source` | `"antplus"`, `"ble"`, `"zwiftudp"`, `null` | `"zwiftudp"` | Teljesítmény adatforrás. `null` = kikapcsolva. |
| `hr_source` | `"antplus"`, `"ble"`, `"zwiftudp"`, `null` | `"zwiftudp"` | Szívfrekvencia adatforrás. `null` = kikapcsolva. |

A power és HR különböző forrásból is jöhet (pl. `power_source: "antplus"`, `hr_source: "ble"`). Ha az érték `null`, az adott szenzorhoz nem csatlakozik semmilyen forrás – hasznos ha a `zone_mode` miatt az egyik adat nem szükséges (pl. `"hr_only"` módban `power_source: null`).

### Forrás-specifikus buffer beállítások

Minden forrásnak saját buffer paraméterei vannak. Ha nincs megadva, a globális fallback értékek érvényesek.

| Prefix | Forrás | Mezők |
|--------|--------|-------|
| `BLE_` | BLE szenzorok | `BLE_buffer_seconds`, `BLE_minimum_samples`, `BLE_buffer_rate_hz`, `BLE_dropout_timeout` |
| `ANT_` | ANT+ szenzorok | `ANT_buffer_seconds`, `ANT_minimum_samples`, `ANT_buffer_rate_hz`, `ANT_dropout_timeout` |
| `zwiftUDP_` | Zwift UDP | `zwiftUDP_buffer_seconds`, `zwiftUDP_minimum_samples`, `zwiftUDP_buffer_rate_hz`, `zwiftUDP_dropout_timeout` |

**Validáció:** `minimum_samples` nem lehet nagyobb mint `buffer_seconds × buffer_rate_hz`.

### ANT+ eszköz beállítások

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `ant_power_device_id` | int | 0–65535 | 0 | ANT+ power meter device ID. 0 = wildcard (első elérhető). |
| `ant_hr_device_id` | int | 0–65535 | 0 | ANT+ HR monitor device ID. 0 = wildcard. |
| `ant_power_reconnect_interval` | int | 1–60 | 5 | Power reconnect várakozás (mp). |
| `ant_power_max_retries` | int | 1–100 | 10 | Power max újrapróba, utána 30s cooldown. |
| `ant_hr_reconnect_interval` | int | 1–60 | 5 | HR reconnect várakozás (mp). |
| `ant_hr_max_retries` | int | 1–100 | 10 | HR max újrapróba. |

**Device ID:** a talált eszközök az `ant_devices.log` fájlba kerülnek. Első futáskor hagyd 0-n (wildcard), majd a logból kiolvasható a specifikus device ID.

**Watchdog:** ha 30 másodpercig nem érkezik ANT+ broadcast, a program automatikusan újraindítja az ANT+ node-ot (USB dongle kihúzás/lemerülés detektálása).

### BLE szenzor bemeneti beállítások

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `ble_power_device_name` | string/null | – | null | BLE power meter neve. `null` → auto-discovery. |
| `ble_power_scan_timeout` | int | 1–60 | 10 | Keresés timeout (mp). |
| `ble_power_reconnect_interval` | int | 1–60 | 5 | Újracsatlakozás várakozás (mp). |
| `ble_power_max_retries` | int | 1–100 | 10 | Max újrapróba. |
| `ble_hr_device_name` | string/null | – | null | BLE HR monitor neve. `null` → auto-discovery. |
| `ble_hr_scan_timeout` | int | 1–60 | 10 | Keresés timeout (mp). |
| `ble_hr_reconnect_interval` | int | 1–60 | 5 | Újracsatlakozás várakozás (mp). |
| `ble_hr_max_retries` | int | 1–100 | 10 | Max újrapróba. |

**Auto-discovery:** a BLE Power a Cycling Power Service (UUID: `0x1818`), a BLE HR a Heart Rate Service (UUID: `0x180D`) alapján keres automatikusan.

### Zwift UDP beállítások

| Mező | Típus | Értékek / Tartomány | Alapértelmezett | Leírás |
|------|-------|---------------------|-----------------|--------|
| `zwift_udp_port` | int | 1024–65535 | 7878 | UDP port, amelyen a háttérprogram adatot küld. |
| `zwift_udp_host` | string | – | `"127.0.0.1"` | UDP host (localhost). |

Ha `power_source` vagy `hr_source` értéke `"zwiftudp"`, a program automatikusan elindítja a `zwift_api_polling.py` scriptet subprocessként. A script Zwift HTTPS API OAuth2 lekérdezéssel szerzi meg az adatokat (Zwift fiókhoz bejelentkezés szükséges), majd JSON formátumban (power, heartrate, cadence, speed_kmh) továbbítja azokat a `zwift_udp_host:zwift_udp_port` címre.

Ha a `zwift_api_polling.py` nem küld adatot, a szokásos dropout logika (ventilátor leállítása) lép életbe.

### Zwift automatikus indítás

| Mező | Típus | Értékek | Alapértelmezett | Leírás |
|------|-------|---------|-----------------|--------|
| `zwift_auto_launch` | bool | true/false | true | Ha true, a program automatikusan elindítja a Zwift-et ha az nem fut. |
| `zwift_launcher_path` | string/null | – | null | ZwiftLauncher.exe egyedi útvonala. `null` → automatikus keresés (Registry + ismert útvonalak). |

**Működés:** ha a `ZwiftApp.exe` nem fut és `zwift_auto_launch` értéke `true`:

1. A program megkeresi a `ZwiftLauncher.exe`-t (Registry → ismert útvonalak → `zwift_launcher_path` felülírás)
2. Elindítja a launchert
3. Ha a `pywinauto` telepítve van: automatikusan megvárja a „Let's Go" gombot (frissítés esetén akár 5 percet is), majd rákattint
4. Ha a `pywinauto` nincs telepítve: a program megvárja hogy a felhasználó manuálisan kattintson a „Let's Go" gombra (max 3 perc)
5. Megvárja a `ZwiftApp.exe` elindulását

**Telepítés:** `pip install pywinauto` (opcionális – nélküle manuális kattintás szükséges).

---

## Cooldown logika

A cooldown csak zóna **csökkentésnél** aktív. Zóna **emelkedésnél** azonnali váltás történik.

**Adaptív módosítások:**

- Nagy zónaesés (≥2 szint) vagy 0W cél → cooldown idő **felezése** (gyorsabb leállás)
- Pending zóna visszaemelkedik → cooldown idő **duplázása** (lassabb emelkedés)
- Felezés és duplázás egyszer-egyszer alkalmazható ciklusonként

---

## Automatikus újracsatlakozás

Mind az ANT+, mind a BLE oldalon automatikus reconnect logika működik:

1. Kapcsolat megszakadás detektálása (BLE: bleak disconnect event, ANT+: watchdog timeout)
2. `reconnect_interval` másodperc várakozás
3. Újrapróbálkozás (max `max_retries` alkalommal)
4. Ha elérte a max-ot: 30 másodperc cooldown, majd újrakezdés

---

## HUD beállítások (`hud`)

A LCARS stílusú HUD ablak viselkedését szabályozó beállítások.

| Mező | Típus | Értékek | Alapértelmezett | Leírás |
|------|-------|---------|-----------------|--------|
| `save_hud_settings` | bool | true/false | false | Ha true, az ablak pozíciója, mérete, átlátszósága és hangerő mentésre kerül a fájlba (ez a flag engedélyezi az automatikus mentést). Ha false, a HUD-on végzett módosítások a memóriában maradnak, fájl nem íródik – a kézi szerkesztések nem lesznek felülírva egy ablak-elhúzással. |
| `sound_enabled` | bool | true/false | true | LCARS hangeffektek be/kikapcsolása. |
| `sound_volume` | float | 0.0–1.0 | 0.5 | Hangeffektek hangereje. Csak akkor mentésre kerül, ha `save_hud_settings=true`. |
| `close_at_zwiftapp.exe` | bool | true/false | true | Ha true, a program automatikusan leáll amikor a ZwiftApp.exe kilép. |
| `opacity` | int | 20–100 | 92 | HUD ablak átlátszósága %-ban. A slider/menüből is módosítható. Csak akkor mentésre kerül, ha `save_hud_settings=true`. |
| `window_geometry` | object | – | `{}` | Per-monitor ablak pozíció/méret. Csak akkor kerül mentésre, ha `save_hud_settings=true`. |

### Ablak pozíció, átlátszóság és hangerő mentése

Az `opacity`, `sound_volume` és `window_geometry` értékek változása **csak akkor** kerül a `settings.json`-ba, ha `save_hud_settings=true`. Ez a mód biztosítja, hogy:

- **Ha `save_hud_settings=true`:** a HUD-on végzett módosítások (ablak elhúzása, átlátszóság állítás, hangerő) automatikusan mentődnek – az elvárt működés.
- **Ha `save_hud_settings=false` (alapértelmezés):** a HUD-on végzett módosítások NEM írják felül a `settings.json`-t – így a kézi szerkesztéseid (pl. egyéb szekciók: `power_zones.ftp`, `ble.device_name` stb.) nem vesznek el egy ablak-elhúzással vagy átlátszóság állítással.

A `window_geometry` mező automatikusan kezelődik: bezáráskor a program menti az ablak pozícióját és méretét az aktuális monitor nevéhez. Induláskor visszaállítja az utoljára használt monitor geometriáját. Ha a monitor nem létezik (pl. külső kijelző lecsatlakoztatva), az elsődleges monitorra kerül az ablak. Több monitor esetén mindegyikhez külön pozíció/méret tárolódik:

```json
"window_geometry": {
  "HDMI-1": {"x": 100, "y": 50, "w": 400, "h": 500},
  "DP-2": {"x": 1920, "y": 0, "w": 340, "h": 460}
}
```

### LCARS hangeffektek

A HUD Star Trek-szerű hangeffekteket használ az események jelzésére:

| Hang | Esemény |
|------|---------|
| `hud_startup` | HUD indítás – tricorder kinyitás (emelkedő sweep) |
| `hud_shutdown` | HUD bezárás – tricorder becsukás (ereszkedő sweep) |
| `zone_up` | Zóna emelkedés |
| `zone_down` | Zóna csökkenés |
| `zone_standby` | Standby-ba lépés (Z0) |
| `sensor_dropout` | Szenzor kiesés (hármas csipogás) |
| `sensor_reconnect` | Szenzor visszacsatlakozás |
| `zwift_connect` | Zwift adatfogadás indulása |
| `zwift_disconnect` | Zwift adatfogadás megszakadása |
| `fan_tx` | Ventilátor parancs elküldve |

A hangerő és ki/bekapcsolás a jobb egérgombos menüből is elérhető, és automatikusan mentődik a `settings.json`-ba.

### ZwiftApp.exe figyelés

Ha `close_at_zwiftapp.exe` értéke `true`, a program ~10 másodpercenként ellenőrzi, hogy a `ZwiftApp.exe` fut-e. Ha a Zwift futott és kilép, a program automatikusan leáll (tricorder becsukás hanggal).

---

## Log fájlok

| Fájl | Tartalom | Max méret |
|------|----------|-----------|
| `smart_fan_controller.log` | Teljes alkalmazás log (zónaváltások, csatlakozás, hibák) | 500 KB (rotált, 2 backup) |
| `ble_devices.log` | Talált BLE eszközök (deduplikált, csak új eszközök kerülnek bele) | – |
| `ant_devices.log` | Talált ANT+ eszközök (deduplikált, device_type + device_id alapján) | – |

A log fájlok helye a `global_settings.log_directory` beállítástól függ. Ha nincs megadva vagy nem elérhető, a program könyvtárába kerülnek.

A `ble_devices.log` és `ant_devices.log` fájlok hasznosak a device_name / device_id beállításához: első futás wildcard módban, majd a logból kiolvasható a specifikus eszköz azonosító.
