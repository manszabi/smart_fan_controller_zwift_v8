# Verziótörténet — FanController_OTA_debug

A firmware részletes változás-naplója. (A `FanController_OTA_debug.ino` fejlécéből
kiemelve; az aktuális verziót a `FIRMWARE_VERSION` define tartalmazza.)

---

## v7.0.0 → v7.1.0 — OTA stabilizálás

- **[MOD-1]** `performUpdate()` – `delay(5000)` kiváltva: `otaPendingReboot` flag + `millis()` alapú várakozás.
- **[MOD-2]** `OTA_INSTALL_MODE` – `delay(2000)` kiváltva (×2): `otaInstallWaiting` flag + `millis()` alapú várakozás.
- **[MOD-3]** `performUpdate()` – WDT törlése flash írás előtt: `esp_task_wdt_delete(NULL)` a watchdog timeout ellen.
- **[MOD-4]** `FIRMWARE_VERSION` + `FIRMWARE_DATE` frissítve.
- **[MOD-6]** 2026-05-24 – `handleMultiClick` (3+ kattintás): visszavált automata módba (`manualMode = false`, `bleEnabled = true`), kikapcsolja a kézi zónát, és újraindítja a BLE advertising-et.

### OTA hibajavítások (2026-05-24)

- **[FIX-ESP-1]** OTA utolsó part nem íródott ki: `OTA_UPDATE_MODE`-ban az `otaWriteFile` blokk az `otaCur+1==otaParts` ellenőrzés UTÁN volt → javítva.
- **[FIX-ESP-1c]** Buffer logika kijavítva: INSTALL_MODE-ban az 1b-s „megfordított" buffer logika rossz volt → most ugyanaz, mint UPDATE_MODE, duplikáció ellen `otaWriteFile=false` védelem.
- **[FIX-ESP-2]** Debug logging az `otaWriteBinary`-be.
- **[FIX-ESP-3]** `otaWriteFile=false` hibás esetben is, hogy ne ragadjunk végtelen ciklusban.
- **[FIX-ESP-4]** Ténylegesen kiírt byte-okat számoljuk, nem a kértet (részleges write detektálás).
- **[FIX-ESP-5]** VALÓDI hiba: módváltás `otaWriteFile` true állapotban. A SPIFFS write 100+ ms ideig fut, eközben a 42. part 0xFC megérkezik és újra true-ra állítja az `otaWriteFile`-t. A write végén false lesz, a következő loop sor azonnal módot vált — a 42. part írása örökre kimarad. Javítás: módváltás csak akkor, ha `otaWriteFile=false`.

### Production javítások (2026-05-24)

- **[FIX-ESP-6]** WDT visszahelyezése a `performUpdate()` végén az `esp_task_wdt_delete()` után — különben "task not found" spam végtelen ideig.
- **[FIX-ESP-7]** Boot után `update.bin` maradványok takarítása, "update.bin is dir" hibák elkerülésére.
- **[FIX-ESP-8]** WDT deinit boot elején, "TWDT already initialized" üzenet elkerülésére soft reset után.
- **[FIX-ESP-9]** `OTA_DEBUG=0` production módra — eltávolítja a per-csomag log spam-et (OTA packet, FS write…).

### SPIFFS védelmek (2026-05-24)

- **[FIX-ESP-10]** Részleges write detektálás az `otaWriteBinary`-ben. Ha a `file.write()` kevesebbet ír, mint amennyi kellene (SPIFFS megtelt), az OTA-t azonnal megszakítjuk és töröljük a részleges `update.bin`-t. Régen végtelen "OTA incomplete" loop volt 96000-en ragadva.
- **[FIX-ESP-11]** Előzetes méret-ellenőrzés a 0xFE parancsnál: ha a firmware nem fér el a SPIFFS-en, hibát küldünk vissza a kliensnek és nem kezdjük el az OTA-t (4 KB tartalékot tartunk a SPIFFS overhead-nek).
- **[FIX-ESP-12]** 2026-05-24 – ismétlődő "OTA file complete": a `performUpdate()` után az 5 mp-es nemblokkoló reboot várakozás közben az INSTALL_MODE újra meg újra lefutott, ismételten triggerelve a complete és `updateFromFS` hívásokat. Javítás: `otaTotalBytes = 0` mielőtt meghívjuk az `updateFromFS()`-t, így a feltételek hamisak lesznek a következő körökben.

---

## v7.2.x – v7.9.x — verziónkénti változások

- **[FIX-ESP-PROD]** 2026-05-24: **7.2.0** production verzió, az összes OTA javítással.
- **[MOD-7]** 2026-05-24: **7.3.0** — FIX-ESP-10, FIX-ESP-11, FIX-ESP-12 és MOD-6 (`handleMultiClick`) hozzáadva, custom partition table (1.3MB APP + 1.3MB SPIFFS).
- **[FIX-ESP-13]** 2026-05-30: **7.5.0** — boot reset-detektálás `esp_reset_reason()` alapra állítva (brownout/panic/WDT után újraindul, nem alszik el), reset-ok + heap logolás hozzáadva a "30-40 perc után leáll" tünethez.
- **[FIX-ESP-14]** 2026-05-30: **7.6.0** — SPIFFS diag napló (`/diag.log`): boot reset ok + alacsony memória bejegyzés, BLE-n lekérdezhető a `DIAG?` paranccsal (`DIAGCLR` töröl). Darabolt, nemblokkoló notify streamelés a fan karakterisztikán.
- **[FIX-ESP-14b]** 2026-05-30: **7.6.1** — naplózás csak ténylegesen szükséges esetben (hibás reset + lowmem, a POWERON/DEEPSLEEP/SW kihagyva → OTA-t nem zavarja), heap-mentes (String helyett snprintf/stack), kis fájl (512B). Külön Python kliens (`diag_client.py`) a lekérdezéshez AUTH-tal.
- **[FIX-ESP-14c]** 2026-05-30: **7.6.2** — átnézés utáni javítások: a diag csomagméret 20B (alapértelmezett BLE MTU mellett is sértetlen napló), és lowmem-írás halasztása streamelés közben (ne csonkoljuk a nyitott naplófájlt).
- **[FIX-ESP-15]** 2026-05-30: **7.6.3** — `enterDeepSleep()` forrásának naplózása (`[sleep] src=button-longpress/idle-timeout/failsafe-timeout`), hogy a szándékos alvás megkülönböztethető legyen a brownout/panik leállástól.
- **[FIX-ESP-16]** 2026-05-31: **7.6.4** — OTA magic-byte ellenőrzés az `Update.begin()` előtt. Ha a feltöltött bináris első byte-ja nem 0xE9, érthető "rossz firmware" hibát adunk a félrevezető "Decryption error" helyett (amit az arduino-esp32 Update könyvtár U_AES_DECRYPT_AUTO módja dob nem-0xE9 fejlécre). A hiba a diag naplóba is bekerül (`[ota] bad magic=0x..`).
- **[FIX-ESP-18]** 2026-06-01: **7.6.5** — `RELAY_SWITCH_DELAY_MS` 100 → 10 ms. A fokozat-váltás break-before-make ideje csökkentve, hogy a teljes táp-tranziens (régi fan ki + új fan be) rövid idő alatt lezajljon, és ne legyen két külön mély feszültségrogyás. MEGJEGYZÉS: a tényleges break minimum ~20 ms a `checkInterval` (20 ms) miatt, mert a `handleZoneChange()` csak annyi időnként fut. A 230V AC ventilátor BROWNOUT csak hardveres snubber/MOV-val szűnik meg.
- **[FIX-ESP-19]** 2026-06-01: **7.6.6** — BROWNOUT/UNKNOWN reset után görgő + relék automatikus bekapcsolása boot-ban (ne maradjon "halott" az eszköz).
- **[FIX-ESP-20]** 2026-06-01: **7.6.6** — az összeomlás előtti ventilátor-fokozat is visszaáll (RTC_NOINIT-be mentve, magic-cel védve a BROWNOUT-törlés ellen).
- **[FIX-ESP-21]** 2026-06-01: **7.6.7** — hibrid fokozat-mentés: RTC (resetre) + NVS (teljes áramtalanításra). NVS-be csak akkor írunk, ha egy fokozat 30 mp-ig stabil maradt (nem a brownout-veszélyes váltási pillanatban, flash-kímélő). Boot-helyreállítás prioritás: RTC → NVS fallback. NVS partíció már létezik, nem kell partíció-átalakítás.
- **[FIX-ESP-22]** 2026-06-01: **7.6.8** — a WDT reseteket (INT_WDT/TASK_WDT/WDT) is bevesszük a görgő + fokozat visszaállításba (eddig csak BROWNOUT/UNKNOWN).
- **[FIX-ESP-23]** 2026-06-02: **7.6.9** — hosszabb türelmi szünetek az `enterDeepSleep()`-ben, hogy az ESP rendszere stabilizálódjon az alvás előtt (INT_WDT ellen): BLE disconnect után 200→500 ms, BLE stop után 100→300 ms, relé OFF után +200 ms, LED OFF után +200 ms, és +500 ms közvetlenül a deep sleep előtt.
- **[FIX-ESP-25]** 2026-06-02: **7.7.0** — intelligens zóna-helyreállítás hibás reset után: RTC jó → RTC; RTC hibás + NVS jó → NVS; mindkettő jó, de különbözik → magasabb zóna; mindkettő hibás → fallback LEVEL:2. Így az UNKNOWN/BROWNOUT/WDT resetkor sosem marad halott állapotban.
- **[FIX-ESP-26]** 2026-06-02: **7.7.1** — a boot NVS olvasás default értéke −1 (nem 0!), hogy a "nincs NVS mentés" eset megkülönböztethető legyen a "mentett 0" esettől. Enélkül az NVS mindig "érvényes 0"-nak látszott, és a fallback LEVEL:2 sosem futott.
- **[FIX-ESP-27]** 2026-06-02: **7.7.2** — NVS force-mentés: sűrű váltogatásnál (ahol sosincs 30 s nyugalom) is mentsünk legalább 5 percenként, ha az aktuális fokozat eltér az NVS-ben tárolttól. Így a stressz/edzés alatti utolsó fokozat is megőrződik teljes áramtalanításra, de flash-kímélő módon (max 5 percenként egy írás).
- **[FIX-ESP-28]** 2026-06-02: **7.7.3** — boot-diagnosztika a soros monitorra (RTC magic + savedZone, NVS zone, diag.log tartalma), egy jól olvasható blokkban. `BOOT_DIAG` kapcsolóval kikapcsolható (0), ha a program stabil — ekkor nem fordul bele kód.
- **[FIX-ESP-29]** 2026-06-06: **7.8.0** — fan relé KIMENET figyelése 3 db H11AA1M AC-bemenetű optocsatolóval (GPIO6/7/20). FONTOS: a H11AA1M kimenete 230V AC jelenlétében NEM folyamatosan alacsony — a nullátmeneteknél ~100 Hz-cel HIGH-ra ugrik. Ezért nem egyetlen `digitalRead`, hanem 40 ms-es idő-ABLAK: ha volt LOW minta, akkor VAN AC. Debounce + zónaváltás utáni türelmi idő szűri a tranzienst. Az elvárt (`relaysEnabled && currentZone`) és a mért állapot eltérésénél reagál.
- **[FIX-ESP-29b]** 2026-06-06: **7.8.1** — ASZIMMETRIKUS reakció: STUCK (zóna OFF, de van AC → beragadt relé) AZONNAL failsafe + diag.log; NOAC (zóna ON, de nincs AC) csak debounce-olt EGYSZERI figyelmeztetés + diag.log, FAILSAFE NÉLKÜL. A STUCK-nál a diag.log SZINKRON (flush) íródik a STATE_FAILSAFE beállítása ELŐTT.
- **[FIX-ESP-30]** 2026-06-06: **7.8.2** — boot-helyreállítás három javítása:
  1. Zóna-visszaállításnál az RTC ELSŐBBSÉGE (a "magasabb zóna" heurisztika helyett): az RTC mindig a legfrissebb (minden váltásnál íródik), az NVS késik (30 s/5 perc), így lefelé váltás után a max() elavult magas zónát hozott vissza. Most: RTC jó → RTC; egyébként NVS; egyébként fallback 2.
  2. A GÖRGŐ állapota is perzisztálódik (RTC magic + NVS), és boot után CSAK akkor kapcsol vissza, ha tényleg aktív volt. Eddig minden hibás reset feltétel nélkül bekapcsolta → idle állapotban váratlan görgő/fan indítás. Ismeretlen állapotnál (nincs RTC magic és nincs NVS rekord) nem indítunk.
  3. `saveZoneToNvsIfStable()` a `currentZone`-t zoneMux kritikus szekcióban olvassa.
