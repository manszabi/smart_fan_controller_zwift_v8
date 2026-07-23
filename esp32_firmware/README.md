# FanController_OTA_debug

ESP32-C3/C6 alapú **háromfokozatú ventilátor- és görgővezérlő**, BLE-n keresztül
irányítható, OTA firmware-frissítéssel, és beépített diagnosztikai naplóval.

**Aktuális firmware verzió:** `7.14.7` (2026-07-23)

> **Fő relé (`RELAY_MAIN`):** ez a relé kapcsolja a **görgőt ÉS a ventilátor tápját**
> együtt (egy edzéshez kell a görgő; görgő/táp nélkül a ventilátor haszontalan).
> Korábban `RELAY_ROLLER` néven csak a görgőt kapcsolta. A hozzá tartozó állapotot a
> `mainActive` jelzi, az NVS-kulcs `fan/main`.

---

## Tartalom

- [Áttekintés](#áttekintés)
- [Hardver / pinkiosztás](#hardver--pinkiosztás)
- [Fan relé BONTÓ-érintkező figyelése (H11AA1M)](#fan-relé-bontó-érintkező-figyelése-h11aa1m)
- [Működés](#működés)
  - [Üzemmódok](#üzemmódok)
  - [Gombvezérlés](#gombvezérlés)
  - [BLE vezérlés és parancsok](#ble-vezérlés-és-parancsok)
  - [Relé-kapcsolás (break-before-make)](#relé-kapcsolás-break-before-make)
  - [Failsafe (biztonsági) állapot](#failsafe-biztonsági-állapot)
  - [Deep sleep (energiatakarékos alvás)](#deep-sleep-energiatakarékos-alvás)
- [Bootkori relé-önteszt (beragadt fő relé detektálása)](#bootkori-relé-önteszt-beragadt-fő-relé-detektálása)
- [Fokozat-mentés és visszaállítás (RTC + NVS hibrid)](#fokozat-mentés-és-visszaállítás-rtc--nvs-hibrid)
- [Diagnosztikai napló](#diagnosztikai-napló)
- [OTA firmware-frissítés](#ota-firmware-frissítés)
- [Particionálás](#particionálás)
- [Diagnosztikai eszközök (Python)](#diagnosztikai-eszközök-python)
- [Fordítás (build)](#fordítás-build)
- [Időzítések összefoglaló](#időzítések-összefoglaló)
- [Verziótörténet (kivonat)](#verziótörténet-kivonat)

---

## Áttekintés

Az eszköz egy 230V AC ventilátort vezérel három fokozatban (33% / 66% / 100%),
plusz egy **fő relét** (`RELAY_MAIN`), ami a görgőt és a ventilátor tápját
együtt kapcsolja. A vezérlés történhet:

- **BLE-n** (telefonról / PC-ről, PIN-es hitelesítéssel), vagy
- **fizikai gombbal** (kézi mód).

A firmware kiemelt figyelmet fordít a **tápoldali instabilitásra (BROWNOUT)**,
amit a 230V induktív terhelés relé-kapcsoláskori árama okoz. Ezért:

- a fokozatot **azonnal** és **tartósan** is elmenti (hibrid RTC + NVS),
- hibás reset után **automatikusan visszaáll** a kapcsolás előtti állapotba,
- minden hibás resetet **naplóz**, ami BLE-n lekérdezhető.

---

## Hardver / pinkiosztás

A pinkiosztás **cél-chip szerint** automatikus (a firmware a `CONFIG_IDF_TARGET_ESP32C6`
makró alapján választ — lásd a `build.sh` `TARGET` opcióját):

| Funkció | GPIO (XIAO C3) | GPIO (XIAO C6) |
|---|---|---|
| Ventilátor 1. fokozat relé (`RELAY_FAN1`) | 10 | 23 |
| Ventilátor 2. fokozat relé (`RELAY_FAN2`) | 9 | 22 |
| Ventilátor 3. fokozat relé (`RELAY_FAN3`) | 8 | 21 |
| Fő relé — görgő + ventilátor táp (`RELAY_MAIN`) | 2 | 2 |
| Relé tápengedélyezés (`RELAY_EN`) | 21 | 17 |
| Nyomógomb (`BUTTON_PIN`) | 3 | 1 |
| Sárga LED (`LED_YELLOW`) | 5 | 0 |
| Piros LED (`LED_RED`) | 4 | 16 |
| Fan1 bontó-érintkező-figyelő opto (`FAN1_SENSE_PIN`, H11AA1M) | 6 | 19 |
| Fan2 bontó-érintkező-figyelő opto (`FAN2_SENSE_PIN`, H11AA1M) | 7 | 20 |
| Fan3 bontó-érintkező-figyelő opto (`FAN3_SENSE_PIN`, H11AA1M) | 20 | 18 |

> A relék **aktív-LOW** vezérlésűek (`digitalWrite(..., LOW)` = bekapcsol).
>
> A `FANx_SENSE_PIN` lábak **belső pullup**-pal (`INPUT_PULLUP`) működnek.
> **C3-specifikus megjegyzés:** a GPIO20 az ESP32-C3 U0RXD lába — ez csak akkor
> szabad, ha a `Serial` **USB-CDC** (és nem a hardveres UART0); mivel a `RELAY_EN`
> a GPIO21 (U0TXD) kimenetként van használva, ez teljesül. A C6-os kiosztásnál a
> SENSE lábak (19/20/18) nem ütköznek az USB-CDC-vel.
>
> **C6 antenna:** a XIAO ESP32-C6-on a firmware bootkor a **külső antennát**
> választja az RF-kapcsolón: `RF_SWITCH_EN`=GPIO3 LOW (kapcsoló engedélyezés) +
> `ANT_SELECT`=GPIO14 HIGH (külső antenna; LOW = beépített). Ezek saját `#define`-ok
> a C6 PINS blokkban (a XIAO variáns `WIFI_ENABLE`/`WIFI_ANT_CONFIG` megfelelői), így
> generikus C6 boardra is fordul. A 2,4 GHz rádiót Wi-Fi/BLE/802.15.4 közösen
> használja egy antenna-kapcsolóval, ezért ez a **BLE-re is** vonatkozik.

### Panel passzív alkatrészek

A panelra a stabil boot-/kapcsolási viselkedés és a zajszűrés érdekében az alábbi
passzív alkatrészek kerültek:

| Hely | Alkatrész | Szerep |
|---|---|---|
| Relé-vezérlő GPIO-k (`RELAY_FAN1/2/3`, `RELAY_MAIN`) | 10 kΩ **felhúzó** | boot/tranziens alatt definiált HIGH → relék **OFF** (aktív-LOW), lebegés ellen |
| Relé tápengedély (`RELAY_EN`) | 10 kΩ **lehúzó** | boot alatt biztos LOW → relék tiltva (különösen a C6 GPIO17 belső felhúzása ellen, `[FIX-ESP-39]`) |
| Nyomógomb (`BUTTON_PIN`) | 22 kΩ felhúzó + 100 nF a kapcsolóval **párhuzamosan** | definiált alapszint + hardveres debounce |
| H11AA1M **bemenet** (230V AC oldal) | 2 × 120 kΩ soros | áramkorlátozás az antiparallel LED-párhoz |
| H11AA1M **kimenet** (`FANx_SENSE_PIN`) | 22 kΩ felhúzó + 100 nF ∥ 1 µF | definiált HIGH + RC-szűrés a ~100 Hz-es AC-ripple-re (a belső `INPUT_PULLUP` mellett) |
| LED-ek (sárga / piros) | 330 Ω soros | áramkorlátozás |
| 5V USB táp | 1000 µF ∥ 100 nF | táppuffer/szűrés a relé-kapcsolási áramlökések ellen (brownout-csökkentés) |

> A 10 kΩ felhúzók (relé-vezérlés) és a 10 kΩ lehúzó (`RELAY_EN`) hardveresen is
> biztosítják a `setup()` legelején szoftveresen beállított „minden relé OFF +
> tápengedély LOW" boot-állapotot, így a boot-pillanatbeli áramlökés/brownout-esély
> kisebb. A H11AA1M kimenetén lévő RC-tag (22 kΩ + 100 nF ∥ 1 µF) simítja a
> nullátmeneteknél jelentkező 100 Hz-es ripple-t; a firmware emellett is idő-ablakos
> mintavételt használ (lásd a következő szakaszt).

---

## Fan relé BONTÓ-érintkező figyelése (H11AA1M)

3 db **H11AA1M** AC-bemenetű optocsatoló figyeli a fan relék **bontó (NC)
érintkezőjét**, hogy relénként megállapítható legyen, melyik fokozat-relé húzott be.

> **Miért a bontó-érintkező, és nem a relé kimenete?** A ventilátorban a
> fokozat-tekercsek **sorosak**, ezért **egyetlen** aktív fokozatnál a 230V AC
> **minden** kimeneti ágon megjelenik. A relé KIMENETÉT (NO-ág) figyelve tehát
> minden ágon „van AC" látszana, és nem lehetne megkülönböztetni, melyik relé
> aktív (téves STUCK a nem aktív fokozatokon). A **bontó (NC) érintkező** viszont
> **relénként egyedileg** tükrözi az adott relé saját kapcsolási állapotát,
> függetlenül attól, merre folyik az AC a kimeneteken.
>
> A sense-jelet a `FAN_SENSE_AC_MEANS_ENGAGED` makró képezi le „relé behúzva"-ra
> (**alapból `0`**: NC-bekötés → AC a sense-ágon ⇒ a relé **NINCS** behúzva). Így a
> `fanRelayEngaged[i]` jelentése „az `i`. fokozat aktív" marad, és az alábbi
> `elvárt vs. mért` failsafe-logika érintetlen, továbbra is helyes.

**FONTOS — miért nem elég egy `digitalRead`, és miért LOW-alapú a detektálás:** a
H11AA1M a bemenetén lévő 230V AC-ra **LOW-t** húz (vezet a fototranzisztor), AC nélkül
a belső felhúzó **HIGH**-ra húz. A vezetés viszont **nem folyamatos**: a nullátmenetek
körül ~**100 Hz**-cel rövid időre megszakad (HIGH-tüske). Ezért a program **idő-ablakot**
figyel, és kifejezetten a **LOW mintát** keresi: ha az utóbbi `AC_SENSE_WINDOW_MS`
(**40 ms**, > 1 hálózati periódus) ideje alatt **volt LOW minta**, akkor **van AC** a
sense-ágon; ha végig HIGH, akkor **nincs**. Erre `80 ms` debounce és a relé-parancs
utáni `300 ms` türelmi idő (`FAN_SENSE_GRACE_MS`) épül (~2× a ~150 ms sense-beállásra).

> **Miért robusztus a LOW-alapú detektálás (a HW-szűrőtől függetlenül):** AC
> jelenlétében a jel — akár hardveresen szűrve (stabil LOW), akár szűretlenül
> (túlnyomóan LOW + rövid nullátmeneti HIGH-tüskék) — **mindig ad LOW mintát**; a
> HIGH-tüskéket szándékosan **ignoráljuk**. Így az opto-kimeneti RC-szűrő (lásd a
> *Panel passzív alkatrészek* szakaszt) **kiesése vagy a kondenzátor kiszáradása esetén
> sem keletkezik téves STUCK** → nincs téves failsafe. (Egy korábbi, a HIGH-szintet
> figyelő változat a tüskéktől megzavarodott volna szűrő nélkül.)

A **bontó-érintkezős** bekötésnél: a relé **behúzva** → NC **nyitva** → nincs AC a
sense-ágon → nincs LOW minta → **`fanRelayEngaged[i]=TRUE`**. Vagyis a `TRUE` jelentése
„az `i`. relé behúzva (a fokozat aktív)" — *nem* „van AC a kimeneten".

A mért állapotot (`fanRelayEngaged[]`) a program összeveti az **elvárttal**
(`relaysEnabled && currentZone == fan`), és a két eltérés-irányra **aszimmetrikusan**
reagál (`DIAG?` paranccsal a `diag.log` lekérdezhető):

- **STUCK** – a zóna **OFF**, de a mérés **aktívat** jelez → beragadt/hegedt relé.
  Reakció: **azonnali `STATE_FAILSAFE`** + figyelmeztetés + `diag.log`. A `diag.log`
  **szinkron** (flush) íródik a failsafe-be lépés **előtt**, így a naplózás nem
  szakad félbe. (A `fanRelayEngaged` ekkor már a 40 ms ablak + 80 ms debounce-on átment.)
- **NOAC** – a zóna **ON**, de a mérés **inaktív** → relé/biztosíték/ventilátor/hálózat hiba.
  Reakció: `FAN_SENSE_MISMATCH_CONFIRM_MS` (**300 ms**) debounce után **egyszeri**
  figyelmeztetés + `diag.log`, **failsafe NÉLKÜL** — a rendszer fut tovább. A debounce
  a grace **után** számol (a két relé közti break-before-make átmenetet a grace fedi,
  ezért az nem ad téves NOAC-ot). A latch (`fanNoacWarned`) megakadályozza az ismételt
  naplózást, az eltérés megszűntével (vagy relé-parancsnál) újraélesedik.

> **Fő relé OFF → nincs értékelés (7.14.0):** mivel a `RELAY_MAIN` adja a ventilátor
> tápját, **kikapcsolt fő relénél nincs AC** a fan-ágakon, így a sense értelmezhetetlen
> (NC-bekötésnél minden relé „behúzva"-nak látszana → téves STUCK; NO-nál téves NOAC).
> Ezért a `checkFanRelayMismatch()` `!mainActive` esetén **kilép** — nincs téves failsafe.
> A fokozatváltás fő relé nélkül is végrehajtódik (a relé kapcsol); a fő relé
> bekapcsolásakor (`activateMain`) türelmi idő (grace) indul, hogy az AC stabilizálódjon.

A teljes funkció a program elején, a `DEBUG`/`OTA_DEBUG`/`BOOT_DIAG` kapcsolók
mellett a **`FAN_SENSE_ENABLE`** makróval ki/be kapcsolható (`1` = be, `0` = ki).
**Jelenleg `1` (bekapcsolva).** Kikapcsolva a hozzá tartozó kód **bele sem fordul**,
és a SENSE lábak szabadon maradnak. Finomhangolás a PINS szekció után:
**`FAN_SENSE_AC_MEANS_ENGAGED`** (bekötés-leképezés, **alapból `0`** a bontó-érintkezős bekötéshez),
`FAN_SENSE_FAILSAFE_ON_STUCK` (STUCK → failsafe),
`FAN_SENSE_WARN_ON_NOAC` (NOAC → figyelmeztetés), valamint az időzítő konstansok.

---

## Működés

### Üzemmódok

- **STATE_NORMAL** – normál működés: BLE vagy kézi vezérlés, fő relé (görgő + táp) + fokozatok.
- **STATE_FAILSAFE** – biztonsági állapot, ha egyszerre **2 vagy több** ventilátor-relé
  aktív (hibás állapot), a bontó-érintkező figyelés **STUCK**-ot jelez, **vagy** a bootkori
  relé-önteszt **beragadt fő relét** észlel. Ekkor minden relé lekapcsol és mindkét LED villog.

### Gombvezérlés

A `OneButton` könyvtár kezeli a gombot:

| Gesztus | Hatás |
|---|---|
| **Egyszeres kattintás** | Ha minden ki: relék + fő relé (görgő/táp) be. Ha aktív ventilátor fokozat: a ventilátor leáll (a fő relé bekapcsolva marad). Ha a fő relé jár és a fokozat 0: fő relé ki + relék ki. |
| **Dupla kattintás** | **Kézi mód** be/léptetés. Belépéskor leállítja a BLE-t, és fokozatot léptet: 1 → 2 → 3 → 0 → 1 … (a fő relét feltételezi — előbb egy kattintás kapcsolja be). |
| **Három 3x kattintás** | Vissza **automata módba**: ventilátor ki, BLE advertising újraindul. |
| **Öt 5x kattintás** | A reléfigyelés és boot teszt ki/be kapcsolása. Aktiválását a két led felváltva villogása jelzi, gomnyomáskor gyors boot után lassabb a villogás. NVS memória tárolja. |
| **Hosszú nyomás (elengedésre)** | **Deep sleep** (`src=button-longpress`). |

### BLE vezérlés és parancsok

Az eszköz neve advertisingban: **`FanController`**.

Két BLE szolgáltatás fut:

- **Ventilátor-vezérlés** – Service `0000ffe0-…`, karakterisztika `0000ffe1-…`
- **OTA** – Service `fb1e4001-…` (RX `…4002`, TX `…4003`)

A vezérlő parancsokat a `ffe1` karakterisztikára kell írni (UTF-8 szöveg):

| Parancs | Leírás | Válasz |
|---|---|---|
| `AUTH:<pin>` | Hitelesítés (alapért. PIN: `123456`) | `AUTH_OK` / `AUTH_FAIL` / `AUTH_LOCKED` |
| `LEVEL:<0-3>` | Fokozat: 0 = ki, 1 = 33%, 2 = 66%, 3 = 100% | — (hiba esetén `AUTH_REQUIRED`) |
| `ROLLER:<0/1>` | Fő relé (görgő + ventilátor táp) ki (0) / be (1). A wire-parancs neve maradt `ROLLER:` (app-kompatibilitás). | — |
| `DIAG?` | Diagnosztikai napló lekérése | `0x02"DIAG_BEGIN"` … `0x04"DIAG_END"` |
| `DIAGCLR` | Diagnosztikai napló törlése | `DIAG_CLEARED` |

**Hitelesítés:** minden vezérlő parancs (LEVEL/ROLLER/DIAG) **AUTH-ot igényel**.
Sikertelen kísérletek: **5 próbálkozás után 60 mp-es zárolás** (`AUTH_LOCKED`).

### Relé-kapcsolás (break-before-make)

Fokozatváltáskor előbb **lekapcsol minden ventilátor-relé**, majd
`RELAY_SWITCH_DELAY_MS` (**10 ms**) szünet után kapcsol be az új fokozat.
Így nincs átfedés („make-before-break"), és a táp-tranziens rövid.

> Megjegyzés: a tényleges break-idő ~20 ms, mert a `handleZoneChange()` csak a
> 20 ms-os `checkInterval` ütemében fut. A 230V AC ventilátor okozta BROWNOUT
> teljes megszüntetése csak **hardveres RC-snubber + MOV varisztor** és nagyobb
> szűrőkondenzátor mellett lehetséges; a firmware csak enyhíti.

### Failsafe (biztonsági) állapot

Ha a `normalMode()` azt észleli, hogy **2 vagy több** ventilátor-relé egyszerre
aktív (beragadt / inkonzisztens relé), vagy a bontó-érintkező figyelés **STUCK**-ot
jelez (zóna OFF, de aktív mérés), vagy a **bootkori relé-önteszt beragadt fő relét**
észlel (lásd lentebb), azonnal `STATE_FAILSAFE`-be vált:

- minden relé lekapcsol (`RELAY_EN` LOW),
- mindkét LED ~2 Hz-cel villog,
- **10 mp** után az eszköz deep sleepbe megy (`FAILSAFE_TIMEOUT_MS`, `src=failsafe-timeout`).

> **Failsafe belépéskor a fő relé (görgő/táp) ÉS a fokozat állapota minden tárolóban
> lenullázódik** (`[FIX-ESP-30b]`): logikai állapot (`currentZone=0`,
> `mainActive=false`), RTC (`savedZone=0`, `savedMain=0`) **és** NVS
> (`zone=0`, `main=0`). Így ha failsafe **közben** egy hibás reset történik
> (akár BROWNOUT, ami az RTC-t is törli), a boot-helyreállítás **semmiképp nem
> indítja újra** a görgőt/ventilátort egy aktív hardverhiba mellett — az eszköz
> `idle` állapotban marad. Az NVS-írás itt egyszer fut le (a failsafe belépésekor).

### Deep sleep (energiatakarékos alvás)

Az eszköz deep sleepbe lép:

- **gombos hosszú nyomásra** (`button-longpress`),
- **1 óra tétlenség** után (`idle-timeout`, csak ha nincs BLE és nincs kézi mód),
- **failsafe** 10 mp után (`failsafe-timeout`).

> **Mi számít „aktivitásnak" (a tétlenség-időzítő nullázásához):** a **fő relé be**
> ÉS megy a ventilátor — automatikus (BLE) módban **élő BLE-kapcsolattal**
> (`mainActive && bleConnected && currentZone≠0`), kézi módban anélkül is
> (`mainActive && manualMode && manualZoneIndex≠0`). A fő relé/görgő futása
> önmagában (ventilátor nélkül) **nem** számít aktivitásnak.

Ébresztés: a **gomb** (GPIO low) megnyomásával.

A `enterDeepSleep()` alvás előtt gondosan „lecsöndesíti" a rendszert, hogy
elkerülje az INT_WDT(5) watchdog-resetet ébredéskor:

1. BLE lecsatlakozás → **500 ms**
2. advertising stop → **300 ms**
3. relék ki → **200 ms** (GPIO settle)
4. LED-ek ki → **200 ms** (GPIO settle)
5. **interrupt cleanup**: `portDISABLE_INTERRUPTS()` +
   `esp_intr_disable_source(ETS_GPIO_INUM)` + `portENABLE_INTERRUPTS()` → **100 ms**
6. korábbi wakeup-források törlése (`esp_sleep_disable_wakeup_source(ALL)`),
   majd csak a gomb GPIO wakeup beállítása
7. **500 ms** végső stabilizáció → `esp_deep_sleep_start()`

---

## Bootkori relé-önteszt (beragadt fő relé detektálása)

A `RELAY_TEST_AT_BOOT` kapcsolóval (alapból **`1`**) bekapcsolható önteszt, ami a
`loop()` elején, **a BLE-kapcsolat előtt**, egyszer fut le. Csak akkor, ha az eszköz
**futásra ébredt** — azaz **software reset** (pl. OTA után) vagy **gombébresztés**
esetén; hiba miatti újraindulásnál (brownout/WDT/panic) **nem** fut, hogy ne zavarja
a fokozat-visszaállítást.

Menete:

- `RELAY_EN` be, a **fő relé (`RELAY_MAIN`) végig OFF**, a ventilátor-reléket
  `FAN1 → FAN2 → FAN3` sorban kapcsolja (egyszerre **csak egy** lehet aktív; időzítés:
  `RELAY_TEST_ON_MS`=120 ms, `RELAY_TEST_GAP_MS`=60 ms).
- Mivel a fő relé OFF, a fan-ágakon **nem szabad AC-nak lennie**. A teszt közben
  mintázza a H11AA1M bontó-érintkezőket: ha **mégis van AC**, az a **fő relé
  beragadását** (zárva ragadt → táp átszivárog) jelzi. A figyelt vonal a bekötéshez
  igazodik (`FAN_SENSE_AC_MEANS_ENGAGED`): NC-nél a kikapcsolt fanek bontóján, NO-nál
  a bekapcsolt fan érintkezőjén.
- Beragadás esetén: `DBG("main_relay stuck")` + `diag.log: [relay] main stuck!` +
  azonnali **`STATE_FAILSAFE`**.

A teljes teszt ~0,5 s, így a boot nem nyúlik hosszúra.

---

## Fokozat-mentés és visszaállítás (RTC + NVS hibrid)

A ventilátor-fokozatot **és a fő relé (görgő/táp) állapotát** is **két**
mechanizmus tárolja, egymást kiegészítve:

### RTC_NOINIT (gyors, resetet túlél)

- **Mikor:** azonnal — a fokozat a `handleZoneChange()`-ben (**még a relé fizikai
  kapcsolása ELŐTT**, ugyanabban a critical szekcióban), a fő relé (görgő/táp) pedig az
  `activateMain()` / `deactivateMain()`-ben.
- **Mit:** `savedZone` + `savedZoneMagic` (0xFA11A5EE), valamint
  `savedMain` + `savedMainMagic` (0xF0117E55). A magic-ek védik az
  érvényességet a BROWNOUT-törlés ellen.
- **Miért:** BROWNOUT / reset után **azonnal**, biztonságosan visszaáll.
- **Korlát:** **teljes áramtalanításkor elveszik** (az RTC RAM tápigényes).

### NVS (flash, áramtalanítást is túlél)

- **Mikor:** a fokozat csak ha **30 mp-ig stabil** maradt (`NVS_SAVE_STABLE_MS`),
  vagy 5 percenként kényszerítve (`NVS_FORCE_SAVE_MS`); a fő relé állapota pedig ha eltér
  az NVS-ben tárolttól. Mindkettő a `loop()`-ból hívott
  `saveZoneToNvsIfStable()`-ben, **csak ha nem fut OTA**.
- **Miért késleltetve / loop-ból:** hogy **ne írjunk flasht** a brownout-veszélyes
  kapcsolási pillanatban (a be-/kikapcsolási áramlökéskor), és kíméljük a flash-kopást.
- **Cache:** `nvsLastSavedZone` / `nvsLastSavedMain` megakadályozza a fölösleges
  újraírást ugyanazzal az értékkel.
- **Korlát:** lassabb írás (~10–50 ms, blokkoló), ezért nem a kapcsolás előtt fut.

### Visszaállítás boot-kor

A fokozat és a fő relé (görgő/táp) **csak hibás reset után** áll vissza automatikusan:

```
ESP_RST_BROWNOUT | ESP_RST_UNKNOWN | ESP_RST_INT_WDT | ESP_RST_TASK_WDT | ESP_RST_WDT
```

**A fő relé dönt először** (`[FIX-ESP-30]`): a visszaállítás **csak akkor** indítja
újra a görgőt + ventilátort, ha a fő relé a reset előtt **tényleg aktív volt**:

1. ha az RTC main-magic érvényes → `savedMain`,
2. különben ha az NVS-ben van érvényes érték (`fan/main`) → onnan (BROWNOUT-fallback),
3. különben **ismeretlen** → **nem indítunk** semmit (idle marad).

Ha a fő relé **nem volt aktív** (vagy ismeretlen), az eszköz **idle** állapotban
marad — így egy spontán hibás reset **nem indít** váratlanul görgőt/ventilátort.

Ha a fő relé **aktív volt**, a fokozat **RTC-elsőbbséggel** áll vissza (a korábbi
„magasabb zóna" heurisztika helyett — az RTC mindig a legfrissebb, az NVS késik):

1. ha az RTC zóna-magic érvényes és `savedZone` 0–3 → onnan (a friss érték),
2. különben ha az NVS-ben van érvényes érték (0–3) → onnan (BROWNOUT-fallback),
3. különben **fallback `LEVEL:2`** (a görgő járt, ne maradjon 0-n).

> **Szándékos** deep sleep utáni ébredésnél (reset ok = `DEEPSLEEP`) **nincs**
> auto-visszaállítás — tiszta lappal indul, ami a kívánt viselkedés.
>
> **Failsafe után** (beragadt/inkonzisztens relé) szintén **nincs** auto-indítás:
> a failsafe **detektálásakor** (még a `STATE_FAILSAFE` beállítása előtt) minden
> állapot lenullázódik RTC+NVS-ben — így a failsafe melletti hibás reset sem állítja
> vissza a reléket (`zeroStateForFailsafe()`, lásd a Failsafe szakaszt).

---

## Diagnosztikai napló

A firmware egy kis (max **512 byte**, körkörös) naplót vezet a SPIFFS-en
(`/diag.log`), ami BLE-n a `DIAG?` paranccsal lekérdezhető. A naplóba **csak hibák
és diagnosztikai események** kerülnek (a rutin „sikeres/info" bejegyzések — pl.
deep sleep belépés, OTA health-check OK — **nem**):

```
[ver] 7.14.0                                     -> stabil firmware-verzió (sticky, lásd lentebb)
[boot]  CRC32 self-test FAIL -> OTA off. Just serial update!  -> CRC-önteszt bukott → OTA letiltva
[boot]  reason=BROWNOUT(11) heap=... min=...     -> hibás reset (pl. brownout)
[boot]  loop-break idle n=...                    -> túl sok gyors hibás reset → idle marad
[lowmem] heap=... min=... t=...s                 -> kevés szabad memória (<20 kB)
[relay] 1 2 ACTIVE ST zone=...                   -> ≥2 ventilátorrelé egyszerre aktív → failsafe
[relay] 3 STUCK zone=...                          -> beragadt ventilátorrelé (STUCK) → failsafe
[relay] main stuck!                               -> bootkori relé-önteszt: beragadt fő relé → failsafe
[ota]   bad magic=0x.. size=...                  -> rossz/sérült firmware fájl
[ota]   crc retry part=... try=...               -> OTA part CRC-hiba, újraküldés
```

**`[ver]` — sticky firmware-verzió sor:** a napló **első sora** mindig a stabil
firmware-verzió (`[ver] <verzió>`). Akkor íródik, amikor a firmware stabilnak
bizonyul (minden validált bootnál, illetve OTA utáni health-check OK-kor), és
**dedup**-olt (ha már ez az első sor, nem ír újra). A sor **sticky**: sem a
méret-trimmelés, sem a `DIAGCLR` nem törli — csak a hibabejegyzések cserélődnek.

A napló MTU-biztos 20 byte-os darabokban streamel (`DIAG_BEGIN` … `DIAG_END`),
így alapértelmezett BLE MTU mellett is sértetlen.

---

## OTA firmware-frissítés

A frissítés a dedikált OTA BLE szolgáltatáson keresztül történik. Védelem:

- **Per-part CRC32 + újraküldés** (`[FIX-ESP-34]`, 7.9.0): a `0xFC` part-vége
  csomag **4 byte CRC32-t** (zlib-kompatibilis) hordoz a part adataira. A fogadó
  a SPIFFS-írás **előtt** ellenőrzi; eltérésnél **nem ír**, hanem ugyanazt a
  partot kéri újra (`0xF1`), legfeljebb **5×**, utána abort (`0x0F` hibaüzenet +
  `[ota] crc retry/abort` a diag naplóba). A part-feldolgozás **soros**: a
  következő part kérése csak CRC-OK + sikeres írás után megy ki.
  ⚠️ **Nincs visszafelé kompatibilitás**: a régi (CRC nélküli) küldő nem támogatott.
  A hozzá tartozó CRC-s küldő ebben a repóban van: **[`sender/ota.py`](sender/)**.
- **CRC32 önteszt → OTA letiltás** (7.14.0): bootkor a `crc32_zlib` rutin ismert
  vektorral ellenőrződik (`crc32("123456789")==0xCBF43926`), **release buildben is**.
  Ha **bukik** (a fordítás/optimalizálás elrontotta a rutint), az **OTA BLE-szolgáltatás
  el sem indul** (a firmware-ellenőrzés megbízhatatlan lenne) — a hiba a diag naplóba
  kerül (`[boot] CRC32 self-test FAIL -> OTA off. Just serial update!`). Az eszköz
  egyébként **normálisan fut tovább** (ventilátor, diag-lekérdezés). Frissen OTA-zott,
  még `PENDING_VERIFY` firmware-nél az `OTA_ROLLBACK_ON_CRC_FAIL` kapcsoló (alapból `0`)
  választhatóvá teszi a visszagörgetést (`1` → `[boot] CRC FAIL on fresh OTA -> rollback`).
- **Magic-byte ellenőrzés** (`[FIX-ESP-16]`): az `Update.begin()` előtt ellenőrzi,
  hogy a feltöltött bináris első byte-ja **0xE9** (érvényes ESP32 app image).
  Ha nem, **érthető hibát** ad a félrevezető „Decryption error" helyett, és a
  diag naplóba is bekerül (`[ota] bad magic=0x..`).
- **Rollback health-check**: a bootloaderben engedélyezett app-rollback
  (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`) mellett az újonnan feltöltött firmware
  `PENDING_VERIFY` állapotban indul. A firmware **csak ~30 s stabil futás után**
  (`OTA_VERIFY_HEALTHY_MS`), vagy egy kontrollált deep sleep előtt jelöli magát
  érvényesnek. Ha addig boot- vagy futáshiba (panic/watchdog/brownout) miatt
  újraindul, a bootloader **automatikusan visszagörget az előző jó verzióra**.
  (USB-flashelt buildnél az állapot `UNDEFINED`, ekkor a health-check nem aktív.)
  A health-check sikere a **soros logba** kerül, a diag.log-ba **nem** (csak hibák).

> **Tipp:** mindig az alkalmazás `*.ino.bin` fájlját töltsd fel — ne a
> `*.merged.bin`, `*.partitions.bin` vagy gzip-elt (`0x1F`) fájlt. Ellenőrzéshez:
> `python3 ota_diagnostic.py firmware.bin`.

---

## Particionálás

`partitions_custom.csv` (4 MB flash, dual-OTA):

| Név | Típus | Altípus | Offset | Méret |
|---|---|---|---|---|
| `nvs` | data | nvs | 0x9000 | 0x5000 (20 kB) |
| `otadata` | data | ota | 0xE000 | 0x2000 |
| `app0` | app | ota_0 | 0x10000 | 0x150000 (1,3 MB) |
| `app1` | app | ota_1 | 0x160000 | 0x150000 (1,3 MB) |
| `spiffs` | data | spiffs | 0x2B0000 | 0x150000 (1,3 MB) |

Az `nvs` partíció tárolja a fokozatot (`fan/zone`) és a fő relé állapotát
(`fan/main` — görgő + ventilátor táp), a `spiffs` a diag naplót.

---

## Diagnosztikai eszközök (Python)

A repóban három Python eszköz található (részletek: `TOOLS_README.md`).
Telepítés: `pip install bleak`.

| Eszköz | Funkció |
|---|---|
| `diag_client.py` | A `/diag.log` lekérése BLE-n (reset okok, lowmem, sleep). |
| `fan_stress.py` | Fokozat-edzés / stressz-teszt a BROWNOUT reprodukálásához. |
| `ota_diagnostic.py` | A firmware `.bin` magic-byte és partíció-ellenőrzése. |

Példák:

```bash
python3 diag_client.py --pin 123456            # napló lekérése
python3 diag_client.py --clear                 # lekérés + törlés
python3 fan_stress.py --duration 3600 --check-interval 60 --log stress.log
python3 ota_diagnostic.py FanController_OTA_debug.ino.bin
```

---

## Fordítás (build)

A projekt **Seeed XIAO ESP32-C3** és **ESP32-C6** boardra fordul, **ESP32 Arduino
core 3.1.3**-mal és a **OneButton** könyvtárral, a `partitions_custom.csv` custom
partícióval. A pinkiosztást a firmware a cél-chip szerint automatikusan választja.

**Claude Code on the web:** a toolchaint a `.claude/hooks/session-start.sh`
SessionStart hook automatikusan telepíti minden munkamenet indulásakor
(arduino-cli + esp32 core + OneButton + ctags + partíció). Fordítás:

```bash
./build.sh                 # fordítás C3-ra (alapértelmezett)
TARGET=c6 ./build.sh       # fordítás C6-ra (XIAO_ESP32C6)
./build.sh --clean         # tiszta build
```

**Manuálisan (arduino-cli):**

```bash
arduino-cli core install esp32:esp32@3.1.3
arduino-cli lib install OneButton          # vagy GitHubról, ha a registry nem elérhető
./build.sh                 # vagy: TARGET=c6 ./build.sh
```

> **Soros (Serial) napló:** a firmwarenek három, egymástól független debug-csatornája
> van a forrás elején: `DEBUG` (ventilátorvezérlő), `OTA_DEBUG` (OTA-átvitel) és
> `BOOT_DIAG` (boot-diagnosztika). A `Serial.begin(115200)` **csak akkor** fut le, ha
> **legalább az egyik** be van kapcsolva (`SERIAL_ENABLED`). Ha mindhárom `0`, a Serial
> el sem indul és semmilyen kiírás nincs (a debug-mentes build ~11 KB-tal kisebb).
> Alapból `DEBUG=1`, `OTA_DEBUG=0`, `BOOT_DIAG=1`.
>
> A build a `build.partitions=partitions_custom` és `upload.maximum_size=1376256`
> (az `app0` mérete, 0x150000) build-property-kkel fordít. Méret: **C3 ≈ 83%**
> (≈1.15 MB), **C6 ≈ 89%** (≈1.23 MB) az 1.375 MB-os app partícióból (a C6 a
> bővebb rádió-stack — Wi‑Fi 6 / BLE 5 / 802.15.4 — miatt nagyobb). Feltöltéshez a
> XIAO bootloader-gomb + soros port kell (a webes környezet csak fordít, nem flashel).

---

## Időzítések összefoglaló

| Paraméter | Érték | Szerep |
|---|---|---|
| `checkInterval` | 20 ms | állapotgép ütem |
| `RELAY_SWITCH_DELAY_MS` | 10 ms | break-before-make szünet |
| `RELAY_TEST_ON_MS` / `RELAY_TEST_GAP_MS` | 120 / 60 ms | bootkori relé-önteszt: relé ON / szünet |
| `NVS_SAVE_STABLE_MS` | 30 000 ms | NVS-írás előtti stabilitási idő |
| `INACTIVITY_MS` | 3 600 000 ms (1 óra) | tétlenségi deep sleep |
| `BLE_ZONE_TIMEOUT_MS` | 720 000 ms (12 perc) | BLE-kapcsolat nélküli fokozat-időkorlát |
| `FAILSAFE_TIMEOUT_MS` | 10 000 ms | failsafe → deep sleep |
| `MAX_AUTH_ATTEMPTS` / lockout | 5 / 60 000 ms | BLE auth védelem |
| `LOW_HEAP_THRESHOLD` | 20 000 byte | lowmem napló küszöb |

---

## Verziótörténet (kivonat)

A teljes, részletes változás-napló ([MOD-x] / [FIX-ESP-x] bejegyzésekkel):
**[verhistory.md](verhistory.md)**.

| Verzió | Változás |
| --- | --- |
| **7.14.0** | **`RELAY_ROLLER` → `RELAY_MAIN`** átnevezés: a fő relé a **görgőt ÉS a ventilátor tápját** kapcsolja (a hozzá tartozó belső nevek `main`-re: `mainActive`, `savedMain`, NVS-kulcs `fan/main` stb.; a BLE `ROLLER:` wire-parancs marad). **Bootkori relé-önteszt** (`RELAY_TEST_AT_BOOT`): SW-reset/gombébresztéskor a fan-reléket sorban kapcsolja (fő relé OFF), és a bontón mért AC-ból **beragadt fő relét** detektál → `[relay] main stuck!` + failsafe. **CRC32 önteszt → OTA letiltás** FAIL esetén (release-ben is) + `OTA_ROLLBACK_ON_CRC_FAIL` kapcsoló. **diag.log: csak hibák** + sticky `[ver]` verziósor (trim/`DIAGCLR` megőrzi) + `[relay]` cimkék; a `[sleep]`/health-check-OK info-sorok kivéve. **Fan-sense konzisztencia** a fő relével: fő relé OFF alatt nincs AC-referencia → a mismatch-figyelés kilép (nincs téves STUCK/NOAC), `activateMain` grace-t állít, `deactivateMain` a fan-reléket+zónát nullázza; a fokozatváltás fő relé nélkül is végrehajtódik. **Aktivitás-definíció**: fő relé be ÉS megy a ventilátor (auto módban BLE-vel, manuál módban anélkül). |
| **7.13.0** | **AC-érzékelés a relé bontó (NC) érintkezőjén** (a soros fan-tekercsek miatt a kimenet-figyelés nem tudta megkülönböztetni a fokozatokat) → bekötés-leképezés a `FAN_SENSE_AC_MEANS_ENGAGED` makróval (alapból **`0`**); a detektálás **LOW-alapú** (opto-vezetés), így az opto-kimeneti RC-szűrő kiesése **sem** ad téves STUCK-ot. A STUCK/NOAC failsafe-logika változatlanul helyes. **Soros kimenet egységesítése**: `Serial.begin` (és `flush`) csak ha `DEBUG`/`OTA_DEBUG`/`BOOT_DIAG` valamelyike aktív; minden debugon kívüli `Serial.print` kapuzott makróra cserélve. Megjegyzések egysorosra húzva. |
| **7.11.1** | Brownout/reset-**hurok-megszakító** (RTC-számláló): rövid időn belül ismétlődő hibás reset → a boot nem állítja vissza a görgőt/ventit, idle marad (30 s stabil futás után nullázódik). Relék **azonnali tiltása** a `setup()` legelején (C6 GPIO17 boot-felhúzás ellen). Főleg a C6 „visszaállítás után furán működik / nincs serial" tünetre. |
| **7.11.0** | RAM-optimalizálás: a kettős, statikus 2×16 KB OTA-buffer helyett **egy, dinamikusan allokált** buffer (csak OTA alatt). Statikus RAM **−32 KB** (globálisok 72→39 KB, szabad RAM ~255→~288 KB). Működés bitre azonos; malloc-hiba esetén az OTA érthető hibával nem indul. |
| **7.10.1** | XIAO ESP32-C6: bootkor a **külső antenna** kiválasztása (RF-switch: `WIFI_ENABLE` GPIO3 LOW + `WIFI_ANT_CONFIG` GPIO14 HIGH), a rádió indítása előtt. Csak C6-on fordul bele; C3 változatlan. |
| **7.10.0** | **XIAO ESP32-C6 támogatás**: a pinkiosztás cél-chip szerint feltételes (`CONFIG_IDF_TARGET_ESP32C6` → C6, egyébként C3). A `build.sh` `TARGET=c3/c6`-tal fordít. C6 GPIO-k: FAN 23/22/21, ROLLER 2, EN 17, BUTTON 1, LED 0/16, SENSE 19/20/18. |
| **7.9.1** | OTA-indítás determinisztikus: a `0xFF`-re a fogadó azonnal `0xF1 0`-t kér (a régi `0xAA`-handshake helyett) → megszűnik a „stuck part 0" verseny. Csonka `0xFC` (<9 byte) → part-újrakérés a csendes eldobás helyett. Közös `otaAbort()` helper. |
| **7.9.0** | OTA per-part **CRC32 + újraküldés**: a `0xFC` 4 byte zlib-CRC32-t hordoz, a fogadó a SPIFFS-írás előtt ellenőrzi, hibánál ugyanazt a partot újrakéri (max 5×, utána abort + diag.log). Soros part-feldolgozás (a kettős-buffer versenyhibák kiváltva), CRC32 boot-önteszt. Régi (CRC nélküli) küldő nem támogatott. |
| **7.8.6** | A failsafe-állapot nullázása (RTC+NVS+logikai) közös `zeroStateForFailsafe()` helperbe került, és már a failsafe **detektálásakor** lefut (a `STATE_FAILSAFE` beállítása előtt). Megszűnik a detektálás és a `failSafeMode()` első lefutása közti időablak → failsafe melletti hibás reset sem állítja vissza a reléket. |
| **7.8.5** | `FAN_SENSE` bekapcsolva (`FAN_SENSE_ENABLE` 0→1): a 3× H11AA1M opto figyeli a relé-kimeneteken a 230V AC-t. STUCK → szinkron `diag.log` + azonnali failsafe; NOAC → egyszeri figyelmeztetés + `diag.log` (failsafe nélkül). |
| **7.8.4** | A relé-kimenet ellenőrzések (2+ relé LOW GPIO-visszaolvasás; FAN_SENSE esetén a 230V AC eltérés) a `normalMode()`-ban a fokozatváltás **után** futnak, hogy a frissen beállított relé-állapotot értékeljék. Failsafe-logika és küszöbök változatlanok. |
| **7.8.3** | Failsafe belépéskor a görgő + fokozat állapota minden tárolóban (logikai + RTC + NVS) lenullázódik — failsafe közbeni hibás reset (akár BROWNOUT) **sem** indítja újra a görgőt/ventilátort. |
| **7.8.2** | Boot-helyreállítás: zóna **RTC-elsőbbséggel** (a „magasabb zóna" heurisztika helyett); a **görgő állapota is perzisztens** (RTC+NVS), és csak akkor áll vissza, ha tényleg aktív volt → nincs idle-ből váratlan indítás; `saveZoneToNvsIfStable()` kritikus szekciós zóna-olvasás. |
| 7.8.1 | Aszimmetrikus reakció a kimenet-figyelésben: STUCK → azonnali failsafe; NOAC → csak egyszeri figyelmeztetés + `diag.log`, failsafe nélkül. |
| **7.8.0** | Fan relé KIMENET figyelése 3× H11AA1M optóval (GPIO6/7/20), AC-tudatos idő-ablakos detektálással; tartós eltérésnél failsafe + `diag.log`. |
| 7.7.3 | Boot-diagnosztika a soros monitorra (RTC/NVS/`diag.log`). |
| 7.7.2 | NVS force-mentés 5 percenként sűrű váltogatásnál is. |
| 7.7.1 | Boot NVS olvasás default −1 (a fallback javítása). |
| **7.7.0** | Interrupt cleanup visszatéve a deep sleep elé (INT_WDT ellen). |
| 7.6.9 | Hosszabb türelmi szünetek az `enterDeepSleep()`-ben (INT_WDT ellen). |
| 7.6.8 | WDT resetek (INT/TASK/WDT) is a görgő + fokozat visszaállításban. |
| 7.6.7 | Hibrid fokozat-mentés: RTC (resetre) + NVS (áramtalanításra). |
| 7.6.6 | BROWNOUT/UNKNOWN reset után görgő + fokozat auto-visszaállítás. |
| 7.6.5 | RELAY_SWITCH_DELAY_MS → 10 ms (rövidebb táp-tranziens). |
| 7.6.4 | OTA magic-byte ellenőrzés a félrevezető „Decryption error" helyett. |
| 7.6.3 | `enterDeepSleep()` forrásának naplózása (`[sleep] src=…`). |
| 7.6.0 | SPIFFS diag napló, BLE-n lekérdezhető (reset ok + lowmem). |
| 7.5.0 | BROWNOUT reset után deep sleep helyett újraindulás. |

---

## Licenc

Lásd a [`LICENSE`](LICENSE) fájlt.