- **[FIX-ESP-30b]** 2026-06-06: **7.8.3** — failsafe BELÉPÉSKOR a görgő ÉS a ventilátor-fokozat állapota minden tárolóban (logikai + RTC + NVS) lenullázódik, hogy egy failsafe közbeni hibás reset (akár BROWNOUT, ami az RTC-t is törli) SE indítsa újra a görgőt/ventilátort. A boot-helyreállítás failsafe után 'idle'-t lát.
- **[FIX-ESP-31]** 2026-06-06: **7.8.4** — a relé-kimenet ellenőrzések (a 2+ relé LOW GPIO-visszaolvasás, és FAN_SENSE esetén a 230V AC eltérés) a `normalMode()`-ban a fokozatváltás (`handleZoneChange`) UTÁNRA kerültek, hogy a frissen beállított relé-állapotot értékeljék. A failsafe-logika és a küszöbök változatlanok.
- **[FIX-ESP-32]** 2026-06-06: **7.8.5** — `FAN_SENSE_ENABLE` bekapcsolva (0→1): a 3 db H11AA1M opto (GPIO6/7/20) figyeli a relé-kimeneteken a 230V AC-t. STUCK (zóna OFF, de van AC → beragadt relé) → szinkron diag.log + azonnali failsafe; NOAC (zóna ON, de nincs AC) → egyszeri figyelmeztetés + diag.log, failsafe nélkül.
- **[FIX-ESP-33]** 2026-06-06: **7.8.6** — a failsafe-állapot nullázása (RTC+NVS+logikai) közös `zeroStateForFailsafe()` helperbe került, és már a failsafe DETEKTÁLÁSAKOR lefut (STUCK + 2-relé-LOW ág, a STATE_FAILSAFE beállítása ELŐTT). Így megszűnik a detektálás és a `failSafeMode()` első lefutása közti időablak: failsafe melletti hibás reset SEM állíthatja vissza a reléket. A helper idempotens (cache-alapú NVS).
- **[FIX-ESP-34]** 2026-06-10: **7.9.0** — OTA per-part CRC32 + újraküldés. A 0xFC part-vége csomag 4 byte CRC32-t (zlib-kompatibilis) hordoz; a fogadó a SPIFFS-írás ELŐTT ellenőrzi, eltérésnél ugyanazt a partot újrakéri (0xF1), max MAX_PART_RETRY-szer, utána abort (0x0F + diag.log). A part-feldolgozás soros lett (a következő partot csak CRC-OK + írás után kérjük), ami kiváltja a korábbi kettős-buffer versenyhibákat is. NINCS visszafelé kompatibilitás: a régi (CRC nélküli, 5 byte-os 0xFC) kliens nem támogatott.
- **[FIX-ESP-35]** 2026-06-10: **7.9.1** — OTA-indítás és -robusztusság:
  1. **Determinisztikus part-0 indítás**: a `0xFF` (UPDATE_MODE) feldolgozásakor a fogadó azonnal `0xF1 0`-t kér, a korábbi `0xAA`-handshake helyett. Ez megszünteti a „stuck part 0" elvi versenyt (a `0xAA` a NORMAL_MODE-ban futott, és versenyezhetett a `0xFF` beérkezésével). A `0xAA` „transfer mode" jel és az `otaSendMode` flag megszűnt; az átvitel tisztán pull-alapú.
  2. **Csonka `0xFC` (<9 byte) kezelése**: a fogadó nem dobja el csendben, hanem újrakéri az aktuálisan várt partot (`otaExpectedPart`), retry-limittel; tartós hiba esetén abort.
  3. Közös **`otaAbort()`** helper (0x0F hibaüzenet + diag.log + `update.bin` törlés + állapot-reset) — a CRC-retry-túllépés és a csonka-`0xFC` is ezt hívja (DRY).
- **[FIX-ESP-40]** 2026-06-16: **7.12.0** — boot-folyamat finomítás: (a) a görgő/fokozat **visszaállítás a BLE-init ELÉ** került → a relék/terhelés hamarabb állnak vissza hibás reset után; (b) a **fan-relé azonnali bekapcsolása** bootkor: a `setFanZone()` után kivárjuk a `RELAY_SWITCH_DELAY_MS` break-before-make időt, majd hívjuk a `handleZoneChange()`-et — így a fokozat-relé nem a `loop()` első iterációjára vár. (A `setup()`-beli redundáns „Relays safe OFF" blokk megszűnt; a relé-OFF a `setup()` legelején történik.)
- **[FIX-ESP-39c]** 2026-06-14: **7.11.3** — a redundáns relé-`pinMode`-ok törlése a `setup()` GPIO-init blokkjából (ezek már a `setup()` legelején megtörténnek, [FIX-ESP-39b]). A LED-`pinMode`-ok megmaradtak. Működés azonos.
- **[FIX-ESP-39b]** 2026-06-14: **7.11.2** — a boot-eleji relé-tiltás (`RELAY_EN` LOW + relék OFF) a `setup()` **legelső** utasításává került, a `Serial.begin`/`delay` ELÉ (a GPIO-hoz nem kell a Serial), így a boot-állapot ablaka ~100+ ms-mal rövidebb.
- **[FIX-ESP-39]** 2026-06-14: **7.11.1** — **brownout/reset-hurok megszakító + relék azonnali tiltása bootkor**. (1) RTC-számláló az egymást követő *hibás reset + visszaállítás* eseményekre: ha rövid időn belül eléri a limitet (`MAX_ERR_RESTORE=3`), a boot NEM állítja vissza a görgőt/ventit, hanem **idle** marad → a hurok megszakad, az eszköz vezérelhető (gomb/BLE). A számláló `ERR_RESTORE_CLEAR_MS=30 s` stabil futás után nullázódik, így a normál „egyszeri hibás reset utáni visszaállítás" változatlan. (2) A `setup()` legelején a `RELAY_EN` LOW + minden relé OFF, hogy a boot-pillanatban (C6: GPIO17/RELAY_EN belső felhúzása) a relék ne kapjanak tápot → kisebb áramlökés/brownout-esély. Főleg a XIAO ESP32-C6 „visszaállítás után furán működik / nincs serial / nem vezérelhető" tünetére.
- **[FIX-ESP-38]** 2026-06-14: **7.11.0** — **RAM-optimalizálás: egy, dinamikus OTA-buffer**. A korábbi kettős, statikus 2×16 KB buffer (`otaBuf1`/`otaBuf2`) helyett **egyetlen** buffer, amit **csak OTA alatt** allokálunk (`malloc` a `0xFF`-nél, `free` a telepítéskor/abortkor/disconnectkor). A `[FIX-ESP-34/35]` soros part-feldolgozása miatt a kettős buffer felesleges volt. Eredmény: a statikus RAM **−32 KB** (globálisok 72 KB → 39 KB; szabad RAM ~255 KB → ~288 KB), OTA alatt 16 KB ideiglenesen. A működés (BLE-protokoll, SPIFFS-eredmény, CRC, retry) **bitre azonos**. Ha a 16 KB malloc elbukik (kevés/töredezett heap), az OTA érthető hibával nem indul el. `0xFB` írás határ-ellenőrzéssel (heap-védelem).
- **[FIX-ESP-37]** 2026-06-14: **7.10.1** — XIAO ESP32-C6: **külső antenna** kiválasztása bootkor (csak C6-on, a rádió indítása előtt): `WIFI_ENABLE` (GPIO3) LOW az RF-kapcsoló engedélyezéséhez, majd `WIFI_ANT_CONFIG` (GPIO14) HIGH = külső antenna. C3-on a blokk `#if`-fel kizárva → változatlan.
- **[FIX-ESP-36]** 2026-06-14: **7.10.0** — **Seeed XIAO ESP32-C6 támogatás**. A pinkiosztás (relé-, gomb-, LED- és H11AA1M kimenet-figyelő pinek) cél-chip szerint feltételes: `CONFIG_IDF_TARGET_ESP32C6` → C6-os kiosztás, egyébként a meglévő C3. A `build.sh` `TARGET=c6`/`c3` választással fordít (`XIAO_ESP32C6` / `XIAO_ESP32C3` FQBN). A C6-os GPIO-k: FAN1=23, FAN2=22, FAN3=21, ROLLER=2, EN=17, BUTTON=1, LED_Y=0, LED_R=16; SENSE: 19/20/18. Fordítás-méret C6: ~89% (1.375 MB partíció).

---

## v7.13.0 — AC-érzékelés a bontó-érintkezőn + soros kimenet egységesítése (2026-06-21)

- **[FIX-ESP-41]** 2026-06-21: **7.13.0** — **AC-érzékelés a relé BONTÓ (NC) érintkezőjére került** (eddig a relé KIMENETÉT, az NO-ágat figyelte). Oka: a ventilátorban a fokozat-tekercsek **sorosak**, ezért **egy** aktív fokozatnál a 230V AC **minden** kimeneti ágon megjelenik → a kimenet-figyelés nem tudta megkülönböztetni, melyik relé húzott be (téves STUCK a nem aktív fokozatokon). A **bontó-érintkező** viszont **relénként egyedileg** tükrözi az adott relé saját kapcsolási állapotát, függetlenül attól, merre folyik az AC a kimeneteken. Emiatt a polaritás megfordult: **`FAN_SENSE_ACTIVE_LOW` alapból `0`** (1→0), így a szűrt sense-állapot jelentése változatlan marad („az `i`. relé behúzva / fokozat aktív"), és az `elvárt vs. mért` STUCK/NOAC failsafe-logika **érintetlen, továbbra is helyes**. A H11AA1M AC-tudatos idő-ablakos mintavétel (40 ms ablak + 80 ms debounce + türelmi idő) és a STUCK→azonnali failsafe / NOAC→egyszeri figyelmeztetés viselkedés változatlan. *(A `FAN_SENSE_ACTIVE_LOW`-t később a `FAN_SENSE_AC_MEANS_ENGAGED` váltotta fel — lásd `[FIX-ESP-43]`.)*
- **[FIX-ESP-43]** 2026-06-21: **7.13.0** — **RC-szűrőtől független, LOW-alapú AC-detektálás** (a kondi kiesése se okozzon téves failsafe-et). A H11AA1M a bemenetén lévő AC-ra LOW-t húz (vezet az opto) — ez hardveres invariáns, a kimeneti RC-szűrőtől független. A korábbi `FAN_SENSE_ACTIVE_LOW=0` a **HIGH** szintet figyelte, ami szűretlen jelen a nullátmeneti HIGH-tüskéktől téves „aktívat" (és így folyamatos téves STUCK-ot) adott volna. Az új `monitorFanRelays()` **mindig a LOW mintát** keresi az időablakban (volt-e opto-vezetés), a HIGH-tüskéket ignorálja, így AC jelenléte szűréssel (stabil LOW) és szűrés nélkül (túlnyomóan LOW) is megbízhatóan kiderül → az opto-kimeneti 100 nF ∥ 1 µF kiesése/kiszáradása **sem** ad téves STUCK-ot. A polaritás-makró általánosítva/átnevezve: `FAN_SENSE_ACTIVE_LOW` → **`FAN_SENSE_AC_MEANS_ENGAGED`** (0 = NC-bekötés: AC ⇒ relé NINCS behúzva; 1 = NO-kimenet: AC ⇒ behúzva); a `fanSenseLastActive` → `fanSenseLastLow`. STUCK/NOAC logika és időzítések változatlanok.
- **[FIX-ESP-42]** 2026-06-21: **7.13.0** — **soros (Serial) kimenet egységesítése + `Serial.begin` feltételhez kötése**. (1) Új `SERIAL_ENABLED` makró (`= DEBUG || OTA_DEBUG || BOOT_DIAG`): a `Serial.begin(115200)` és minden `Serial.flush()` **csak** ekkor fordul bele → ha mindhárom debug-kapcsoló `0`, **nincs Serial-inicializálás** és egyetlen `Serial.*` hívás sem marad a binárisban (debug-mentes build ~11 KB-tal kisebb). (2) Új, variadikus **érték-kiíró makrók**: `DBG_V`/`DBG_VLN` és `OTA_DBG_V`/`OTA_DBG_VLN` (a `HEX`-es kétargumentumú alakot is kezelik). (3) Minden korábban **debugon kívül maradt** `Serial.print(érték)`/`println(érték)` átírva a megfelelő kapuzott makróra — ezek eddig `DEBUG=0` mellett is kiírtak (pl. zóna/roller érték, OTA part-szám, partíció-címek, CRC-retry adatok, heap, reset reason banner, CRC-önteszt). A `0xFC` part-szám immár az `OTA_DEBUG` csatornán megy. A `printBootDiag()` változatlan, saját `BOOT_DIAG` kapuja alatt (dedikált boot-diagnosztika). A három debug-csatorna (`DEBUG`, `OTA_DEBUG`, `BOOT_DIAG`) így teljesen független és tisztán kapuzott.
- **[MOD-8]** 2026-06-21: **7.13.0** — **forráskód-megjegyzések egységesítése**: minden többsoros `//` kommentblokk tömör **egysorossá** húzva, és a kommentálatlan, kevésbé magától értetődő globális változók (OTA állapot-változók, fő állapotgép, fokozat/relé/görgő/manuál állapotok, zónaváltás-változók) tömör egysoros magyarázatot kaptak. A fordított bináris a komment-tisztítástól bitre azonos.
- **[MOD-9]** 2026-06-21: **7.13.0** — **fan-sense feliratok + változónevek pontosítása** az NC-érintkezős bekötéshez (a kimenet-figyelés korszakából maradt, immár félrevezető nevek/kommentek). A `fanLineLive` → **`fanRelayEngaged`** átnevezés (a `TRUE` jelentése „az adott relé behúzva", nem „van AC a kimeneten"), a hozzá tartozó lokális párokkal együtt (`rawLive`→`rawEngaged`, `live`→`engaged`, `expectedLive`→`expectedEngaged`); a STUCK/NOAC kommentek és a NOAC figyelmeztető szöveg átírva a relé behúzva/nincs behúzva szemantikára. Csak elnevezés/komment — a fordított bináris bitre azonos.
- **[HW-1]** 2026-06-21: **panel passzív alkatrészek** (firmware-kód nem változik, dokumentációs feljegyzés a stabil boot-/kapcsolási viselkedéshez és zajszűréshez):
  - relé-vezérlő GPIO-k (`RELAY_FAN1/2/3`, `RELAY_ROLLER`): **10 kΩ felhúzó** → boot/tranziens alatt definiált HIGH (relék OFF, aktív-LOW);
  - relé tápengedély (`RELAY_EN`): **10 kΩ lehúzó** → boot alatt biztos LOW (relék tiltva; C6 GPIO17 belső felhúzása ellen, lásd `[FIX-ESP-39]`);
  - nyomógomb: **22 kΩ felhúzó** + **100 nF** a kapcsolóval párhuzamosan (debounce);
  - H11AA1M bemenet (230V AC): **2 × 120 kΩ** soros áramkorlátozás;
  - H11AA1M kimenet (`FANx_SENSE_PIN`): **22 kΩ felhúzó** + **100 nF ∥ 1 µF** RC-szűrés a 100 Hz-es AC-ripple-re (a belső `INPUT_PULLUP` mellett);
  - LED-ek: **330 Ω** soros áramkorlátozás;
  - 5V USB táp: **1000 µF ∥ 100 nF** táppuffer/szűrés a relé-kapcsolási áramlökések ellen (brownout-csökkentés).

---

## v7.14.0 — Fő relé (görgő + ventilátor táp), bootkori relé-önteszt, OTA/diag.log megerősítés (2026-06-23)

- **[MOD-10]** 2026-06-23: **7.14.0** — **`RELAY_ROLLER` → `RELAY_MAIN` átnevezés.** A fő relé a gyakorlatban a **görgőt ÉS a ventilátor tápját** kapcsolja egyszerre (görgő nélkül nincs edzés, görgő/táp nélkül a ventilátor haszontalan), ezért a név és a hozzá tartozó belső azonosítók `main`-re változtak: `rollerActive`→`mainActive`, `savedRoller`/`savedRollerMagic`→`savedMain`/`savedMainMagic`, `nvsLastSavedRoller`→`nvsLastSavedMain`, `restore_roller`→`restore_main`, `activateRoller`/`deactivateRoller`→`activateMain`/`deactivateMain`, a BLE-parancs belső változói (`hasRollerCommand`/`rollerCommand`/`rollerCmd`)→`main*`, és az **NVS-kulcs** `fan/roller`→`fan/main`. A BLE **wire-protokoll `ROLLER:` parancsa változatlan** (app-kompatibilitás), csak a belső kód és a fő relé állapotára utaló DBG/komment lett `main`. *(Erase-all-flash USB-flasheléshez igazítva — a régi NVS-érték törlődik.)*
- **[FIX-ESP-44]** 2026-06-23: **7.14.0** — **CRC32 önteszt → OTA letiltás.** A bootkori `crc32_zlib` önteszt (`crc32("123456789")==0xCBF43926`) mostantól **release buildben is** fut, és bukás esetén nemcsak kiír, hanem **letiltja az OTA-t** (az OTA BLE-szolgáltatás el sem indul), mert a firmware-ellenőrzés megbízhatatlan lenne. A hiba a diag.log-ba kerül (`[boot] CRC32 self-test FAIL -> OTA off. Just serial update!`), az eszköz egyébként normálisan fut tovább (ventilátor, diag-lekérdezés a fő BLE-szolgáltatáson). Új `OTA_ROLLBACK_ON_CRC_FAIL` kapcsoló (alapból `0`): `1` esetén frissen OTA-zott (`PENDING_VERIFY`) firmware CRC-bukásánál visszagörget az előző jó verzióra (`esp_ota_mark_app_invalid_rollback_and_reboot`).
- **[FIX-ESP-45]** 2026-06-23: **7.14.0** — **Bootkori relé-önteszt + beragadt fő relé detektálás.** Új `RELAY_TEST_AT_BOOT` kapcsoló (alapból `1`): a `loop()` elején, **BLE-kapcsolat előtt**, egyszer fut le — csak **software resetnél** (pl. OTA után) és **gombébresztésnél** (hiba miatti újraindulásnál NEM, hogy ne zavarja a fokozat-visszaállítást). `RELAY_EN` be, a **fő relé végig OFF**, a fan-reléket `FAN1→FAN2→FAN3` sorban kapcsolja (egyszerre csak egy; `RELAY_TEST_ON_MS`=120 ms, `RELAY_TEST_GAP_MS`=60 ms). Mivel a fő relé OFF, a bontó-érintkezőkön nem lehet AC; ha mégis van (a `FAN_SENSE_AC_MEANS_ENGAGED` szerinti vonalon), az a **fő relé beragadását** jelzi → `DBG("main_relay stuck")` + `diag.log: [relay] main stuck!` + azonnali `STATE_FAILSAFE`.
- **[FIX-ESP-46]** 2026-06-23: **7.14.0** — **Fan-sense konzisztencia a fő relével.** Mivel a `RELAY_MAIN` adja a ventilátor tápját, **kikapcsolt fő relénél nincs AC** a fan-ágakon → a sense értelmezhetetlen (NC-nél minden „behúzva" → téves STUCK; NO-nál téves NOAC). (1) `checkFanRelayMismatch()` `!mainActive` esetén **kilép** (nincs téves failsafe) és nullázza a NOAC-követést. (2) `activateMain()` türelmi időt (grace) állít + nullázza a mismatch-állapotot (az AC stabilizálódásához). (3) `deactivateMain()` a **fan-reléket OFF**-ra és a **zónát nullára** állítja (folyamatban lévő váltással együtt) — nincs „fokozat táp nélkül" inkonzisztens állapot. (4) A fokozatváltás (`setFanZone`) fő relé nélkül is végrehajtódik (a relé kapcsol), a figyelést az (1) kezeli.
- **[MOD-11]** 2026-06-23: **7.14.0** — **diag.log: csak hibák + sticky verziósor.** A napló mostantól csak hibákat/diagnosztikai eseményeket tárol; a rutin „sikeres/info" bejegyzések (deep sleep belépés `[sleep]`, OTA health-check OK) kikerültek. A napló **első sora** mindig a stabil firmware-verzió (`[ver] <verzió>`): minden validált bootnál és OTA utáni health-check OK-kor íródik, **dedup**-olva, és **sticky** (a méret-trimmelés és a `DIAGCLR` is megőrzi). A relé-failsafe bejegyzések egységes `[relay]` cimkét kaptak (`[relay] 1 2 ACTIVE ST zone=…`, `[relay] N STUCK zone=…`, `[relay] main stuck!`).
- **[MOD-12]** 2026-06-23: **7.14.0** — **Aktivitás-definíció pontosítása** (tétlenség-időzítő). `hasActivity = mainActive && ((bleConnected && currentZone≠0) || (manualMode && manualZoneIndex≠0))`: aktivitás csak ha a **fő relé be ÉS megy a ventilátor** — automatikus (BLE) módban **élő BLE-kapcsolattal**, kézi módban anélkül is. A fő relé/görgő futása önmagában (ventilátor nélkül) nem számít aktivitásnak.
- **[FIX-ESP-47]** 2026-06-23: **7.14.0** — **STUCK-detektálás gyorsítása**: `FAN_SENSE_GRACE_MS` 1500→**300 ms**. A sense-állapot ~150 ms alatt beáll (40 ms AC-ablak + 80 ms debounce + relé-mechanika), így a 300 ms (~2× tartalék) elég — a kapcsolás utáni beragadt relé detektálása ~1,5 s helyett ~0,3 s. A debounce/ablak/NOAC-confirm változatlan.
- **[FIX-ESP-48]** 2026-06-23: **7.14.0** — **NOAC figyelmeztetés gyorsítása**: `FAN_SENSE_MISMATCH_CONFIRM_MS` 1000→**300 ms**. A megerősítés a kapcsolás utáni grace **után** számol (`!inGrace`), így a két relé közti break-before-make átmenetet a grace fedi — a confirm már csak a grace utáni, tényleges „nincs AC" debounce-a (tranziens hálózati zaj ellen). Egy valódi NOAC-hiba így ~grace+confirm = ~0,6 s alatt naplózódik (eddig ~1,3 s). Failsafe-mentes marad.
- **[MOD-13]** 2026-06-23: **7.14.0** — **DEBUG=0 build tisztítása + komment-egységesítés.** A csak DBG-kiírásban használt, `DEBUG=0` mellett `-Wunused` figyelmeztetést adó változók rendezve (`TAG` törölve; `running`/`next`/`stName`/`fromZone` `#if DEBUG` blokkba) → `--warnings all` mellett mindkét cél (C3/C6) tiszta. A session során bevezetett kódrészek többsoros kommentjei tömör egysorosra húzva. `FIRMWARE_VERSION`→7.14.0 / `FIRMWARE_DATE`→2026-06-23.

---

## v7.14.7 — Átvilágítás: wrap-safe időzítés + apró tisztítás (2026-07-23)

*(Megjegyzés: a 7.14.1–7.14.6 közti lépések változásai külön nem lettek naplózva —
egyetlen összevont "Update FanController_OTA_debug.ino" commitban érkeztek.)*

- **[FIX-ESP-49]** 2026-07-23: **7.14.7** — **`handleZoneChange` millis()-túlcsordulás.**
  A `now >= start` külső feltétel pont a moduláris (wrap-safe) kivonás védelmét
  iktatta ki: `millis()` túlcsordulásakor (~49,7 naponta) a 10 ms-os
  break-before-make védőidő egyszer kimaradhatott volna. Javítás: egyetlen
  szabványos `(unsigned long)(now - start) < RELAY_SWITCH_DELAY_MS` ellenőrzés —
  normál működésben bitre azonos, túlcsorduláskor is helyes.
- **[MOD-14]** 2026-07-23: **7.14.7** — **Halott `currentMillis` globális törölve.**
  A globálist csak írta a kód (`normalMode`), olvasni soha nem olvasta (a
  `handleLEDs` paramétere árnyékolta) — eltávolítva.
- **[MOD-15]** 2026-07-23: **7.14.7** — **`rebootEspWithReason` kiírja az okot.**
  A `reason` paraméter eddig nem jelent meg a debug-kimenetben (release buildben
  továbbra is no-op).
- **[MOD-16]** 2026-07-23: **7.14.7** — **`ota_diagnostic.py` hordozhatóság.**
  A partíciós tábla beégetett `/home/user/...` útvonala a szkript saját
  könyvtárára cserélve — bármely gépen működik.

---

## v7.14.8 — Átvilágítás: boot-helyreállítási regresszió + Python segédeszközök (2026-08-24)

- **[FIX-ESP-50]** 2026-08-24: **7.14.8** — **Kritikus regresszió: a hibás-reset utáni
  boot-helyreállítás véletlenül az 5x-kattintás bypass-kapcsolóhoz lett kötve.**
  Az „5 gombnyomás hozzáadása" commit (bypass mód, 2026-06-27) a `setup()`
  BROWNOUT/UNKNOWN/WDT-ági fő relé + ventilátorfokozat visszaállítását
  (`enableRelays()`/`activateMain()`/`setFanZone()`/`handleZoneChange()`)
  tévedésből `if (relaySenseBypass)` alá tette. Mivel a bypass alapból **kikapcsolt**
  (`false`), ez a **normál/gyári üzemmódban teljesen kiiktatta** a
  FIX-ESP-19/25/30/39/40 óta meglévő boot-helyreállítást: hibás reset (brownout/
  panic/WDT) után az eszköz `[boot] reason=...` bejegyzést írt, a hurok-megszakító
  számlálót is növelte, de a relék/ventilátor **soha nem álltak vissza** — pontosan
  az a „holtan marad" tünet, amit ezek a korábbi javítások megszüntettek. (A README
  szerint az 5x-kattintás célja kizárólag „a reléfigyelés és boot teszt ki/be
  kapcsolása" — a boot-helyreállításhoz semmi köze.) Javítás: a négy hívás feltétel
  nélkülire állítva, a bypass csak azt szabályozza, aminek dokumentálva van
  (`monitorFanRelays`/`checkFanRelayMismatch`/`relayBootTest`).
- **[MOD-17]** 2026-08-24: **7.14.8** — **Halott `restore_main` globális törölve.**
  A FIX-ESP-50 melletti hiba miatt bekerült változót csak írta a kód, olvasni
  soha nem olvasta.
- **[MOD-18]** 2026-08-24: **7.14.8** — **Bypass-jelző LED-villogás DRY.**
  Az 5x-kattintás kezelőjében és a `setup()`-ban szó szerint duplikált, 12 soros
  „1 mp gyors váltakozó villogás" blokk közös `bypassBlinkIndicator()` helperbe
  emelve (kisebb flash, egyetlen hely a jövőbeli módosításhoz).
- **[MOD-18b]** 2026-08-24: **7.14.8** — **OneButton API modernizálás.**
  A `button.setPressTicks()`/`setClickTicks()` az OneButton 2.6.1-ben deprecated
  (`--warnings all` mellett fordítási figyelmeztetést adott) — lecserélve a
  jelenlegi `setPressMs()`/`setClickMs()` hívásokra (a viselkedés azonos, csak a
  metódusnév változott). C3 és C6 célon is figyelmeztetés-mentesen fordul.
- **[MOD-19]** 2026-08-24: **`fan_stress.py`** — **Python 3.8 kompatibilitás.**
  A `find_address()` visszatérési típusa `str | None` (PEP 604) volt `from __future__
  import annotations` nélkül — ez a `TOOLS_README.md` által ígért Python 3.8/3.9 alatt
  `TypeError`-ral elszáll importáláskor (a `|` union-szintaxis csak 3.10-től
  értékelhető ki futásidőben). Hozzáadva a `from __future__ import annotations`.
- **[MOD-20]** 2026-08-24: **`serial_monitor.py`** — **Újracsatlakozási szál-szivárgás.**
  A `read_loop()` a kapcsolat-vesztéskor a szálindító `connect()`-et hívta újra,
  ami minden újracsatlakozáskor **egy újabb** `read_loop` szálat indított a már
  futó mellé — ismétlődő/összefésült sorokhoz és szál-felhalmozódáshoz vezetve
  hosszú, sok-újracsatlakozásos munkameneteknél. Javítás: a portnyitás
  `_open_port()`-ba különítve; a `connect()` (első csatlakozás) indítja a
  szálat, az újracsatlakozási ágak csak `_open_port()`-ot hívnak. Emellett a
  RX/TX decode-fallback `errors='ignore'`-t használt, ami **soha nem dob
  kivételt** → a hex-fallback ág holt kód volt (bináris/nem-UTF-8 adat
  csendben, hibásan jelent volna meg szövegként); most szigorú UTF-8 dekódolás
  + `UnicodeDecodeError`-ra tényleges hex-fallback.
- **[MOD-21]** 2026-08-24: **`ota_diagnostic.py`** — kihasználatlan `import struct` és
  `offset_dec`/`subtype` változók törölve; explicit, érthető hibaüzenet 0 byte-os
  (üres) firmware-fájlra ahelyett, hogy az `IndexError` az általános except-ágba
  esne. Emellett futtatással megerősített, valódi hiba: a partíciós tábla
  kiírása duplán tette ki a `0x` prefixet (`"0x0x10000"`), mert a CSV `offset`/
  `size` mezője már tartalmazza — a formázó string javítva, most helyesen
  `0x10000`-et ír.
- **[MOD-22]** 2026-08-24: **`sender/discover.py`** — az elavult
  `asyncio.get_event_loop()` + `run_until_complete()` pár lecserélve
  `asyncio.run()`-ra (a többi szkripttel egységesen), `if __name__ == "__main__"`
  őrfeltétellel.

---

## v7.14.9 — Második átvilágítási kör: OTA-puffer határellenőrzés, wrap-safe határidők, allokáció-mentes forró utak (2026-08-24)

- **[FIX-ESP-51]** 2026-08-24: **7.14.9** — **OTA heap-túlolvasás a `0xFC` hosszmezőjéből.**
  A part-vége csomag 16 bites hosszmezője (`0..65535`) ellenőrzés nélkül került az
  `otaWriteLen`-be, és ez vezérelte a `crc32_zlib(buf, blen)` **olvasását** és az
  `otaWriteBinary(..., buf, blen)` **kiírását** a `OTA_BUF_SIZE` = **16 KB** pufferből.
  Sérült hosszmező (BLE bithiba) vagy eltérő `PART`-méretű kliens esetén akár ~48 KB
  **heap-túlolvasás** történhetett (idegen heap-tartalom a `update.bin`-be írva, illetve
  potenciális összeomlás). Jellemző **inkonzisztencia**: a `0xFB` **író** ág már
  határ-ellenőrzött volt (`if ((base + x) < (int)OTA_BUF_SIZE)`), az olvasást vezérlő
  hosszmező viszont nem. Javítás: `wlen <= 0 || wlen > OTA_BUF_SIZE` → `otaAbort("bad part length")`.
- **[FIX-ESP-52]** 2026-08-24: **7.14.9** — **`DIAGCLR` streamelés közben csonkolta a naplót.**
  A `diagLog()` append-ágát a `[FIX-ESP-42]` guard védi (`if (diagStreaming) return;`),
  a `handleDiagRequest()` **törlő** ága viszont **nem** volt védve: egy folyamatban lévő
  `DIAG?` stream alatt érkező `DIAGCLR` `FILE_WRITE`-tal (truncate) írta felül, illetve
  `FLASH.remove()`-val törölte a fájlt, **miközben a `diagFile` nyitott `FILE_READ`
  handle-t tartott rá** — a stream ezután csonkolt/érvénytelen fájlból olvasott tovább.
  (A `diag_client.py --clear` megvárja a `DIAG_END`-et, de bármely más kliens — mobil app,
  `fan_stress.py` — interleavelheti a két parancsot.) Javítás: `if (diagClearRequested && !diagStreaming)`
  — a kérés **függőben marad**, és a stream lezárása után a következő híváskor fut le.
- **[FIX-ESP-53]** 2026-08-24: **7.14.9** — **További `millis()`-túlcsordulásra érzékeny határidők**
  (a `[FIX-ESP-49]` által javított hibaosztály maradék előfordulásai — **inkonzisztencia**,
  mert a `monitorFanRelays()` grace-e már a wrap-safe idiómát használta):
  1. `otaLoop()` — `millis() >= otaRebootAt`: az OTA utáni **5 s**-os, eredményküldésre
     hagyott várakozás a túlcsordulás körül kimaradt volna (azonnali reboot).
  2. `otaLoop()` — `millis() >= otaInstallWaitUntil`: ugyanez a **2 s**-os telepítés előtti
     várakozásra.
  3. `setFanZone()` — `now >= sourceLockedUntil` / `now < sourceLockedUntil`: a **2 s**-os
     BLE-vs-gomb forrás-prioritás zárolás a túlcsorduláskor korán lejárt, majd tévesen
     újra aktívnak látszott volna.
  Mindhárom a szabványos előjeles különbségre cserélve. A javítás **normál működésben
  bitre azonos** — külön host-oldali teszttel ellenőrizve (a régi idióma a normál
  tartományban 0 hibát ad, a wrap körül 2204-et; az új sehol nem hibázik).
- **[MOD-23]** 2026-08-24: **7.14.9** — **OTA forró út: heap-allokáció csomagonként.**
  Az `OtaCallbacks::onWrite()` a hosszt `pCharacteristic->getValue().length()`-ből vette;
  a `getValue()` a BLE-könyvtárban **`String`-et ad vissza érték szerint**, azaz a teljes
  csomagot **lemásolta a heapre** — csak azért, hogy a hossz megvan-e. Egy 1,1 MB-os
  firmware ~**11 500** csomag → ugyanennyi felesleges `malloc`/`memcpy`/`free` a legidőkritikusabb
  úton (heap-fragmentáció + CPU). A `getData()`/`getLength()` ugyanazt a `BLEValue`-puffert
  éri el másolás nélkül (a könyvtár forrásában ellenőrizve). `int`-ként tartva, mert a
  lenti `len - 2` **előjeles** kell legyen (1 bájtos csomagnál `size_t` alulcsordulna).
- **[MOD-24]** 2026-08-24: **7.14.9** — **BLE-parancsok: `String` allokáció parancsonként.**
  Mind az **5** parancságban (`AUTH:`/`LEVEL:`/`ROLLER:`/`DIAG?`/`DIAGCLR`) egy
  `String correctPin = BLE_AUTH_PIN;` heap-allokáció született, pusztán a
  `correctPin.length() > 0` **fordítási időben eldönthető** feltételhez; az `AUTH:` ág
  ráadásul egy `val.substring(5)` allokációval is indult. Helyette `static constexpr bool
  BLE_AUTH_REQUIRED = (sizeof(BLE_AUTH_PIN) > 1)` és `strcmp(val.c_str() + 5, BLE_AUTH_PIN)`
  — allokáció-mentes, viselkedésben azonos (host-oldali teszttel 10 határesetre ellenőrizve:
  üres/rövid/hosszú/whitespace-es PIN — 0 eltérés). Egyúttal törölve a **halott**
  `#if !defined(BLE_AUTH_PIN)` őr: a makró egy sorral fentebb mindig definiált, így
  sosem sülhetett el — az ÜRES PIN-t akarta elkapni, amit valójában a `setup()`
  `static_assert`-je ellenőriz.
- **[MOD-25]** 2026-08-24: **7.14.9** — **`String` paraméterek érték szerint.**
  `rebootEspWithReason(String)` → `const char*`, `sendOtaResult(String)` → `const String&`
  (hívásonkénti felesleges `String`-másolat megszüntetése).
- **[MOD-26]** 2026-08-24: **7.14.9** — **Halott kód.** A `lastPrint1`/`lastPrint2`/`lastPrint3`
  globálisok csak deklarálva voltak, sehol nem használva (a státusz-kiírás `static Timer`-t
  használ) — törölve. A `#include "esp_log.h"` egyetlen szimbóluma sem szerepelt a
  kódban — törölve (az `esp_system.h` marad: az `esp_reset_reason()` onnan jön).
- **[MOD-27]** 2026-08-24: **7.14.9** — **`otaSendSize` nem állt vissza bontáskor.**
  A flash-méret csomagot kapcsolatonként egyszer küldjük, de a flag a `onDisconnect`
  OTA-állapot-reset blokkjából kimaradt (miközben az összes többi OTA-flag nullázódik),
  így egy **második** OTA-kliens már nem kapta meg. A jelenlegi `sender/ota.py` nem
  használja, de a protokoll-állapot így konzisztens.

*Ellenőrzés: mindkét cél (XIAO ESP32-C3 és C6) `--warnings all` mellett hiba- és
figyelmeztetés-mentesen fordul. Flash C3: 1 146 468 → 1 145 894 bájt (−574).*

---

## v7.15.0 — Deep sleep alatt beragadó görgő-relé

*Tünet: az ESP deep sleepbe megy, és a görgő reléje **meghúzva marad** (ehhez a
tápengedélynek is aktívnak kell lennie); ébredéskor a bootkori relé-önteszt
jogosan „beragadt fő relét" jelez → failsafe → az eszköz leáll.*

- **[FIX-ESP-55]** 2026-09-02: **7.15.0** — **Kimenetek rögzítése (pad-hold) deep sleep alatt.**
  Deep sleepben a digitális IO tápdomain lekapcsol: a GPIO-k **nagyimpedanciásra
  (lebegőre)** váltanak, és az alvás **teljes ideje alatt** lebegve maradnak. A relé-
  vezérlés aktív-LOW, a `RELAY_EN` tápengedély aktív-HIGH, így alvás alatt csak a panel
  10 kΩ-os fel-/lehúzói védenek: egy kis szivárgó áram, kapacitív átkötés vagy zaj már
  behúzhatja a görgő reléjét (`RELAY_MAIN`). Az ESP32 **pad-hold** latch-e viszont az
  always-on tápdomainben van, ezért az elalvás pillanatában **aktívan hajtott** szintet
  (`RELAY_EN`=LOW, minden relé=HIGH) alvás alatt is tartja. Az `enterDeepSleep()` és a
  `setup()` mindkét korai alvás-ága (`POWERON` → alvás, illetve „gomb nélküli ébredés →
  vissza aludni") most rögzíti az 5 relé-lábat (`relayPadsHoldEnable()`), a `setup()`
  pedig — a lábak biztonságos szintre hajtása **után** — feloldja
  (`relayPadsHoldRelease()`), így nincs átmeneti glitch.
  - **C6:** van láb-szintű deep sleep hold, és a `RELAY_MAIN` (GPIO2) **RTC(LP)-láb** →
    a hold a **bootloader alatt is él**, a `setup()` feloldása **kötelező** (enélkül a
    görgő reléje soha többé nem lenne kapcsolható).
  - **C3:** nincs láb-szintű deep sleep hold, ezért a láb-szintű `gpio_hold_en()` mellé
    kell a globális `gpio_deep_sleep_hold_en()` is; ébredéskor magától felold.
  - A `gpio_deep_sleep_hold_en()` **deklarációs feltétele core-verziónként eltér**
    (IDF 5.3 / core 3.1.x: `SOC_GPIO_SUPPORT_HOLD_IO_IN_DSLP && !…SINGLE…`; IDF 5.5 /
    core 3.3.x: csak `!…SINGLE…`, a `HOLD_IO_IN_DSLP` cap megszűnt). A firmware
    **mindkettőt** lefedi egy származtatott makróval (`PAD_HOLD_NEEDS_GLOBAL_DSLP`),
    különben az egyik core-on a hívás **némán kimaradna** — nem fordítási hibával,
    hanem úgy, hogy a rögzítés C3-on nem lép életbe. Nem támogatott célnál `#error`.
  - A hold csak a **kimenetet** rögzíti; a bemeneti út él, ezért a **gombos
    GPIO-ébresztés** (a pad bemenetéről) változatlanul működik. Az ébresztő láb
    `INPUT_PULLUP`-ja az `enterDeepSleep()`-ben is explicit (eddig csak a `setup()`
    alvás-ágain volt az).
  - `DEEP_SLEEP_PAD_HOLD` makróval kikapcsolható (alapból `1`).
- **[FIX-ESP-56]** 2026-09-02: **7.15.0** — **A deep sleep nem törölte a „görgő aktív volt"
  jelzést.** Az `enterDeepSleep()` csak `disableRelays()`-t hívott: az fizikailag
  lekapcsol, de a `mainActive` / `savedMain=1` (RTC_NOINIT) és az NVS `fan/main=1`
  **érintetlen maradt** (a `deactivateMain()`-t, ami ezeket nullázza, nem hívta senki).
  Az RTC_NOINIT tartalma túléli az alvást **és** a resetek nagy részét, ezért egy alvás
  közbeni/utáni **BROWNOUT / WDT / UNKNOWN** reset boot-helyreállítása (`[FIX-ESP-19]`,
  `[FIX-ESP-22]`) ebből **magától újra bekapcsolta** a tápengedélyt és a görgőt —
  miközben a felhasználó szerint az eszköz „alszik". Javítás:
  - `enterDeepSleep()`: `deactivateMain()` (RTC-állapot 0) + `persistRelayStateOff()`
    (NVS `main=0`, `zone=0`) a `disableRelays()` előtt.
  - `setup()` `POWERON` ága (áramtalanítás után indulunk, gombra várunk): ugyanez a
    nullázás alvás előtt — különben egy alvás közbeni brownout az **NVS-fallbackből**
    indíthatta volna a görgőt.
  - `disableRelays()` a `mainActive`-ot is törli (tápengedély nélkül a görgő-relé
    fizikailag sem lehet behúzva).
  - `rebootEspWithReason()` (OTA-reboot) a reset előtt biztonságos szintre hajtja a
    reléket, hogy a reset alatti lebegő lábak ne kapcsolhassanak.
  - Az NVS-nullázás három helyen bitre azonosan ismétlődött → közös
    `persistRelayStateOff()` (`zeroStateForFailsafe`, `zeroStateForBypass`, alvás).

*Ellenőrzés: mindkét cél (XIAO ESP32-C3 és C6) hibamentesen fordul `esp32:esp32@3.1.3`
(CI-pin) és `@3.3.11` alatt is; a `PAD_HOLD_NEEDS_GLOBAL_DSLP` mindkét core-on a helyes
ágat választja (C3 → 1, C6 → 0, `static_assert`-tel ellenőrizve).*

---

## v7.16.0 — Átvilágítás friss szemmel + toolchain a 3.3.11-es core-ra

*A vizsgálat kifejezetten az `esp32:esp32@3.3.11` (IDF 5.5) core-ra készült. A 3.3-as
core-tól az alapértelmezett BLE stack **NimBLE** (a 3.1.x-ben Bluedroid volt) — a `BLE`
könyvtár API-ja azonos, a viselkedése nem; az alábbi FIX-ESP-58/61 ebből fakad.*

- **[FIX-ESP-57]** 2026-09-02: **7.16.0** — **Bukott OTA-telepítés után az eszköz
  véglegesen OTA-módban ragadt.** A `performUpdate()` három bukó ága (rossz `0xE9` magic,
  `Update.begin()` hiba, `Update.end()` hiba) `return`-nel lépett ki, **anélkül, hogy az
  `otaMode`-ot visszaállította volna**. A hívó `updateFromFS()` előtt az `otaLoop()` már
  kinullázta az `otaTotalBytes`-t, így az `OTA_INSTALL_MODE` ágon utána **egyik feltétel
  sem illeszkedett** — se telepítés, se abort. Következmény: `otaIsRunning()` örökre igaz
  → a `stateMachineStep()` azonnal visszatér, tehát **nem fut a gomb (`button.tick()`), a
  failsafe, a relé-eltérés-figyelés, a diag-kiszolgálás és az NVS-mentés sem**, a LED-ek
  OTA-mintát villognak, a relék az aktuális állapotukban maradnak. Csak a BLE-kapcsolat
  bontása (`onDisconnect`) vagy áramtalanítás hozta vissza. Javítás: a `performUpdate()`
  `bool`-t ad vissza, és bukásnál a hívó kötelezően `otaResetState()`-et hív (a diag
  naplóba is bekerül). Egyúttal a háromszor, apró eltérésekkel ismételt OTA-mező-nullázás
  (`otaAbort`, `otaWriteBinary` „SPIFFS full", `onDisconnect`) közös
  `otaResetState()`-be került — így nem maradhat ki mező (az `otaAbort` eddig pl. az
  `otaInstallWaiting`-et nem törölte).
- **[FIX-ESP-58]** 2026-09-02: **7.16.0** — **`pServer->disconnect(0)`: hardkódolt
  kapcsolat-azonosító.** A BLE-kapcsolat bontása két helyen (kézi mód bekapcsolása,
  `enterDeepSleep`) fixen a `0` azonosítót zárta. NimBLE alatt viszont a `conn_handle`-t
  a kontroller osztja (0, 1, 2 … újrahasznosítva), tehát az **első újracsatlakozás után a
  bontás egyszerűen nem történik meg** (`ble_gap_terminate` `ENOTCONN`). A kézi módnál ez
  a rosszabb: a kód utána mégis `bleConnected = false`-ra állt, így az `onDisconnect`
  **soha nem futott le** — nem nullázódott az auth/OTA/diag állapot, és a
  `bleDisconnectTime` 0 maradt, amitől a **12 perces „BLE elszállt → mindent le"
  biztonsági időzítő el sem indult**, miközben a ventilátor futott. Javítás: a könyvtár
  saját, mindkét stack alatt karbantartott `pServer->getConnId()`-je (3.1.x-ben is
  létezik). Alvás előtt így a kliens is tiszta bontást kap supervision-timeout helyett.
- **[FIX-ESP-59]** 2026-09-02: **7.16.0** — **A bootkori relé-önteszt órákkal később is
  elsülhetett.** A `relayTestPending` egyszeri jelzés volt, de a lefutást csak a
  `!bleConnected` kapuzta — időkorlát nélkül. Ha a telefon a boot utáni pillanatban
  visszacsatlakozott (advertising indulása és az első `loop()` között), a teszt függőben
  maradt, és **az első BLE-bontáskor sült el**, akár órákkal később, **működő görgő
  mellett**: `RELAY_MAIN`-t OFF-ra hajtotta és végigkapcsolta a fan-reléket, a
  `mainActive` viszont igaz maradt. Emiatt a `checkFanRelayMismatch()` nem lépett ki a
  `!mainActive` ágon, és NC-bekötésnél az **AC hiánya mind a három ágon „behúzva"-nak
  látszik** (`FAN_SENSE_AC_MEANS_ENGAGED=0`) → `relaysEnabled=false` mellett azonnali,
  **téves `STUCK` → failsafe → 10 s villogás → deep sleep**. Javítás: `RELAY_TEST_WINDOW_MS`
  (15 s) időablak, és elévülés, ha közben elindult a görgő/tápengedély; a `relayBootTest()`
  a `mainActive`-ot is nullázza (a tesztet követően a MAIN fizikailag OFF).
- **[FIX-ESP-60]** 2026-09-02: **7.16.0** — a `failSafeMode()` minden körben lehúzza a
  `RELAY_EN`-t, de a `relaysEnabled` flag `true` maradt (hamis állapot a mismatch-logika
  és a diag számára) — nullázva.
- **[FIX-ESP-61]** 2026-09-02: **7.16.0** — **NimBLE: a kézi `BLE2902` deprecated.** A
  CCCD (0x2902) leírót NimBLE automatikusan létrehozza a `NOTIFY` property mellé, és a
  könyvtár a kézi hozzáadást fel is ismeri (no-op) — de az osztály `[[deprecated]]`,
  ez adta az egyetlen két fordítási figyelmeztetést 3.3.11 alatt. A hozzáadás
  `#if !defined(CONFIG_NIMBLE_ENABLED)` mögé került (szándékosan a NimBLE HIÁNYÁT nézve,
  nem a Bluedroid meglétét: ha a Bluedroid-makró neve változna, a leíró akkor is
  bekerül — a hiánya Bluedroid alatt működésképtelen notify-t adna).
- **[FIX-ESP-62]** 2026-09-02: **7.16.0** — **OTA: csomag-hossz ellenőrzés a fejléc-mezők
  előtt.** Csonka csomagnál a `0xFB` (`pData[1]`) és a `0xFE`/`0xFF` (`pData[1..4]`) a BLE
  értékpuffer végén túl olvasott. A `0xFC`-nek már volt hossz-őre (`[FIX-ESP-51]`), a
  többinek nem — pótolva.

### Toolchain / CI: `esp32:esp32@3.1.3` → `@3.3.11`

- `build.sh` (`CORE_VERSION`), a GitHub Actions workflow (core install + cache-kulcs) és
  a `.claude/hooks/session-start.sh` (`ESP32_CORE_VERSION`) a 3.3.11-es core-ra állítva —
  ez fut éles használatban is. A forrás továbbra is fordul a 3.1.x Bluedroid stackkel.
- A hook **OneButton**-telepítése `git clone`-ra váltott: a GitHub
  `/archive/refs/tags/…` útvonalát a webes környezet hálózati szabálya 403-mal tiltja
  (csak release-asset útvonalak engedettek), ezért a lépés eddig **némán** elbukott
  (a hook `set -e` nélkül fut) és a `./build.sh` „OneButton.h not found"-dal állt meg.
  Tarball-tartalék + explicit hibajelzés is került mellé.
- README: a `DEBUG`/`OTA_DEBUG`/`BOOT_DIAG` alapértékei a valósághoz igazítva
  (mindhárom `0`), és a deep sleep-szakaszból korábban kikerült „interrupt cleanup"
  lépés leírása is a kódhoz igazítva (`[FIX-ESP-55]` körében).

*Ellenőrzés: mindkét cél (XIAO ESP32-C3 és C6) `--warnings all` mellett **hiba- és
figyelmeztetés-mentesen** fordul `esp32:esp32@3.3.11` alatt. Flash C3: 685 895 bájt
(49%), C6: 787 826 bájt (57%).*

---

## v7.16.1 — TWDT-konfiguráció: néma bukás megszüntetése

- **[FIX-ESP-63]** 2026-09-02: **7.16.1** — **A boot-kori watchdog-konfiguráció némán
  elbukhatott.** A `setup()` eddig ellenőrzés nélküli `esp_task_wdt_deinit()` +
  `esp_task_wdt_init(&wdt_config)` párral írta felül a TWDT-t. Amit az IDF 5.5
  forrásából (`components/esp_system/task_wdt/task_wdt.c`) ellenőrizve tudni lehet:
  - a `deinit()` leszedi a figyelt idle taskokat, de **`ESP_ERR_INVALID_STATE`-tel
    bukik**, ha bármely task/user még feliratkozva van (`entries_slist` nem üres);
  - az `init()` szintén **`ESP_ERR_INVALID_STATE`**-et ad, ha a TWDT már fut.

  Vagyis ha a `deinit()` elbukik, az `init()` is elbukik, és **némán a gyári 5000 ms
  marad** a szándékolt 15 000 ms helyett (a `.trigger_panic`/idle-maszk sem áll be) —
  a kód pedig egyik visszatérési értéket sem nézte.

  A **jelenlegi** core-beállítás mellett ez nem sül el: az `esp32:esp32@3.3.11`
  sdkconfigjában `CONFIG_ESP_TASK_WDT_INIT=y` (5 s, panic), viszont
  `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0` **nincs bekapcsolva**, és az Arduino
  `main.cpp` a `loopTask`-ot sem iratkoztatja fel (`loopTaskWDTEnabled = false`) —
  tehát a `setup()` idején nulla feliratkozó van, a `deinit()` átmegy. De ez a
  **körülményektől** függ (core-verzió, sdkconfig, bármely korán induló könyvtár), nem
  a kódtól — a néma 5 s-os visszaesés pedig pont a hosszan blokkoló szakaszokat
  (relé-önteszt, bypass-villogás, alvás előtti csendesítés) érintené.

  Javítás: a pontosan erre való **`esp_task_wdt_reconfigure()`** (IDF 5.3 óta létezik,
  tehát a core 3.1.x-szel is fordul), ami a **futó** TWDT-t írja át — timeout, panic és
  idle-maszk együtt, `deinit()` nélkül; `init()` már csak tartalék arra az esetre, ha a
  TWDT nem futna (`CONFIG_ESP_TASK_WDT_INIT=n`). Mindkét hívás és az
  `esp_task_wdt_add(NULL)` visszatérési értéke ellenőrzött, hiba esetén soros log **és**
  `diag.log` bejegyzés (`[boot] TWDT config failed: …` / `… add failed: …`). Ugyanez a
  védelem került a `performUpdate()` négy `esp_task_wdt_add(NULL)` visszairatkozására is:
  ha ezek elbuknának, a loopTask a futás hátralévő részére **őrizetlenül** maradna.

  > **Nyitott megfigyelés (a v7.16.2-ben megszüntetve, lásd `[FIX-ESP-64]`):** a `wdt_config.idle_core_mask =
  > (1 << 0)` a 0. mag idle taskját is figyelteti — ezt a firmware kapcsolja be (az
  > Arduino alapbeállítás nem figyeli). Ennek két következménye van: (1) a
  > `performUpdate()` `esp_task_wdt_delete(NULL)` hívása **nem teljes** védelem a hosszú
  > flash-írásra, mert az idle task figyelt marad; (2) egy TWDT-panic `TASK_WDT` reset-okot
  > ad, amit a boot-helyreállítás hibás resetnek tekint (visszakapcsolhatja a görgőt).
  > A gyakorlatban a flash-műveletek engednek futni az idle tasknak, ezért maradt.

*Ellenőrzés: mindkét cél `--warnings all` mellett hiba- és figyelmeztetés-mentesen fordul
`esp32:esp32@3.3.11` alatt (C3 685 799 B / 49%, C6 787 842 B / 57%).*

---

## v7.16.2 — A watchdog csak a `loop()`-ot figyeli

- **[FIX-ESP-64]** 2026-09-02: **7.16.2** — **`wdt_config.idle_core_mask`: `(1 << 0)` → `0`.**
  Eddig a firmware a 0. mag **idle taskját** is felíratta a TWDT-re (az Arduino gyári
  beállítása ezt nem teszi). Ez a bejegyzés nem azt kérdezi, hogy „él-e a program" — azt a
  `loopTask` bejegyzése méri, amit a `loop()` eleje etet —, hanem hogy **„volt-e a CPU-nak
  üresjárata"**: az idle task a legalacsonyabb prioritású, tehát csak akkor fut (és eteti a
  watchdogot az `idle_hook_cb`-n keresztül), ha semmi más nem futóképes.

  Egymagos chipen (`CONFIG_FREERTOS_UNICORE=y` mindkét célon) ez alig ad pluszt: ha bármi
  ténylegesen felzabálja a CPU-t, a `loopTask` sem jut futáshoz, tehát a saját bejegyzésünk
  amúgy is eldurran. Egyedül az az eset marad, amikor egy task úgy pörög, hogy közben a
  `loop()` még kap időszeletet (azonos prioritáson vagy `taskYIELD()`-del), az idle viszont
  soha — ilyen task ebben a firmware-ben nincs (saját taskot nem hozunk létre, a BLE
  host/kontroller taskok pedig magasabb prioritásúak).

  Az ára viszont valós volt:
  1. **Féloldalassá tette a `performUpdate()` `esp_task_wdt_delete(NULL)` hívását**: a
     saját taskot levettük a hosszú flash-írás idejére, az idle bejegyzés viszont élesben
     maradt. Mostantól a leiratkozás teljes — nem marad TWDT-bejegyzés, így az
     `esp_task_wdt_reconfigure()`/timer-logika a timert is leállítja
     (`if (!SLIST_EMPTY(&entries_slist)) restart`).
  2. **Egy téves pánik itt drágább, mint a hiba, amit véd**: a TWDT-panic `TASK_WDT`
     reset-okot ad, amit a boot-helyreállítás (`[FIX-ESP-22]`) hibás resetnek tekint, tehát
     **visszakapcsolhatja a görgőt**. Fals riasztásra pedig volt esély: a nem engedő
     szakaszok (pl. a `relayTestWait()` 200 ms-os `delayMicroseconds()` ciklusai) épp az
     idle taskot éheztetik — és pontosan ezért kapcsolja ki az Arduino is alapból az
     idle-figyelést (`CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0` nincs beállítva).

  A watchdog jelentése így pontosan az lett, amit ez a firmware akar: **„iterál-e még a
  `loop()`"** — 15 s-os timeouttal, panickal.

*Ellenőrzés: mindkét cél `--warnings all` mellett hiba- és figyelmeztetés-mentesen fordul
`esp32:esp32@3.3.11` alatt (C3 685 799 B / 49%, C6 787 842 B / 57%).*

---

## v7.17.0 — Karbantarthatóság: duplikációk kiemelése + fordítási idejű védőhálók

*Ez a kör szándékosan **viselkedés-semleges**: nem hibát javít, hanem azt csökkenti, hogy
a következő módosítás hibát tudjon becsempészni. A bináris nem lett bitre azonos (ez egy
de-duplikációtól nem is várható), viszont **kisebb** lett: C3 685 799 → 685 717 bájt,
C6 787 842 → 787 760 bájt.*

### Fordítási idejű pin-ellenőrzések (új védőháló)

A `PINS` blokk két cél (C3/C6) **kézzel karbantartott** listája, ahol a legkönnyebben
elkövethető hiba, hogy két funkció ugyanarra a GPIO-ra kerül — a C3-on ez különösen éles
(GPIO20/21 = U0RXD/U0TXD, GPIO2/8/9 = strapping lábak). Két `static_assert` került be:

- **Pin-ütközés:** a használt lábakból bitmaszk készül (`pinBit`), és a `pinCount()`
  `constexpr` bitszámláló eredményét a listaelemek darabszámához hasonlítjuk. Ha két
  funkció ugyanarra a lábra kerül, a maszk kevesebb bites → **fordítási hiba**, nem pedig
  rejtélyes működés a panelon. A `FAN_SENSE_ENABLE` / C6-specifikus lábak feltételesen
  kerülnek a maszkba és a darabszámba.
- **Ébresztő láb:** `SOC_GPIO_DEEP_SLEEP_WAKE_VALID_GPIO_MASK & pinBit(BUTTON_PIN)` — a
  gombnak deep sleepből kell ébresztenie, erre csak az RTC/LP-képes lábak alkalmasak
  (C3: GPIO0–5, C6: GPIO0–7). Rossz láb esetén az eszköz **egyszerűen nem ébredne fel**;
  most ez fordításkor derül ki.

  *Mindkét ellenőrzés kipróbálva: szándékos ütközés (`FAN2_SENSE_PIN` 7 → 6) és nem
  ébresztésképes gombláb (`BUTTON_PIN` 3 → 7) mellett a fordítás a saját üzenetünkkel áll le.*

  A blokk szándékosan az **első függvénydefiníció után** áll, nem a `PINS` mellett: az
  `.ino` automatikus prototípus-generálása a legelső függvénydefiníció elé szúrja be a
  prototípusokat, így egy korán definiált `constexpr` függvény eltolná a beszúrási pontot
  (a `CommandSource` enum elé) és elrontaná a fordítást.

### Duplikációk kiemelése (viselkedés változatlan)

- **`fanRelaysOff()`**: a „mindhárom fokozat-relé OFF" hármas **nyolc** helyen szerepelt
  szó szerint (break-before-make, MAIN lekapcsolás, `enableRelays`/`disableRelays`,
  failsafe, bootteszt ×3). Több helyen épp az a lényeg, hogy **egyik** fan se maradjon
  behúzva — jobb, ha ez egy néven nevezett művelet, mint három egymás mellé másolt sor.
- **`ledBlink()` / `ledHeartbeat()`**: a piros és a sárga LED ága szó szerint ugyanazt a
  villogás- és életjel-szerkezetet másolta le, csak más lábbal és más állapotváltozókkal.
  A `handleLEDs()` ~100 sorról ~25-re rövidült, az állapotváltozók referenciaként mennek
  át. Egyúttal kiesett a `bleEnabled && !bleConnected` redundáns fele (abba az ágba
  eleve csak `!bleConnected` mellett lehet eljutni).

### Olvashatóság / konzisztencia

- **OTA opkódok néven nevezve**: a `0xFB`/`0xFC`/`0xFD`/`0xFE`/`0xFF`/`0xEF` és a
  `0x0F`/`0xF1`/`0xF2` eddig nyers hex-literálként szerepelt szétszórva, a protokoll
  leírása pedig kizárólag a `sender/ota.py`-ban és a READMÉ-ben élt. Mostantól
  `OTA_RX_*` / `OTA_TX_*` konstansok, a csomagformátummal a definíciójuk mellett.
  (Az érték egyetlen bitje sem változott; a `0xEF` továbbra is két különböző dolgot
  jelent a két irányban — ezt most már a két külön név is mutatja.)
- **`FLASH` vs. `SPIFFS`**: a `setup()` négy helyen közvetlenül `SPIFFS.`-t hívott,
  miközben a kód többi része a `FLASH` makrót használja. Egységesítve — egy esetleges
  későbbi fájlrendszer-váltás így tényleg egyetlen `#define` módosítása.
- **Halott kód**: a `bootMagic` RTC_NOINIT változó + `BOOT_MAGIC` makró csak írva volt,
  soha nem olvasva — törölve.
- `sender/ota.py`: `from __future__ import print_function` (Python 2-es maradvány) törölve;
  a `bleak` amúgy is Python 3.8+-t igényel.

### CI: a sketch figyelmeztetései hibának számítanak

A GitHub Actions build mostantól `--warnings all`-lal fordít, és **elbukik**, ha a
`FanController_OTA_debug.ino` sorára esik `warning:`. Szándékosan csak a saját forrásunk
figyelmeztetéseire — egy jövőbeli core-/könyvtár-frissítés zaja ne törje a CI-t olyasmin,
amit nem mi javítunk. Ez pont azt a fajta dolgot fogja meg automatikusan, amit a 3.3.11-re
váltáskor kézzel kellett észrevenni (a NimBLE alatt `[[deprecated]]` `BLE2902`).

*Ellenőrzés: mindkét cél `--warnings all` mellett hiba- és figyelmeztetés-mentesen fordul
`esp32:esp32@3.3.11` alatt; a CI-kapu logikája helyben kipróbálva mindkét irányban.*

---

## v7.18.0 — SPIFFS → LittleFS

- **[FIX-ESP-65]** 2026-09-02: **7.18.0** — **A `spiffs` partíción LittleFS fut, nem SPIFFS.**
  A kód oldalán ez ténylegesen **két sor** (`#include` + a `FLASH` makró), mert a
  fájlrendszert az egész forrás a `FLASH` makrón át éri el (a `setup()` maradék négy
  közvetlen `SPIFFS.` hívását a v7.17.0 egységesítette), a `LittleFSFS` pedig ugyanúgy
  `FS`-leszármazott: a `File` API (`open/read/write/seek/available/readStringUntil/close`),
  a `FILE_READ/WRITE/APPEND` és a `totalBytes()/usedBytes()/format()/exists()/remove()`
  szignatúrája azonos.

  **Partíciós tábla nem változik:** a `LittleFS.begin()` alapértelmezett
  `partitionLabel`-je `"spiffs"`, tehát ugyanazt a partíciót csatolja.

  **Miért érte meg épp ennek az eszköznek:**
  - **Áramszünet-biztonság.** A LittleFS copy-on-write, páros metaadat-blokkokkal: írás
    közbeni áramtalanítás nem hagy sérült, felcsatolhatatlan fájlrendszert (a SPIFFS-nél
    ez reális kimenet). Ez a firmware pont a rossz pillanatokban ír: a `diag.log`
    bejegyzés **brownout/WDT reset után, bootkor** születik, az OTA pedig egy ~0,7 MB-os
    `/update.bin`-t stagel a partícióra.
  - **Telített fájlrendszer.** A SPIFFS ~75–80% fölött a szemétgyűjtés miatt drasztikusan
    belassul — az OTA viszont épp jócskán megtölti a partíciót. A LittleFS a nagy fájlt és
    az append-et lényegesen jobban bírja.
  - A SPIFFS felfelé gyakorlatilag karbantartatlan; új terveknél a LittleFS az ajánlott.

  **A mount kétlépcsős lett.** Eddig `begin(FORMAT_..._IF_FAILED)` volt egyetlen hívásban,
  ami elrejti, hogy kellett-e formázni. Most előbb formázás **nélkül** próbálunk csatolni,
  és ha ez nem megy, formázunk + naplózunk (`[fs] mount failed -> formatted`). Így látszik
  a naplóban a SPIFFS→LittleFS váltás egyszeri formázása **és** egy esetleges későbbi
  fájlrendszer-sérülés is. (A `begin()` felcsatolt állapotban azonnal `true`-val tér vissza
  — `esp_littlefs_mounted()` ellenőrzéssel —, ezért a kétlépcsős hívás biztonságos.)

  **Egyszeri hatás a frissítéskor:** az első boot a régi, SPIFFS-formátumú partíciót nem
  tudja LittleFS-ként felcsatolni, ezért **megformázza** → a korábbi `diag.log` elveszik.
  Ugyanez fordítva is igaz: egy SPIFFS-es buildre visszagörgetve az is formázna egyet.

  **Átnevezések és szövegek:** `FORMAT_SPIFFS_IF_FAILED` → `FORMAT_FS_IF_FAILED`,
  `SPIFFS_OVERHEAD` → `FS_OVERHEAD`, a log-/hibaszövegekben „SPIFFS" → „FS"
  (`ERR: FS full`, `ERR: FS too small (need …)`). A BLE-n visszaküldött hibaszövegre
  **egyik Python eszköz sem illeszt** (csak kiírja), ezért ez biztonságos csere.

  **Ár:** flash C3 685 717 → **692 383** bájt (49% → 50%), C6 787 760 → **794 414** bájt
  (57%); statikus RAM +120 bájt. A LittleFS a felcsatoláskor pár kB heapet foglal a
  cache-nek — ahogy a SPIFFS is.

*Ellenőrzés: mindkét cél `--warnings all` mellett hiba- és figyelmeztetés-mentesen fordul
`esp32:esp32@3.3.11` alatt.*

