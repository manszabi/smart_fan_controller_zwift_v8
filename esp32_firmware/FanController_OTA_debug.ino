// FanController_OTA_debug — XIAO ESP32-C3/C6 ventilator+gorgo vezerlo (BLE+OTA). Valtozasok: verhistory.md
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <OneButton.h>
#include "esp_sleep.h"
#include "esp_task_wdt.h"
#include "driver/gpio.h"  // [FIX-ESP-55] pad-hold: kimenetek rögzítése deep sleep alatt
#include <Update.h>
#include "FS.h"
#include "LittleFS.h"  // [FIX-ESP-65] SPIFFS helyett (áramszünet-biztos, telített FS-en is gyors)
#include "esp_ota_ops.h"
#include "esp_system.h"
#include <Preferences.h>  // [FIX-ESP-21] NVS fokozat-mentés áramtalanításra

// ===================== DEBUG CONFIG =====================
#define DEBUG 0
#define OTA_DEBUG 0
#define BOOT_DIAG 0
#define FAN_SENSE_ENABLE 1

// A Serial-t csak akkor inicializáljuk, ha valamelyik kimeneti csatorna aktív
#if DEBUG || OTA_DEBUG || BOOT_DIAG
#define SERIAL_ENABLED 1
#else
#define SERIAL_ENABLED 0
#endif

// CRC-FAIL utáni frissen OTA-zott firmware: 0=fut tovább OTA nélkül, 1=rollback+reboot
#define OTA_ROLLBACK_ON_CRC_FAIL 0

// [FIX-ESP-55] Relé-kimenetek pad-hold-ja deep sleep alatt: 0=ki, 1=be
// (alvás alatt a lábak különben nagyimpedanciásra váltanak — lásd a DEEP SLEEP PAD HOLD blokkot)
#define DEEP_SLEEP_PAD_HOLD 1

// Bootkori ventilátorrelé-önteszt (RELAY_MAIN nélkül): 0=ki, 1=be
#define RELAY_TEST_AT_BOOT 1
#define RELAY_TEST_ON_MS 200        // egy relé bekapcsolva-tartása (ms)
#define RELAY_TEST_GAP_MS 200       // szünet két relé között (ms)
#define RELAY_TEST_WINDOW_MS 15000  // [FIX-ESP-59] ennyin belül futhat le boot után; utána elévül

// Ventilátorvezérlő debug: _P/_V = print (literál/érték), sima/_VLN = println (literál/érték)
#if DEBUG
#define DBG(x) Serial.println(F(x))
#define DBG_P(x) Serial.print(F(x))
#define DBG_V(...) Serial.print(__VA_ARGS__)
#define DBG_VLN(...) Serial.println(__VA_ARGS__)
#else
#define DBG(x)
#define DBG_P(x)
#define DBG_V(...)
#define DBG_VLN(...)
#endif

// OTA debug: ugyanaz a séma, külön kapcsolóval
#if OTA_DEBUG
#define OTA_DBG(x) Serial.println(F(x))
#define OTA_DBG_P(x) Serial.print(F(x))
#define OTA_DBG_V(...) Serial.print(__VA_ARGS__)
#define OTA_DBG_VLN(...) Serial.println(__VA_ARGS__)
#else
#define OTA_DBG(x)
#define OTA_DBG_P(x)
#define OTA_DBG_V(...)
#define OTA_DBG_VLN(...)
#endif

// ===================== VERSION INFO =====================
#define FIRMWARE_VERSION "7.18.0"
#define FIRMWARE_DATE "2026-09-02"

// ===================== PINS =====================
#if defined(CONFIG_IDF_TARGET_ESP32C6)
#define RELAY_FAN1 23
#define RELAY_FAN2 22
#define RELAY_FAN3 21
#define RELAY_MAIN 2  // roller + ventilátor táp
#define RELAY_EN 17
#define BUTTON_PIN 1
#define LED_YELLOW 0
#define LED_RED 16
#define RF_SWITCH_EN 3  // RF-kapcsoló engedélyezés (XIAO C6: WIFI_ENABLE), aktív LOW
#define ANT_SELECT 14   // antenna választó (XIAO C6: WIFI_ANT_CONFIG): HIGH=külső, LOW=belső
#else
#define RELAY_FAN1 10
#define RELAY_FAN2 9
#define RELAY_FAN3 8
#define RELAY_MAIN 2  // roller + ventilátor táp
#define RELAY_EN 21
#define BUTTON_PIN 3
#define LED_YELLOW 5
#define LED_RED 4
#endif

// FAN relé bontó-érintkező (NC) figyelés H11AA1M-mel: AC ⇒ LOW; az AC-t a LOW mintából detektáljuk
#if FAN_SENSE_ENABLE
#if defined(CONFIG_IDF_TARGET_ESP32C6)
#define FAN1_SENSE_PIN 19  // D? — Fan1 (RELAY_FAN1) bontó (NC) érintkezőjének figyelése
#define FAN2_SENSE_PIN 20  // D? — Fan2 (RELAY_FAN2) bontó (NC) érintkezőjének figyelése
#define FAN3_SENSE_PIN 18  // D? — Fan3 (RELAY_FAN3) bontó (NC) érintkezőjének figyelése
#else
#define FAN1_SENSE_PIN 6   // D4 — Fan1 (RELAY_FAN1) bontó (NC) érintkezőjének figyelése
#define FAN2_SENSE_PIN 7   // D5 — Fan2 (RELAY_FAN2) bontó (NC) érintkezőjének figyelése
#define FAN3_SENSE_PIN 20  // D7 — Fan3 (RELAY_FAN3) bontó (NC) érintkezőjének figyelése
#endif
// AC jelentése a sense-ágon: 0=NC bekötés→AC⇒relé NINCS behúzva (jelen HW), 1=NO→AC⇒behúzva
#define FAN_SENSE_AC_MEANS_ENGAGED 0

const uint8_t fanSensePins[3] = { FAN1_SENSE_PIN, FAN2_SENSE_PIN, FAN3_SENSE_PIN };

const unsigned long AC_SENSE_WINDOW_MS = 40;              // > 1 hálózati periódus (20 ms): a nullátmeneti HIGH-tüske ne látsszon "nincs AC"-nak
const unsigned long AC_SENSE_DEBOUNCE_MS = 80;            // relé-kapcsolás/perdülés kiszűrése a fanRelayEngaged átbillenése előtt
const unsigned long FAN_SENSE_GRACE_MS = 300;             // kapcsolás utáni türelmi idő (~2× a ~150 ms sense-beállásra)
const unsigned long FAN_SENSE_MISMATCH_CONFIRM_MS = 300;  // NOAC megerősítés a grace UTÁN (a relé-átmenetet a grace fedi)
#define FAN_SENSE_FAILSAFE_ON_STUCK 1                     // STUCK → STATE_FAILSAFE (azonnal, türelmi idő után)
#define FAN_SENSE_WARN_ON_NOAC 1                          // NOAC  → figyelmeztetés + diag.log (failsafe NÉLKÜL)

unsigned long fanSenseLastLow[3] = { 0, 0, 0 };      // utolsó LOW (AC-vezetés) minta ideje (ms)
bool fanRelayEngaged[3] = { false, false, false };   // SZŰRT állapot: TRUE = az adott relé behúzva (NC nyitva, a fokozat aktív)
unsigned long fanSenseChangeSince[3] = { 0, 0, 0 };  // mióta tér el a nyers a szűrttől (debounce)
bool fanSenseSeen[3] = { false, false, false };      // láttunk-e már valaha LOW (AC) mintát
unsigned long fanSenseGraceUntil = 0;                // eddig nem értékelünk eltérést
unsigned long fanMismatchSince[3] = { 0, 0, 0 };     // NOAC: mióta áll fenn az eltérés (0 = nincs)
bool fanNoacWarned[3] = { false, false, false };     // NOAC: figyelmeztettünk-e már (ne spammeljen)
#endif                                               // FAN_SENSE_ENABLE

// ===================== FS / OTA DEFINES =====================
// [FIX-ESP-65] A fájlrendszer a `FLASH` makrón keresztül érhető el az egész forrásban;
// a SPIFFS → LittleFS váltás így ténylegesen ez az egy sor. A LittleFS alapértelmezett
// partíció-címkéje `"spiffs"`, ezért a `partitions_custom.csv` változatlan marad.
#define FLASH LittleFS
#define FORMAT_FS_IF_FAILED true

#define OTA_NORMAL_MODE 0
#define OTA_UPDATE_MODE 1
#define OTA_INSTALL_MODE 2

// ===================== OTA WIRE-PROTOKOLL =====================
// Az opkódok eddig csak nyers hex-literálként szerepeltek a kódban; a protokoll leírása
// kizárólag a `sender/ota.py`-ban élt. Ugyanazok az értékek, néven nevezve.
// Kliens → eszköz (OTA RX karakterisztika, WRITE / WRITE_NR):
static const uint8_t OTA_RX_PART_CHUNK = 0xFB;  // [0xFB][darab-index][adat…] — part-darab a pufferbe
static const uint8_t OTA_RX_PART_END = 0xFC;    // [0xFC][hossz_hi][hossz_lo][part_hi][part_lo][CRC32 4B]
static const uint8_t OTA_RX_FILE_DEL = 0xFD;    // [0xFD] — /update.bin törlése
static const uint8_t OTA_RX_TOTAL_SIZE = 0xFE;  // [0xFE][össz-méret 4B]
static const uint8_t OTA_RX_BEGIN = 0xFF;       // [0xFF][partok_hi][partok_lo][MTU_hi][MTU_lo]
static const uint8_t OTA_RX_FS_FORMAT = 0xEF;   // [0xEF] — fájlrendszer formázása
// Eszköz → kliens (OTA TX karakterisztika, NOTIFY):
static const uint8_t OTA_TX_RESULT = 0x0F;    // [0x0F]"szöveg" — eredmény vagy hibaüzenet
static const uint8_t OTA_TX_REQ_PART = 0xF1;  // [0xF1][part_hi][part_lo] — ezt a partot kérem
static const uint8_t OTA_TX_COMPLETE = 0xF2;  // [0xF2][partok_hi][partok_lo] — átvitel kész
static const uint8_t OTA_TX_FS_INFO = 0xEF;   // [0xEF][total 3B][used 3B] — fájlrendszer-méret

static const size_t OTA_BUF_SIZE = 16384;  // OTA part-puffer (16 KB): átviteli sebesség vs. RAM egyensúly, csak OTA alatt foglalt
static uint8_t* otaBuf = nullptr;

// ===================== DIAG LOG (FIX-ESP-14) =====================
#define DIAG_LOG_PATH "/diag.log"
const size_t DIAG_LOG_MAX = 512;               // napló max. mérete: kicsi a flash-hely/kopás miatt (körkörös, [ver] sticky)
const uint32_t LOW_HEAP_THRESHOLD = 20000;     // ~20 kB szabad heap alatt "kevés memória" bejegyzés (BLE/OTA tartalék)
const size_t DIAG_CHUNK_SIZE = 20;             // = alap BLE MTU (23) − 3 ATT overhead → fragmentálás nélkül átmegy
const unsigned long DIAG_CHUNK_INTERVAL = 25;  // ms két csomag között (BLE flow control)

#define OTA_SERVICE_UUID "fb1e4001-54ae-4a28-9f74-dfccb248601d"
#define OTA_CHARACTERISTIC_UUID_RX "fb1e4002-54ae-4a28-9f74-dfccb248601d"
#define OTA_CHARACTERISTIC_UUID_TX "fb1e4003-54ae-4a28-9f74-dfccb248601d"

static BLECharacteristic* pOtaTx = nullptr;
static BLECharacteristic* pOtaRx = nullptr;

static bool otaDeviceConnected = false;                 // BLE OTA-kliens csatlakozva
static bool otaSendSize = true;                         // küldjük-e a flash-méretet a kliensnek
static bool otaWriteFile = false;                       // van-e CRC-OK, kiírásra váró part
static int otaWriteLen = 0;                             // [FIX-ESP-38] az aktuális part hossza (egy buffer)
static int otaParts = 0, otaCur = 0, otaMTU = 0;        // összes part / aktuális part / part-méret
static int otaMode = OTA_NORMAL_MODE;                   // OTA állapotgép: NORMAL / UPDATE / INSTALL
static bool otaCrcOk = true;                            // CRC32 önteszt eredménye; FAIL esetén az OTA letiltva
unsigned long otaReceivedBytes = 0, otaTotalBytes = 0;  // eddig kiírt / várt összes byte
unsigned long otaLedTimer = 0;                          // OTA-villogás időzítő
bool otaLedState = false;                               // OTA-villogás LED állapot

static uint32_t otaExpectedCrc = 0;   // a 0xFC-ben kapott elvárt CRC32
static int otaPartRetry = 0;          // aktuális part újraküldés-számláló
static const int MAX_PART_RETRY = 5;  // ennyi sikertelen CRC után abort
static int otaExpectedPart = 0;

bool otaPendingReboot = false;
unsigned long otaRebootAt = 0;

// [OTA health-check] true: frissen OTA-zott, még meg nem erősített (PENDING_VERIFY) firmware fut
bool otaPendingVerify = false;
const unsigned long OTA_VERIFY_HEALTHY_MS = 30000;  // OTA health-check: ennyi stabil futás után validál

bool otaInstallWaiting = false;
unsigned long otaInstallWaitUntil = 0;

volatile bool diagRequested = false;       // DIAG? parancs érkezett
volatile bool diagClearRequested = false;  // DIAGCLR parancs érkezett
bool diagStreaming = false;                // épp streamelünk-e
File diagFile;                             // nyitott naplófájl streamelés alatt
unsigned long diagLastChunk = 0;           // utolsó csomag ideje

// ===================== FAN / BLE STRUCTS =====================
portMUX_TYPE zoneMux = portMUX_INITIALIZER_UNLOCKED;

struct BleCommand {
  bool hasCommand;
  int zone;
  bool hasMainCommand;
  int mainCommand;
};

enum SystemState {
  STATE_NORMAL,
  STATE_FAILSAFE
};

SystemState currentState = STATE_NORMAL;  // fő állapotgép: NORMAL / FAILSAFE

unsigned long lastCheck = 0;
const unsigned long checkInterval = 20;  // állapotgép-lépés periódusa, ~50 Hz: gyors reakció, de kíméli a CPU-t/BLE-t
unsigned long lastBlink = 0;
const unsigned long blinkInterval = 100;  // failsafe LED-villogás fél-periódus (~5 Hz, jól látható riasztás)
bool blinkState = false;
unsigned long failStart = 0;  // failsafe belépés ideje (timeout-hoz)
bool failStartSet = false;

volatile BleCommand bleCmd = { false, 0, false, 0 };
portMUX_TYPE bleCmdMux = portMUX_INITIALIZER_UNLOCKED;

// ===================== TIMERS =====================
const unsigned long INACTIVITY_MS = 3600000;      // 1 óra tétlenség → deep sleep (edzéshossz felső becslése)
const unsigned long RELAY_SWITCH_DELAY_MS = 10;   // break-before-make szünet (tényleges ~20 ms a checkInterval miatt)
const unsigned long LED_BLINK_INTERVAL = 500;     // normál státusz-LED villogás (~1 Hz)
const unsigned long HEARTBEAT_INTERVAL = 2000;    // életjel-pulzus periódusa
const unsigned long HEARTBEAT_PULSE = 100;        // életjel-pulzus hossza
const unsigned long BLE_RESTART_DELAY = 500;      // BLE-stack stabilizálódása újraindítás előtt
const unsigned long FAILSAFE_TIMEOUT_MS = 10000;  // failsafe-ben ennyi LED-villogás után deep sleep (elég a hiba jelzésére)

volatile bool zoneChanging = false;
volatile unsigned long bleDisconnectTime = 0;
const unsigned long BLE_ZONE_TIMEOUT_MS = 720000;  // BLE elszállás után 12 perccel mindent lekapcsol, ha zóna aktív (biztonsági)

// ===================== FAN BLE UUIDs =====================
#define SERVICE_UUID "0000ffe0-0000-1000-8000-00805f9b34fb"
#define CHARACTERISTIC_UUID "0000ffe1-0000-1000-8000-00805f9b34fb"

// ===================== BLE AUTH =====================
#define BLE_AUTH_PIN "123456"
// [MOD-24] A PIN hossza fordítási időben ismert (string literál). Eddig minden egyes
// BLE-parancs egy `String correctPin = BLE_AUTH_PIN;` heap-allokációval indult, csak
// azért, hogy a .length() > 0 eldőljön. (A korábbi `#if !defined(BLE_AUTH_PIN)` őr
// halott volt: a makró egy sorral fentebb mindig definiált — az ÜRES PIN-t akarta
// elkapni, amit valójában a setup() static_assert-je ellenőriz.)
static constexpr bool BLE_AUTH_REQUIRED = (sizeof(BLE_AUTH_PIN) > 1);
#define MAX_AUTH_ATTEMPTS 5         // ennyi hibás PIN után zárolás (brute-force ellen)
#define AUTH_LOCKOUT_TIME_MS 60000  // 60 s zárolás a hibás kísérletek után

bool isAuthenticated = false;
int authAttempts = 0;
unsigned long lockoutStart = 0;

// ===================== BLE ÁLLAPOT =====================
BLEServer* pServer = NULL;
BLECharacteristic* pCharacteristic = NULL;
volatile bool bleConnected = false;
volatile bool bleEnabled = true;
volatile bool bleNeedsRestart = false;
volatile unsigned long bleRestartTime = 0;

OneButton button(BUTTON_PIN, true, true);

// ===================== FAN / RELÉ / ZÓNA ÁLLAPOT =====================
int currentZone = 0;                // aktív ventilátor fokozat (0=ki, 1..3)
int manualZoneIndex = 0;            // kézi módban a léptetett fokozat (dupla kattintás)
bool manualMode = false;            // kézi (gombos) mód, BLE nélkül
bool mainActive = false;            // RELAY_MAIN (roller + ventilátor táp) aktív
bool relaysEnabled = false;         // tápengedély (RELAY_EN) be
bool zoneChangeInProgress = false;  // folyamatban lévő break-before-make fokozatváltás
unsigned long zoneChangeStart = 0;  // a váltás indításának ideje (ms)
int pendingZone = 0;                // a váltás célfokozata (handleZoneChange élesíti)
#if RELAY_TEST_AT_BOOT
bool relayTestPending = false;  // relé-önteszt esedékes (loopban, BLE kapcsolat előtt)
#endif

// ===================== AKTIVITÁS / BOOT =====================
volatile unsigned long lastActivityTime = 0;
bool wasActive = false;
esp_reset_reason_t lastBootResetReason = ESP_RST_UNKNOWN;  // [FIX-ESP-19] boot reset-ok mentése

// ===================== LED / HEARTBEAT =====================
unsigned long lastRedToggle = 0;
unsigned long lastYellowToggle = 0;
bool redLedState = false;
bool yellowLedState = false;
unsigned long lastHeartbeat = 0;
unsigned long lastHeartbeat_red = 0;
bool heartbeatPulse = false;
bool heartbeatPulse_red = false;

// ===================== SOROS STÁTUSZ-KIÍRÁS =====================
const unsigned long printInterval = 30000;  // státusz-kiírás periódusa a soros logba (ne spammeljen)

RTC_NOINIT_ATTR uint32_t savedZoneMagic;
RTC_NOINIT_ATTR int savedZone;
#define SAVED_ZONE_MAGIC 0xFA11A5EE

RTC_NOINIT_ATTR uint32_t savedMainMagic;
RTC_NOINIT_ATTR int savedMain;  // 1 = aktív volt, 0 = nem
#define SAVED_MAIN_MAGIC 0xF0117E55

// [FIX-ESP-39] Hibás-reset hurok-megszakító: gyors ismétlődő hibás resetnél a boot nem állít vissza → megszakad a brownout-hurok
RTC_NOINIT_ATTR uint32_t errRestoreMagic;
RTC_NOINIT_ATTR int errRestoreCount;  // egymást követő gyors hibás resetek száma (RTC)
#define ERR_RESTORE_MAGIC 0x10075EED
const int MAX_ERR_RESTORE = 3;                     // ennyiedik egymást követőnél már idle
const unsigned long ERR_RESTORE_CLEAR_MS = 30000;  // ennyi stabil futás után nullázzuk
bool errRestoreCleared = false;

Preferences fanPrefs;
int nvsLastSavedZone = -1;                       // amit utoljára NVS-be írtunk (cache, hogy ne írjunk feleslegesen)
int nvsLastSavedMain = -1;                       // [FIX-ESP-30] görgő NVS cache (-1 = nincs mentve)
unsigned long zoneStableSince = 0;               // mikortól stabil a jelenlegi fokozat
bool nvsZonePending = false;                     // van-e még nem mentett stabil fokozat
const unsigned long NVS_SAVE_STABLE_MS = 30000;  // 30 mp stabilitás után mentünk
unsigned long lastNvsSaveTime = 0;               // mikor írtunk utoljára NVS-be
const unsigned long NVS_FORCE_SAVE_MS = 300000;  // 5 perc → kényszerített mentés

// ===================== BYPASS MODE (5 short press) =====================
bool relaySenseBypass = false;  // új üzemmód flag
Preferences bypassPrefs;        // NVS tároló a bypass módhoz

// ===================== COMMAND SOURCE PRIORITY =====================
enum CommandSource {
  SRC_NONE = 0,
  SRC_BLE = 1,
  SRC_BUTTON = 2,
};

CommandSource activeSource = SRC_NONE;
unsigned long sourceLockedUntil = 0;
const unsigned long SOURCE_LOCK_MS = 2000;  // forrás-prioritás zárolás: egy parancsforrást ennyi ideig nem írhat felül alacsonyabb prioritású (BLE vs gomb)

struct Timer {
  unsigned long last = 0;
  unsigned long interval = 0;

  bool elapsed(unsigned long now) {
    if ((unsigned long)(now - last) >= interval) {
      last = now;
      return true;
    }
    return false;
  }
};

// ===================== FORWARD DECLARATIONS =====================
void setFanZone(int zone, CommandSource source = SRC_NONE);
void activateMain();
void deactivateMain();
void enableRelays();
void disableRelays();
#if RELAY_TEST_AT_BOOT
void relayBootTest();
#endif
void handleLEDs(unsigned long currentMillis);
void enterDeepSleep(const char* reason);
void handleClick();
void handleLongPressStop();
void handleDoubleClick();
void handleMultiClick();
void handleZoneChange();
void handleBleCommand();
void stateMachineStep();
void normalMode();
void saveZoneToNvsIfStable();  // [FIX-ESP-21]
void zeroStateForFailsafe();   // [FIX-ESP-33] failsafe-állapot perzisztens nullázása
void zeroStateForBypass();     // bypass módhoz
void persistRelayStateOff();   // [FIX-ESP-56] NVS: "görgő/fokozat volt-e aktív" nullázása
#if FAN_SENSE_ENABLE
void monitorFanRelays();       // [FIX-ESP-29] H11AA1M kimenet-mintavétel + szűrés
void checkFanRelayMismatch();  // [FIX-ESP-29] elvárt vs. mért → failsafe
#endif
void failSafeMode();
void ota_boot_flow();
void otaInitService(BLEServer* server);
void otaLoop();
void diagLog(const char* line);
void handleDiagRequest();
void printBootDiag();  // [FIX-ESP-28]
// ===================== DEEP SLEEP PAD HOLD ([FIX-ESP-55]) =====================
// Deep sleepben a digitális IO tápdomain lekapcsol: a GPIO-k nagyimpedanciásra
// (lebegőre) váltanak, és az alvás TELJES ideje alatt lebegve maradnak. A relé-
// vezérlés aktív-LOW, a tápengedély (RELAY_EN) aktív-HIGH, ezért lebegő lábon a
// panel 10 kΩ-os fel-/lehúzói az egyetlen védelem: egy kis szivárgó áram, kapacitív
// átkötés vagy zaj már behúzhatja a görgő reléjét (RELAY_MAIN). Ilyenkor az ESP
// "alszik", a relé mégis meghúzva marad — ébredéskor pedig a bootteszt jogosan
// beragadt MAIN-t jelez → failsafe → az ESP leáll.
//
// A pad-hold latch az always-on tápdomainben van, ezért az elalvás pillanatában
// AKTÍVAN HAJTOTT szintet (RELAY_EN=LOW, minden relé=HIGH) az alvás alatt is tartja.
//   * C6: van láb-szintű deep sleep hold (SOC_GPIO_SUPPORT_HOLD_SINGLE_IO_IN_DSLP),
//     és a RELAY_MAIN (GPIO2) RTC(LP)-láb → a hold az ébredés UTÁN, a bootloader
//     alatt is él, a setup() oldja fel (ezért kötelező a feloldás!).
//   * C3: nincs láb-szintű deep sleep hold — az IDF doksi szerint a `gpio_hold_en()`
//     digitális lábon alvás alatt NEM tart, ezért kell MELLÉ a globális
//     `gpio_deep_sleep_hold_en()` is; ez ébredéskor magától felold.
// A hold csak a KIMENETET rögzíti; a bemeneti út él, így a gombos GPIO-ébresztés
// (RTC_CNTL_GPIO_WAKEUP_REG, a pad bemenetéről) változatlanul működik.
#if DEEP_SLEEP_PAD_HOLD

// Kell-e a globális deep sleep hold a láb-szintű mellé? A `gpio_deep_sleep_hold_en()`
// DEKLARÁCIÓS feltétele core-verziónként eltér, ezért mindkettőt lefedjük:
//   IDF 5.3 (core 3.1.x):  SOC_GPIO_SUPPORT_HOLD_IO_IN_DSLP && !SOC_..._SINGLE_...
//   IDF 5.5 (core 3.3.x):  !SOC_..._SINGLE_...   (a HOLD_IO_IN_DSLP cap megszűnt)
// Ha csak az egyiket néznénk, a másik core-on a hívás NÉMÁN kimaradna (C3-on a
// rögzítés nem lépne életbe) vagy fordítási hibát adna.
#if defined(SOC_GPIO_SUPPORT_HOLD_SINGLE_IO_IN_DSLP) && SOC_GPIO_SUPPORT_HOLD_SINGLE_IO_IN_DSLP
#define PAD_HOLD_NEEDS_GLOBAL_DSLP 0  // C6: a láb-szintű hold alvás alatt is tart
#elif !defined(SOC_GPIO_SUPPORT_HOLD_IO_IN_DSLP) || SOC_GPIO_SUPPORT_HOLD_IO_IN_DSLP
#define PAD_HOLD_NEEDS_GLOBAL_DSLP 1  // C3: kell a globális deep sleep hold is
#else
#error "A cel chip nem tamogatja a deep sleep pad-holdot -> allitsd DEEP_SLEEP_PAD_HOLD 0-ra"
#endif

static const gpio_num_t HOLD_PINS[] = {
  (gpio_num_t)RELAY_EN, (gpio_num_t)RELAY_MAIN,
  (gpio_num_t)RELAY_FAN1, (gpio_num_t)RELAY_FAN2, (gpio_num_t)RELAY_FAN3
};
static const size_t HOLD_PIN_COUNT = sizeof(HOLD_PINS) / sizeof(HOLD_PINS[0]);

// Elalvás ELŐTT, a lábak biztonságos szintre hajtása UTÁN hívandó.
static void relayPadsHoldEnable() {
  // Előbb láb-szinten (ezt kéri a globális API is: csak a már hold-olt lábakra hat)
  for (size_t i = 0; i < HOLD_PIN_COUNT; i++) gpio_hold_en(HOLD_PINS[i]);
#if PAD_HOLD_NEEDS_GLOBAL_DSLP
  gpio_deep_sleep_hold_en();  // C3: a láb-szintű hold önmagában nem tart alvás alatt
#endif
}

// Bootkor, a lábak biztonságos szintre hajtása UTÁN hívandó. C6-on ez oldja fel a
// GPIO2 RTC-holdját — enélkül a görgő reléje soha többé nem lenne kapcsolható.
static void relayPadsHoldRelease() {
#if PAD_HOLD_NEEDS_GLOBAL_DSLP
  gpio_deep_sleep_hold_dis();
#endif
  for (size_t i = 0; i < HOLD_PIN_COUNT; i++) gpio_hold_dis(HOLD_PINS[i]);
}
#else
static void relayPadsHoldEnable() {}
static void relayPadsHoldRelease() {}
#endif  // DEEP_SLEEP_PAD_HOLD

// ===================== KIS SEGÉDFÜGGVÉNYEK =====================
// Mindhárom fokozat-relé OFF (aktív-LOW → HIGH=OFF). Nyolc helyen ismétlődött szó
// szerint; több helyen épp az a lényeg, hogy EGYIK fan se maradjon behúzva
// (break-before-make, MAIN lekapcsolás, failsafe, bootteszt) — jobb, ha ez egyetlen,
// néven nevezett műveletnek látszik, mint három egymás mellé másolt sornak.
static inline void fanRelaysOff() {
  digitalWrite(RELAY_FAN1, HIGH);
  digitalWrite(RELAY_FAN2, HIGH);
  digitalWrite(RELAY_FAN3, HIGH);
}

// ===================== FORDÍTÁSI IDEJŰ PIN-ELLENŐRZÉSEK =====================
// A PINS blokk két cél (C3/C6) kézzel karbantartott listája — a legkönnyebben elkövethető
// hiba, hogy két funkció ugyanarra a GPIO-ra kerül. A C3-on ez különösen éles: a GPIO20/21
// az U0RXD/U0TXD, a GPIO2/8/9 pedig strapping láb. Derüljön ki fordításkor, ne a panelon.
// (Szándékosan itt, az első függvénydefiníció UTÁN: az .ino automatikus prototípus-
// generálása a legelső függvénydefiníció elé szúrja be a prototípusokat, ezért a PINS
// blokk mellé tett `constexpr` függvények eltolnák a beszúrási pontot.)
static constexpr uint64_t pinBit(int p) {
  return (uint64_t)1 << p;
}
static constexpr int pinCount(uint64_t mask) {
  int n = 0;
  while (mask) {
    n += (int)(mask & 1);
    mask >>= 1;
  }
  return n;
}

static constexpr uint64_t USED_PIN_MASK =
  pinBit(RELAY_FAN1) | pinBit(RELAY_FAN2) | pinBit(RELAY_FAN3) | pinBit(RELAY_MAIN)
  | pinBit(RELAY_EN) | pinBit(BUTTON_PIN) | pinBit(LED_YELLOW) | pinBit(LED_RED)
#if FAN_SENSE_ENABLE
  | pinBit(FAN1_SENSE_PIN) | pinBit(FAN2_SENSE_PIN) | pinBit(FAN3_SENSE_PIN)
#endif
#if defined(CONFIG_IDF_TARGET_ESP32C6)
  | pinBit(RF_SWITCH_EN) | pinBit(ANT_SELECT)
#endif
  ;
static constexpr int USED_PIN_TOTAL = 8
#if FAN_SENSE_ENABLE
                                      + 3
#endif
#if defined(CONFIG_IDF_TARGET_ESP32C6)
                                      + 2
#endif
  ;
static_assert(pinCount(USED_PIN_MASK) == USED_PIN_TOTAL,
              "PINS: ket funkcio ugyanarra a GPIO-ra van kotve!");

// A gombnak deep sleepbol kell ebresztenie — erre csak a chip RTC/LP-kepes labai
// alkalmasak (C3: GPIO0-5, C6: GPIO0-7). Rossz lab valasztasa eseten az eszkoz
// egyszeruen nem ebredne fel; ez fordituskor derul ki.
static_assert((SOC_GPIO_DEEP_SLEEP_WAKE_VALID_GPIO_MASK & pinBit(BUTTON_PIN)) != 0,
              "BUTTON_PIN nem tud deep sleepbol ebreszteni ezen a chipen!");

bool otaIsRunning() {
  return (otaMode != OTA_NORMAL_MODE);
}

// [FIX-ESP-63] TWDT-hívások néma bukásának kiszűrése: a hibát a soros logba ÉS a
// diag.log-ba is kitesszük, hogy ne kelljen találgatni, ha a watchdog nem úgy viselkedik.
static void wdtLogIfError(const char* what, esp_err_t rc) {
  if (rc == ESP_OK) return;
  char e[72];
  snprintf(e, sizeof(e), "[boot] TWDT %.10s failed: %.30s", what, esp_err_to_name(rc));
  DBG_VLN(e);
  diagLog(e);
}

// ===================== OTA HELPERS =====================
static uint32_t crc32_zlib(const uint8_t* p, size_t n) {
  uint32_t crc = 0xFFFFFFFF;
  for (size_t i = 0; i < n; i++) {
    crc ^= p[i];
    for (int k = 0; k < 8; k++) {
      crc = (crc >> 1) ^ (0xEDB88320u & (~(crc & 1u) + 1u));
    }
  }
  return ~crc;
}

// [FIX-ESP-57] Az OTA állapotgép teljes alaphelyzetbe állítása (puffer felszabadítással).
// Eddig ugyanez a mező-lista háromszor, apró eltérésekkel ismétlődött (otaAbort,
// otaWriteBinary "FS full", onDisconnect) — és egy negyedik helyen HIÁNYZOTT.
static void otaResetState() {
  otaMode = OTA_NORMAL_MODE;
  otaInstallWaiting = false;
  otaInstallWaitUntil = 0;
  otaReceivedBytes = 0;
  otaTotalBytes = 0;
  otaParts = 0;
  otaCur = 0;
  otaMTU = 0;
  otaWriteLen = 0;
  otaWriteFile = false;
  otaPartRetry = 0;
  otaExpectedPart = 0;
  if (otaBuf) {
    free(otaBuf);
    otaBuf = nullptr;
  }
}

static void otaAbort(const String& msg) {
  DBG_P("OTA abort: ");
  DBG_VLN(msg);
  char e[80];
  snprintf(e, sizeof(e), "[ota] abort: %.60s", msg.c_str());
  diagLog(e);
  if (pOtaTx) {
    String result = String((char)OTA_TX_RESULT) + "ERR: " + msg;
    pOtaTx->setValue(result.c_str());
    pOtaTx->notify();
    delay(200);
  }
  if (FLASH.exists("/update.bin")) FLASH.remove("/update.bin");
  otaResetState();  // [FIX-ESP-57] (benne a [FIX-ESP-38] puffer-felszabadítás is)
}

static void rebootEspWithReason(const char* reason) {  // [MOD-25] volt: String érték szerint (másolat hívásonként)
  DBG_P("Rebooting: ");
  DBG_VLN(reason);
  disableRelays();  // [FIX-ESP-56] a reset alatt lebegő lábak előtt hajtsuk biztonságos szintre a reléket
  delay(1000);
  ESP.restart();
}

static void otaWriteBinary(fs::FS& fs, const char* path, uint8_t* dat, int len) {
  OTA_DBG_P("FS write len=");
  OTA_DBG_VLN(len);

  File file = fs.open(path, FILE_APPEND);
  if (!file) {
    DBG("FS write fail");
    otaWriteFile = false;
    return;
  }
  size_t written = file.write(dat, len);
  file.close();
  otaWriteFile = false;
  otaReceivedBytes += written;  // [FIX-ESP-4] 2026-05-24: a TÉNYLEGESEN kiírt
  OTA_DBG_P("FS write done, total=");
  OTA_DBG_VLN(otaReceivedBytes);

  if (written < (size_t)len) {
    DBG_P("FS full! Wrote ");
    DBG_V(written);
    DBG_P(" of ");
    DBG_V(len);
    DBG_P(" bytes (FS free: ");
    DBG_V(FLASH.totalBytes() - FLASH.usedBytes());
    DBG(")");
    DBG("Aborting OTA");

    otaResetState();  // [FIX-ESP-57]

    if (fs.exists(path)) {
      fs.remove(path);
      DBG("Partial update.bin removed");
    }

    if (pOtaTx) {
      String result = String((char)OTA_TX_RESULT) + "ERR: FS full";
      pOtaTx->setValue(result.c_str());
      pOtaTx->notify();
      delay(200);
    }
  }
}

void ota_boot_flow() {
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* boot = esp_ota_get_boot_partition();

  DBG("=== OTA BOOT FLOW ===");

  DBG_P("Running partition: type=");
  DBG_V(running->type);
  DBG_P(" subtype=");
  DBG_V(running->subtype);
  DBG_P(" address=0x");
  DBG_VLN(running->address, HEX);

  if (running != boot) {
    DBG_P("Boot partition: type=");
    DBG_V(boot->type);
    DBG_P(" subtype=");
    DBG_V(boot->subtype);
    DBG_P(" address=0x");
    DBG_VLN(boot->address, HEX);

    DBG("New firmware booted FIRST TIME");
  }

  esp_ota_img_states_t state;
  esp_err_t st = esp_ota_get_state_partition(running, &state);

  if (st == ESP_OK) {
#if DEBUG
    const char* stName;
    switch (state) {
      case ESP_OTA_IMG_NEW: stName = "NEW"; break;
      case ESP_OTA_IMG_PENDING_VERIFY: stName = "PENDING_VERIFY"; break;
      case ESP_OTA_IMG_VALID: stName = "VALID"; break;
      case ESP_OTA_IMG_INVALID: stName = "INVALID"; break;
      case ESP_OTA_IMG_ABORTED: stName = "ABORTED"; break;
      default: stName = "UNDEFINED"; break;
    }
    DBG_P("OTA image state: ");
    DBG_V(stName);
    DBG_P(" (0x");
    DBG_V(state, HEX);
    DBG(")");
#endif

    // Health-check: NE validáljuk most — a loop/enterDeepSleep majd, stabil futás után (itt a fájlrendszer sincs még felcsatolva)
    if (state == ESP_OTA_IMG_PENDING_VERIFY) {
      otaPendingVerify = true;
      DBG("PENDING_VERIFY → health-check: validalas stabil futas utan");
    }
  } else {
    DBG_P("Failed to read OTA state: ");
    DBG_VLN(esp_err_to_name(st));
  }

  DBG("=== OTA BOOT FLOW END ===");
}

void sendOtaResult(const String& result) {  // [MOD-25] volt: érték szerint (másolat hívásonként)
  if (!pOtaTx) return;
  pOtaTx->setValue(result.c_str());
  pOtaTx->notify();
  delay(200);
}

// [FIX-ESP-57] Visszatérési érték: true = a firmware ki lett írva (újraindulás jön),
// false = a telepítés MEGBUKOTT. A hívó ilyenkor kötelezően visszaállítja az OTA
// állapotgépet — enélkül `OTA_INSTALL_MODE`-ban ragadtunk (lásd updateFromFS).
bool performUpdate(Stream& updateSource, size_t updateSize) {
  String result = String((char)OTA_TX_RESULT);

  DBG("=== OTA DEBUG START ===");

  DBG("WDT delete (flash write may block)...");
  // [FIX-ESP-64] Az idle-figyelés kikapcsolása óta ez TELJES leiratkozás: a TWDT-nek
  // nem marad bejegyzése, így a timer is leáll a hosszú flash-írás idejére.
  esp_task_wdt_delete(NULL);

#if DEBUG
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* next = esp_ota_get_next_update_partition(NULL);

  DBG("Running partition:");
  DBG_P("  addr=0x");
  DBG_VLN(running->address, HEX);
  DBG_P(" size=");
  DBG_V(running->size);
  DBG_P(" label=");
  DBG_VLN(running->label);

  DBG("Next OTA partition:");
  DBG_P("  addr=0x");
  DBG_VLN(next->address, HEX);
  DBG_P(" size=");
  DBG_V(next->size);
  DBG_P(" label=");
  DBG_VLN(next->label);
#endif

  DBG_P("updateSize = ");
  DBG_VLN(updateSize);

  int magic = updateSource.peek();
  DBG_P("First byte (magic) = 0x");
  DBG_VLN(magic, HEX);
  if (magic != 0xE9) {
    DBG("ERR: bad firmware magic (not 0xE9)");
    char m[40];
    snprintf(m, sizeof(m), "[ota] bad magic=0x%02X size=%u", (unsigned)(magic & 0xFF), (unsigned)updateSize);
    diagLog(m);

    result += "ERR: rossz firmware (magic=0x";
    char hx[4];
    snprintf(hx, sizeof(hx), "%02X", (unsigned)(magic & 0xFF));
    result += hx;
    result += ", nem app .bin)";
    DBG("=== OTA DEBUG END ===");

    wdtLogIfError("re-add", esp_task_wdt_add(NULL));  // [FIX-ESP-63]
    sendOtaResult(result);
    return false;
  }

  DBG("Calling Update.begin()...");
  bool ok = Update.begin(updateSize);
  if (!ok) {
    DBG("Update.begin FAILED!");
    DBG_P("Error code: ");
    DBG_VLN(Update.getError());
    DBG_P("Error string: ");
    DBG_VLN(Update.errorString());

    result += "Update.begin FAILED: ";
    result += Update.errorString();
    DBG("=== OTA DEBUG END ===");

    wdtLogIfError("re-add", esp_task_wdt_add(NULL));  // [FIX-ESP-63]
    sendOtaResult(result);
    return false;
  }

  DBG("Update.begin OK");

  DBG("Calling Update.writeStream...");
  size_t written = Update.writeStream(updateSource);

  DBG_P("Update.writeStream returned: ");
  DBG_VLN(written);

  if (written != updateSize) {
    DBG("WARNING: written != updateSize");
    DBG_P("Expected: ");
    DBG_VLN(updateSize);
    DBG_P("Got: ");
    DBG_VLN(written);
  }

  DBG("Calling Update.end()...");
  bool endOK = Update.end();

  DBG_P("Update.end() returned: ");
  DBG_VLN(endOK ? "true" : "false");

  if (!endOK) {
    DBG("Update.end FAILED");
    DBG_P("Error code: ");
    DBG_VLN(Update.getError());
    DBG_P("Error string: ");
    DBG_VLN(Update.errorString());

    result += "Update.end FAILED: ";
    result += Update.errorString();
    DBG("=== OTA DEBUG END ===");

    wdtLogIfError("re-add", esp_task_wdt_add(NULL));  // [FIX-ESP-63]
    sendOtaResult(result);
    return false;
  }

  DBG_P("Update.isFinished(): ");
  DBG_VLN(Update.isFinished() ? "true" : "false");

  if (!Update.isFinished()) {
    DBG("ERROR: Update not finished!");
  }

  DBG("=== OTA DEBUG END ===");

  DBG("WDT add back");
  wdtLogIfError("re-add", esp_task_wdt_add(NULL));  // [FIX-ESP-63]

  result += "Written: " + String(written) + "/" + String(updateSize) + "\n";
  result += "OTA done\n";

  if (otaDeviceConnected) {
    DBG("BLE connected → sending OTA result + scheduling reboot");
    sendOtaResult(result);
    otaPendingReboot = true;
    otaRebootAt = millis() + 5000;
  } else {
    DBG("No BLE → immediate reboot");
    rebootEspWithReason("OTA done");
  }
  return true;
}

void updateFromFS(fs::FS& fs) {
  File updateBin = fs.open("/update.bin");
  if (updateBin) {
    if (updateBin.isDirectory()) {
      DBG("update.bin is dir");
      updateBin.close();
      return;
    }

    size_t updateSize = updateBin.size();

    if (updateSize > 0) {
      DBG("Start OTA from FS");
      // [FIX-ESP-57] Bukott telepítés (rossz magic / Update.begin / Update.end) után az
      // otaMode eddig OTA_INSTALL_MODE-on maradt, a felső if-ek pedig már egyikre sem
      // illeszkedtek (otaTotalBytes=0) → az eszköz VÉGLEG OTA-módban ragadt: nem futott
      // az állapotgép (gomb, failsafe, relé-figyelés, diag, NVS-mentés se), csak a
      // BLE-bontás vagy áramtalanítás hozta vissza. Most kötelezően visszaállunk.
      if (!performUpdate(updateBin, updateSize)) {
        DBG("OTA install failed → OTA state reset");
        diagLog("[ota] install failed -> state reset");
        otaResetState();
      }
    } else {
      DBG("update.bin empty");
    }

    updateBin.close();

    DBG("Removing update.bin");
    fs.remove("/update.bin");

  } else {
    DBG("update.bin not found");
  }
}

// ===================== BLE SERVER CALLBACKS =====================
class MyServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer) {
    bleConnected = true;
    otaDeviceConnected = true;
    DBG("BLE connected");
    bleDisconnectTime = 0;
  };

  void onDisconnect(BLEServer* pServer) {
    bleConnected = false;
    otaDeviceConnected = false;
    isAuthenticated = false;
    authAttempts = 0;
    lockoutStart = 0;
    bleDisconnectTime = millis();
    DBG("BLE disconnected");

    if (diagStreaming) {
      if (diagFile) diagFile.close();
      diagStreaming = false;
    }
    diagRequested = false;
    diagClearRequested = false;

    // [MOD-27] A flash-méret csomagot kapcsolatonként egyszer küldjük; eddig a flag
    // bontáskor nem állt vissza, így egy MÁSODIK OTA-kliens már nem kapta meg.
    otaSendSize = true;

    if (otaMode != OTA_NORMAL_MODE) {
      DBG("OTA interrupted – resetting OTA state");
      otaResetState();  // [FIX-ESP-57]
      otaPendingReboot = false;
      otaRebootAt = 0;
      if (FLASH.exists("/update.bin")) {
        FLASH.remove("/update.bin");
        DBG("Incomplete update.bin removed");
      }
    }

    if (bleEnabled) {
      portENTER_CRITICAL(&bleCmdMux);
      bleNeedsRestart = true;
      bleRestartTime = 0;
      portEXIT_CRITICAL(&bleCmdMux);
    }
  }
};

// ===================== FAN CHARACTERISTIC CALLBACKS =====================
class MyCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pCharacteristic) {
    if (!bleConnected) {
      DBG("BLE cmd rejected (no conn)");
      return;
    }

    String val = pCharacteristic->getValue();
    val.trim();

    if (val.length() == 0) return;

    DBG_P("BLE cmd: ");
    if (val.startsWith("AUTH:")) {
      DBG("AUTH:****");
    } else {
      DBG_VLN(val);
    }

    if (val.startsWith("AUTH:")) {
      if (lockoutStart != 0 && millis() - lockoutStart < AUTH_LOCKOUT_TIME_MS) {
        DBG("Auth locked");
        pCharacteristic->setValue("AUTH_LOCKED");
        pCharacteristic->notify();
        return;
      }

      // [MOD-24] Összehasonlítás a már meglévő `val` puffere felett, substring-allokáció
      // nélkül. Az ág feltétele (startsWith("AUTH:")) garantálja a legalább 5 karaktert,
      // így a +5 offset legrosszabb esetben a lezáró NUL-ra mutat (üres PIN → nem egyezik).
      const char* receivedPin = val.c_str() + 5;

      if (!BLE_AUTH_REQUIRED || strcmp(receivedPin, BLE_AUTH_PIN) == 0) {
        isAuthenticated = true;
        authAttempts = 0;
        DBG("Auth OK");
        pCharacteristic->setValue("AUTH_OK");
        pCharacteristic->notify();
      } else {
        authAttempts++;
        DBG("Auth failed");
        if (authAttempts >= MAX_AUTH_ATTEMPTS) {
          lockoutStart = millis();
          DBG("Auth lockout");
          pCharacteristic->setValue("AUTH_LOCKED");
        } else {
          pCharacteristic->setValue("AUTH_FAIL");
        }
        pCharacteristic->notify();
      }

    } else if (val.startsWith("LEVEL:")) {
      if (BLE_AUTH_REQUIRED && !isAuthenticated) {  // [MOD-24] allokáció-mentes
        DBG("LEVEL rejected (no auth)");
        pCharacteristic->setValue("AUTH_REQUIRED");
        pCharacteristic->notify();
        return;
      }

      if (val.length() != 7 || !isDigit(val.charAt(6))) {
        DBG("Invalid zone value");
        return;
      }

      int zone = val.charAt(6) - '0';

      if (zone > 3 || zone < 0) {
        DBG("Zone out of range");
        return;
      }

      portENTER_CRITICAL(&bleCmdMux);
      bleCmd.zone = zone;
      bleCmd.hasCommand = true;
      portEXIT_CRITICAL(&bleCmdMux);

      DBG_P("Zone queued: ");
      DBG_VLN(zone);

    } else if (val.startsWith("ROLLER:")) {
      if (BLE_AUTH_REQUIRED && !isAuthenticated) {  // [MOD-24] allokáció-mentes
        DBG("ROLLER rejected (no auth)");
        pCharacteristic->setValue("AUTH_REQUIRED");
        pCharacteristic->notify();
        return;
      }

      if (val.length() != 8 || !isDigit(val.charAt(7))) {
        DBG("Invalid roller value");
        return;
      }

      int mainCmd = val.charAt(7) - '0';

      if (mainCmd != 0 && mainCmd != 1) {
        DBG("Roller must be 0/1");
        return;
      }

      portENTER_CRITICAL(&bleCmdMux);
      bleCmd.mainCommand = mainCmd;
      bleCmd.hasMainCommand = true;
      portEXIT_CRITICAL(&bleCmdMux);

      DBG_P("Roller queued: ");
      DBG_VLN(mainCmd);

    } else if (val.startsWith("DIAG?")) {
      if (BLE_AUTH_REQUIRED && !isAuthenticated) {  // [MOD-24] allokáció-mentes
        DBG("DIAG rejected (no auth)");
        pCharacteristic->setValue("AUTH_REQUIRED");
        pCharacteristic->notify();
        return;
      }
      diagRequested = true;
      DBG("Diag log requested");

    } else if (val.startsWith("DIAGCLR")) {
      if (BLE_AUTH_REQUIRED && !isAuthenticated) {  // [MOD-24] allokáció-mentes
        DBG("DIAGCLR rejected (no auth)");
        pCharacteristic->setValue("AUTH_REQUIRED");
        pCharacteristic->notify();
        return;
      }
      diagClearRequested = true;
      DBG("Diag clear requested");

    } else {
      DBG("Unknown BLE cmd");
    }
  }
};

// ===================== OTA CHARACTERISTIC CALLBACKS =====================
class OtaCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pCharacteristic) {
    uint8_t* pData = pCharacteristic->getData();
    // [MOD-23] getLength() a getValue().length() helyett: a getValue() String-et ad
    // vissza ÉRTÉK szerint, azaz minden egyes OTA-csomagra heap-allokáció + másolás
    // (~11 500 csomag / 1,1 MB firmware). A getData()/getLength() ugyanazt a puffert
    // éri el másolás nélkül. `int`-ként tartjuk: a lenti `len - 2` előjeles kell legyen.
    const int len = (int)pCharacteristic->getLength();
    if (pData == NULL || len == 0) return;

    OTA_DBG("OTA packet");

    // [FIX-ESP-62] Csonka csomagnál eddig a fejléc-mezőket a BLE értékpuffer VÉGÉN
    // TÚL olvastuk (0xFB: pData[1]; 0xFE/0xFF: pData[1..4]). A 0xFC-nek már volt
    // hossz-őre ([FIX-ESP-51] mellett), a többinek nem — pótolva.
    const int needLen = (pData[0] == OTA_RX_PART_CHUNK) ? 2
                        : ((pData[0] == OTA_RX_TOTAL_SIZE || pData[0] == OTA_RX_BEGIN) ? 5 : 1);
    if (len < needLen) {
      DBG_P("OTA packet too short, cmd=0x");
      DBG_VLN(pData[0], HEX);
      return;
    }

    if (pData[0] == OTA_RX_PART_CHUNK) {
      if (otaBuf) {
        int base = pData[1] * otaMTU;
        for (int x = 0; x < len - 2; x++) {
          if ((base + x) < (int)OTA_BUF_SIZE) otaBuf[base + x] = pData[x + 2];
        }
      }

    } else if (pData[0] == OTA_RX_PART_END) {
      OTA_DBG_P("0xFC part=");
      OTA_DBG_VLN((pData[3] * 256) + pData[4]);
      if (len < 9) {
        DBG("0xFC too short (no CRC) — re-requesting part");
        otaPartRetry++;
        if (otaPartRetry <= MAX_PART_RETRY && pOtaTx) {
          uint8_t rq[] = { OTA_TX_REQ_PART, (uint8_t)(otaExpectedPart / 256), (uint8_t)(otaExpectedPart % 256) };
          pOtaTx->setValue(rq, 3);
          pOtaTx->notify();
        } else {
          otaAbort("0xFC truncated");
        }
      } else {
        // [FIX-ESP-51] A 0xFC hosszmezője (0..65535) vezérli a CRC-számítás és a
        // fájlba írás olvasását az OTA_BUF_SIZE (16 KB) pufferből. A 0xFB író ág már
        // határ-ellenőrzött, ez viszont eddig nem volt: sérült/eltérő PART-méretű
        // kliens ~48 KB heap-túlolvasást okozhatott. Nem fér bele → abort.
        int wlen = (pData[1] * 256) + pData[2];
        if (wlen <= 0 || wlen > (int)OTA_BUF_SIZE) {
          DBG_P("0xFC bad part length: ");
          DBG_VLN(wlen);
          otaAbort("bad part length");
        } else {
          otaWriteLen = wlen;
          otaExpectedCrc = ((uint32_t)pData[5] << 24) | ((uint32_t)pData[6] << 16) | ((uint32_t)pData[7] << 8) | ((uint32_t)pData[8]);
          otaCur = (pData[3] * 256) + pData[4];
          otaWriteFile = true;
        }
      }

    } else if (pData[0] == OTA_RX_FILE_DEL) {
      if (FLASH.exists("/update.bin")) {
        FLASH.remove("/update.bin");
      }

    } else if (pData[0] == OTA_RX_TOTAL_SIZE) {
      otaReceivedBytes = 0;
      otaTotalBytes = ((uint32_t)pData[1] << 24) | ((uint32_t)pData[2] << 16) | ((uint32_t)pData[3] << 8) | ((uint32_t)pData[4]);
      uint32_t fsFree = FLASH.totalBytes() - FLASH.usedBytes();
      DBG_P("FS free: ");
      DBG_VLN(fsFree);
      DBG_P("OTA size: ");
      DBG_VLN(otaTotalBytes);

      const uint32_t FS_OVERHEAD = 4096;  // egy blokknyi tartalék a fájlrendszer metaadatainak
      if (otaTotalBytes + FS_OVERHEAD > fsFree) {
        DBG("ERR: FS too small for OTA");
        DBG_P("Need (with overhead): ");
        DBG_VLN(otaTotalBytes + FS_OVERHEAD);
        DBG_P("Available: ");
        DBG_VLN(fsFree);

        if (pOtaTx) {
          String result = String((char)OTA_TX_RESULT) + "ERR: FS too small (need " + String(otaTotalBytes + FS_OVERHEAD) + ", have " + String(fsFree) + ")";
          pOtaTx->setValue(result.c_str());
          pOtaTx->notify();
          delay(200);
        }

        otaMode = OTA_NORMAL_MODE;
        otaTotalBytes = 0;
        otaReceivedBytes = 0;
        return;
      }

    } else if (pData[0] == OTA_RX_BEGIN) {
      otaParts = (pData[1] * 256) + pData[2];
      otaMTU = (pData[3] * 256) + pData[4];
      otaCur = 0;
      otaWriteFile = false;
      otaPartRetry = 0;
      otaExpectedPart = 0;
      if (!otaBuf) otaBuf = (uint8_t*)malloc(OTA_BUF_SIZE);
      if (!otaBuf) {
        DBG("OTA abort: malloc fail (no RAM)");
        otaAbort("no RAM for OTA");
      } else {
        otaMode = OTA_UPDATE_MODE;
        DBG_P("OTA parts: ");
        DBG_VLN(otaParts);
        if (pOtaTx) {
          uint8_t rq[] = { OTA_TX_REQ_PART, 0x00, 0x00 };
          pOtaTx->setValue(rq, 3);
          pOtaTx->notify();
        }
      }

    } else if (pData[0] == OTA_RX_FS_FORMAT) {
      FLASH.format();
      otaSendSize = true;
    }
  }
};

// Bypass-mód jelzés: 1 mp gyors váltakozó LED-villogás (defenzív: 1 ms delay + WDT reset, nem 60 ms blokkolás)
static void bypassBlinkIndicator() {
  unsigned long t0 = millis();
  while (millis() - t0 < 1000) {
    digitalWrite(LED_YELLOW, HIGH);
    digitalWrite(LED_RED, LOW);
    for (int i = 0; i < 60; i++) { delay(1); esp_task_wdt_reset(); }
    digitalWrite(LED_YELLOW, LOW);
    digitalWrite(LED_RED, HIGH);
    for (int i = 0; i < 60; i++) { delay(1); esp_task_wdt_reset(); }
  }
  digitalWrite(LED_YELLOW, LOW);
  digitalWrite(LED_RED, LOW);
}

// ===================== BUTTON HANDLERS =====================
void handleClick() {
  if (otaIsRunning()) return;

  DBG("Button: click");

  if (!mainActive) {
    enableRelays();
    delay(100);
    activateMain();
  } else if (currentZone != 0) {
    // Aktív ventilátor → első gombnyomás csak a ventilátort állítja le (görgő/relé marad); a következő kattintás kapcsol ki mindent
    manualZoneIndex = 0;
    setFanZone(0, SRC_BUTTON);
  } else {
    deactivateMain();
    delay(100);
    disableRelays();
  }
}

void handleLongPressStop() {
  if (otaIsRunning()) return;

  DBG("Button: long → sleep");
  enterDeepSleep("button-longpress");
}

void handleDoubleClick() {
  if (otaIsRunning()) return;

  bleEnabled = false;
  DBG("Button: double");

  if (!manualMode) {
    manualMode = true;
    DBG("Manual mode ON");

    if (bleConnected) {
      pServer->disconnect(pServer->getConnId());  // [FIX-ESP-58] nem hardkódolt 0
      delay(100);
    }

    BLEDevice::stopAdvertising();
    bleConnected = false;

    manualZoneIndex = 1;
    setFanZone(manualZoneIndex, SRC_BUTTON);

  } else {
    manualZoneIndex = (manualZoneIndex + 1) % 4;
    setFanZone(manualZoneIndex, SRC_BUTTON);
  }
}

void handleMultiClick() {
  if (otaIsRunning()) return;

  int clicks = button.getNumberClicks();

  if (clicks == 3) {
    DBG("Multi-click → AUTO mode");

    manualMode = false;
    bleEnabled = true;

    manualZoneIndex = 0;
    setFanZone(0, SRC_BUTTON);

    BLEDevice::startAdvertising();
    DBG("Manual mode OFF, BLE advertising restarted");
    return;
  }

  if (clicks == 5) {
    relaySenseBypass = !relaySenseBypass;
    bypassPrefs.putBool("enabled", relaySenseBypass);

    bypassBlinkIndicator();

    zeroStateForBypass();
    disableRelays();  // [FIX-ESP-44] Defensive: ensure relays are OFF before restart

    ESP.restart();
  }
}

// ===================== OTA SERVICE INIT =====================
void otaInitService(BLEServer* server) {
  if (!otaCrcOk) {  // CRC-FAIL → nem regisztráljuk az OTA szolgáltatást
    DBG("OTA service NOT started: CRC32 self-test failed");
    return;
  }

  BLEService* pOtaService = server->createService(OTA_SERVICE_UUID);

  pOtaTx = pOtaService->createCharacteristic(
    OTA_CHARACTERISTIC_UUID_TX,
    BLECharacteristic::PROPERTY_NOTIFY);
  pOtaRx = pOtaService->createCharacteristic(
    OTA_CHARACTERISTIC_UUID_RX,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);

  pOtaRx->setCallbacks(new OtaCallbacks());
#if !defined(CONFIG_NIMBLE_ENABLED)
  pOtaTx->addDescriptor(new BLE2902());  // [FIX-ESP-61] lásd a fan-karakterisztikánál
#endif
  pOtaTx->setNotifyProperty(true);

  pOtaService->start();

  BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(OTA_SERVICE_UUID);
}

static const char* resetReasonStr(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_EXT: return "EXT";
    case ESP_RST_SW: return "SW";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SDIO: return "SDIO";
    default: return "UNKNOWN";
  }
}

void diagLog(const char* line) {
  // [FIX-ESP-42] Guard: avoid concurrent FILE_APPEND while diagStreaming holds FILE_READ on the FS
  if (diagStreaming) return;

  if (FLASH.exists(DIAG_LOG_PATH)) {
    File f = FLASH.open(DIAG_LOG_PATH, FILE_READ);
    if (f) {
      size_t sz = f.size();
      if (sz > DIAG_LOG_MAX) {
        // A [ver] (stabil firmware-verzió) sort mindig megőrizzük a fájl elején
        String verLine = f.readStringUntil('\n');
        bool hasVer = verLine.startsWith("[ver] ");

        uint8_t tmp[DIAG_LOG_MAX / 2];
        f.seek(sz - sizeof(tmp));
        int n = f.read(tmp, sizeof(tmp));
        f.close();
        int start = 0;
        for (int i = 0; i < n; i++) {
          if (tmp[i] == '\n') {
            start = i + 1;
            break;
          }
        }
        File w = FLASH.open(DIAG_LOG_PATH, FILE_WRITE);  // FILE_WRITE = truncate
        if (w) {
          if (hasVer) {
            w.print(verLine);
            w.print('\n');
          }
          if (n > start) w.write(tmp + start, n - start);
          w.close();
        }
      } else {
        f.close();
      }
    }
  }

  File f = FLASH.open(DIAG_LOG_PATH, FILE_APPEND);
  if (f) {
    f.print(line);
    f.print('\n');
    f.close();
  } else {
    DBG("diagLog write fail");
  }
}

// A diag.log első sorát adja vissza, ha az egy [ver] sor; egyébként üres String.
String diagReadVersionLine() {
  String v;
  if (FLASH.exists(DIAG_LOG_PATH)) {
    File f = FLASH.open(DIAG_LOG_PATH, FILE_READ);
    if (f) {
      String first = f.readStringUntil('\n');
      if (first.startsWith("[ver] ")) v = first;
      f.close();
    }
  }
  return v;
}

// Stabil verziót a diag.log sticky első sorába írja ([ver] X), dedupe-olva
void logStableVersion() {
  if (diagStreaming) return;

  char want[40];
  snprintf(want, sizeof(want), "[ver] %s", FIRMWARE_VERSION);

  if (diagReadVersionLine() == want) return;  // már stimmel → nincs írás

  // Újraírás: [ver] elöl, alatta a meglévő tartalom a régi [ver] sor nélkül
  String rest;
  if (FLASH.exists(DIAG_LOG_PATH)) {
    File f = FLASH.open(DIAG_LOG_PATH, FILE_READ);
    if (f) {
      bool first = true;
      while (f.available()) {
        String line = f.readStringUntil('\n');
        if (first) {
          first = false;
          if (line.startsWith("[ver] ")) continue;  // régi verzió sor kihagyása
        }
        rest += line;
        rest += '\n';
      }
      f.close();
    }
  }

  File w = FLASH.open(DIAG_LOG_PATH, FILE_WRITE);  // truncate
  if (w) {
    w.print(want);
    w.print('\n');
    w.print(rest);
    w.close();
  }
}

void printBootDiag() {
#if BOOT_DIAG
  bool rtcValid = (savedZoneMagic == SAVED_ZONE_MAGIC && savedZone >= 0 && savedZone <= 3);
  bool nvsValid = (nvsLastSavedZone >= 0 && nvsLastSavedZone <= 3);
  bool mainRtcValid = (savedMainMagic == SAVED_MAIN_MAGIC && (savedMain == 0 || savedMain == 1));

  Serial.println();
  Serial.println(F("===================================="));
  Serial.println(F("BOOT DIAG (RTC / NVS / diag.log)"));
  Serial.println(F("===================================="));

  Serial.print(F("Free heap: "));
  Serial.println(ESP.getFreeHeap());

  Serial.print(F("RTC magic: 0x"));
  Serial.print(savedZoneMagic, HEX);
  Serial.print(F(" ("));
  Serial.print(rtcValid ? F("valid") : F("invalid"));
  Serial.println(F(")"));
  Serial.print(F("RTC savedZone: "));
  Serial.println(savedZone);
  Serial.print(F("RTC savedMain: "));
  Serial.print(savedMain);
  Serial.print(F(" ("));
  Serial.print(mainRtcValid ? F("valid") : F("invalid"));
  Serial.println(F(")"));

  Serial.print(F("NVS zone: "));
  Serial.print(nvsLastSavedZone);
  Serial.print(F(" ("));
  Serial.print(nvsValid ? F("valid") : F("none/invalid"));
  Serial.println(F(")"));
  Serial.print(F("NVS main: "));
  Serial.print(nvsLastSavedMain);
  Serial.print(F(" ("));
  Serial.print((nvsLastSavedMain == 0 || nvsLastSavedMain == 1) ? F("valid") : F("none/invalid"));
  Serial.println(F(")"));

  Serial.println(F("--- diag.log ---"));
  if (FLASH.exists(DIAG_LOG_PATH)) {
    File df = FLASH.open(DIAG_LOG_PATH, FILE_READ);
    if (df) {
      if (df.size() == 0) {
        Serial.println(F("(ures)"));
      } else {
        while (df.available()) Serial.write(df.read());
        Serial.println();
      }
      df.close();
    } else {
      Serial.println(F("(nem olvashato)"));
    }
  } else {
    Serial.println(F("(nincs diag.log)"));
  }
  Serial.println(F("===================================="));
#endif
}

void handleDiagRequest() {
  if (!pCharacteristic) return;

  // [FIX-ESP-52] Streamelés közben NEM csonkoljuk a naplót: a diagFile ilyenkor nyitott
  // FILE_READ handle-t tart rá (a diagLog append-ágát ugyanez védi, [FIX-ESP-42]).
  // A kérés függőben marad, és a stream lezárása után a következő híváskor lefut.
  if (diagClearRequested && !diagStreaming) {
    diagClearRequested = false;
    // A [ver] sort megőrizzük: csak a hibabejegyzéseket töröljük
    String ver = diagReadVersionLine();
    if (ver.length()) {
      File w = FLASH.open(DIAG_LOG_PATH, FILE_WRITE);  // truncate
      if (w) {
        w.print(ver);
        w.print('\n');
        w.close();
      }
    } else if (FLASH.exists(DIAG_LOG_PATH)) {
      FLASH.remove(DIAG_LOG_PATH);
    }
    pCharacteristic->setValue("DIAG_CLEARED");
    pCharacteristic->notify();
    DBG("Diag log cleared");
    return;
  }

  if (diagRequested && !diagStreaming) {
    diagRequested = false;
    diagFile = FLASH.open(DIAG_LOG_PATH, FILE_READ);
    static const uint8_t DIAG_BEGIN[] = { 0x02, 'D', 'I', 'A', 'G', '_', 'B', 'E', 'G', 'I', 'N' };
    pCharacteristic->setValue((uint8_t*)DIAG_BEGIN, sizeof(DIAG_BEGIN));
    pCharacteristic->notify();
    diagStreaming = true;
    diagLastChunk = millis();
    return;
  }

  if (diagStreaming) {
    unsigned long now = millis();
    if (now - diagLastChunk < DIAG_CHUNK_INTERVAL) return;
    diagLastChunk = now;

    if (diagFile && diagFile.available()) {
      uint8_t buf[DIAG_CHUNK_SIZE];
      int n = diagFile.read(buf, DIAG_CHUNK_SIZE);
      if (n > 0) {
        pCharacteristic->setValue(buf, n);
        pCharacteristic->notify();
      }
    } else {
      if (diagFile) diagFile.close();
      static const uint8_t DIAG_END[] = { 0x04, 'D', 'I', 'A', 'G', '_', 'E', 'N', 'D' };
      pCharacteristic->setValue((uint8_t*)DIAG_END, sizeof(DIAG_END));
      pCharacteristic->notify();
      diagStreaming = false;
      DBG("Diag log sent");
    }
  }
}

// ===================== SETUP =====================
void setup() {
  // [FIX-ESP-39] Relék azonnali tiltása a setup() legelső lépéseként (Serial előtt) → legrövidebb boot-ablak: tápengedély LOW + minden relé OFF
  pinMode(RELAY_EN, OUTPUT);
  digitalWrite(RELAY_EN, LOW);
  pinMode(RELAY_FAN1, OUTPUT);
  digitalWrite(RELAY_FAN1, HIGH);
  pinMode(RELAY_FAN2, OUTPUT);
  digitalWrite(RELAY_FAN2, HIGH);
  pinMode(RELAY_FAN3, OUTPUT);
  digitalWrite(RELAY_FAN3, HIGH);
  pinMode(RELAY_MAIN, OUTPUT);
  digitalWrite(RELAY_MAIN, HIGH);
  relaysEnabled = false;
  // [FIX-ESP-55] Az alvás előtt bekapcsolt pad-hold feloldása — CSAK most, hogy a
  // lábak a hold alatt is a fenti biztonságos szinten legyenek (nincs átmeneti glitch).
  // C6-on a RELAY_MAIN (GPIO2) RTC-holdja az ébredést is túléli, ezért kötelező.
  relayPadsHoldRelease();

  pinMode(LED_YELLOW, OUTPUT);
  pinMode(LED_RED, OUTPUT);

#if SERIAL_ENABLED
  Serial.begin(115200);
  delay(100);
#endif

  DBG("GPIO + Serial init, Relays safe off done");

  bypassPrefs.begin("bypass", false);
  relaySenseBypass = bypassPrefs.getBool("enabled", false);

  if (relaySenseBypass) {
    bypassBlinkIndicator();
  }

#if defined(CONFIG_IDF_TARGET_ESP32C6)
  // C6: külső antenna kiválasztása a BLE rádió indítása előtt (közös 2,4 GHz antenna-kapcsoló)
  pinMode(RF_SWITCH_EN, OUTPUT);
  digitalWrite(RF_SWITCH_EN, LOW);  // RF switch control aktiválás
  delay(100);
  pinMode(ANT_SELECT, OUTPUT);
  digitalWrite(ANT_SELECT, HIGH);  // külső antenna használata
#endif

#if FAN_SENSE_ENABLE
  pinMode(FAN1_SENSE_PIN, INPUT_PULLUP);
  pinMode(FAN2_SENSE_PIN, INPUT_PULLUP);
  pinMode(FAN3_SENSE_PIN, INPUT_PULLUP);
  fanSenseGraceUntil = millis() + FAN_SENSE_GRACE_MS;  // boot RC-beállás (a téves STUCK-ot a !mainActive kilépés fedi)
#endif

  DBG("LED boot state");
  digitalWrite(LED_YELLOW, HIGH);
  digitalWrite(LED_RED, LOW);

  ota_boot_flow();

  static_assert(sizeof(BLE_AUTH_PIN) > 1, "BLE_AUTH_PIN is empty!");

  DBG("Boot");

  // [FIX-ESP-65] Előbb formázás NÉLKÜL próbálunk csatolni, hogy meg tudjuk különböztetni
  // a "rendben felcsatolt" és a "nem volt csatolható → megformáztuk" esetet — és az
  // utóbbit naplózni is tudjuk. Ide esik a SPIFFS → LittleFS váltás egyszeri formázása
  // is (a régi SPIFFS-tartalom nem csatolható LittleFS-ként), meg egy esetleges későbbi
  // fájlrendszer-sérülés is. A `begin()` felcsatolt állapotban azonnal `true`-val tér
  // vissza, ezért a kétlépcsős hívás biztonságos.
  if (!FLASH.begin(false)) {
    if (FLASH.begin(FORMAT_FS_IF_FAILED)) {
      DBG("FS not mountable → formatted");
      diagLog("[fs] mount failed -> formatted");
    } else {
      DBG("FS mount + format FAILED");
    }
  }

  if (FLASH.exists("/update.bin")) {
    File f = FLASH.open("/update.bin");
    if (f) {
      bool isDir = f.isDirectory();
      f.close();
      if (isDir) {
        DBG("Stale update.bin dir removed");
      } else {
        DBG("Stale update.bin removed");
      }
      FLASH.remove("/update.bin");
      delay(100);
    }
  }

  // [FIX-ESP-63] A TWDT-t az Arduino core MÁR elindította (`CONFIG_ESP_TASK_WDT_INIT=y`,
  // 5000 ms, panic; idle-task figyelés nélkül). Eddig `deinit()+init()` párral írtuk felül,
  // ellenőrzés nélkül. Ez némán elbukhat: a `deinit()` `ESP_ERR_INVALID_STATE`-et ad, ha
  // bármely task/user fel van iratkozva a TWDT-re, és ilyenkor az `init()` is
  // `ESP_ERR_INVALID_STATE` ("already initialized") — vagyis maradt volna a gyári **5 s**
  // a szándékolt 15 s helyett, mindenféle jelzés nélkül. (A jelenlegi core-beállítás
  // mellett a `deinit()` átmegy, mert boot után még senki nincs feliratkozva — de ez a
  // körülményektől függ, nem a kódtól.) Helyette a pontosan erre való
  // `esp_task_wdt_reconfigure()`: a FUTÓ TWDT-t írja át (timeout + panic + idle-maszk),
  // deinit nélkül; `init()` csak tartalék, ha a TWDT nem futna. Mindkettő ellenőrizve.
  // (Az `esp_task_wdt_reconfigure()` az IDF 5.3 óta létezik, tehát a core 3.1.x-szel is jó.)
  //
  // [FIX-ESP-64] `idle_core_mask = 0`: NEM figyeltetjük a 0. mag idle taskját (ez egyben
  // az Arduino gyári beállítása is). Az idle bejegyzés nem azt kérdezi, hogy "él-e a
  // program" (azt a loopTask bejegyzése méri), hanem hogy "volt-e a CPU-nak üresjárata" —
  // az idle task ugyanis csak akkor fut (és etet), ha semmi más nem futóképes. Egymagos
  // chipen (CONFIG_FREERTOS_UNICORE=y) ez szinte semmi pluszt nem ad: ha bármi tényleg
  // felzabálja a CPU-t, a loopTask sem jut futáshoz, tehát a saját bejegyzésünk amúgy is
  // eldurran. Cserébe viszont két valódi ára van:
  //   1) féloldalassá teszi a `performUpdate()` `esp_task_wdt_delete(NULL)` hívását — a
  //      hosszú flash-írás alatt az idle bejegyzés élesben maradt volna;
  //   2) egy téves pánik `TASK_WDT` reset-okot ad, amit a boot-helyreállítás hibás
  //      resetnek tekint → VISSZAKAPCSOLHATJA a görgőt. Itt a fals riasztás ára nagyobb,
  //      mint a hiba, amit védene (a nem engedő szakaszok — pl. a relé-önteszt 200 ms-os
  //      `delayMicroseconds` ciklusai — pont ilyen fals pánikot okoznának).
  // Így a watchdog jelentése pontosan az, amit ez a firmware akar: "iterál-e a loop()".
  esp_task_wdt_config_t wdt_config = {
    .timeout_ms = 15000,
    .idle_core_mask = 0,  // [FIX-ESP-64] CSAK a loopTask figyelt — lásd a fenti indoklást
    .trigger_panic = true
  };

  esp_err_t wdtRc = esp_task_wdt_reconfigure(&wdt_config);
  if (wdtRc == ESP_ERR_INVALID_STATE) {  // a TWDT nem futott (CONFIG_ESP_TASK_WDT_INIT=n) → indítsuk
    wdtRc = esp_task_wdt_init(&wdt_config);
  }
  wdtLogIfError("config", wdtRc);
  wdtLogIfError("add", esp_task_wdt_add(NULL));

  esp_reset_reason_t resetReason = esp_reset_reason();
  lastBootResetReason = resetReason;  // [FIX-ESP-19] globális mentés
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();

  DBG("");
  DBG("====================================");
  DBG("Xiao ESP32C3 Fan + Roller + OTA");
  DBG_P("FW: v");
  DBG_P(FIRMWARE_VERSION);
  DBG_P(" (");
  DBG_P(FIRMWARE_DATE);
  DBG(")");
  DBG_P("Reset reason: ");
  DBG_V((int)resetReason);
  DBG_P(" (");
  DBG_V(resetReasonStr(resetReason));
  DBG(")");
  DBG("====================================");

  // CRC32 önteszt ismert vektorral; FAIL → OTA letiltva + diag.log (release-ben is fut)
  {
    const uint8_t tv[] = { '1', '2', '3', '4', '5', '6', '7', '8', '9' };
    uint32_t got = crc32_zlib(tv, 9);
    otaCrcOk = (got == 0xCBF43926);
    DBG_P("CRC32 self-test: 0x");
    DBG_V(got, HEX);
    DBG_VLN(otaCrcOk ? F(" OK") : F(" FAIL!"));
    if (!otaCrcOk) {
      diagLog("[boot] CRC32 self-test FAIL -> OTA off. Just serial update!");
#if OTA_ROLLBACK_ON_CRC_FAIL
      // Frissen OTA-zott (PENDING_VERIFY) firmware CRC-bukásánál visszagörgetés
      if (otaPendingVerify) {
        diagLog("[boot] CRC FAIL on fresh OTA -> rollback");
        esp_ota_mark_app_invalid_rollback_and_reboot();  // sikernél nem tér vissza
        diagLog("[boot] rollback failed (no valid app) -> running, OTA off");
      }
#endif
    }
  }

  // Validált bootnál rögzítjük a stabil verziót; PENDING_VERIFY-t a health-check intézi
  if (!otaPendingVerify) logStableVersion();

  if (resetReason != ESP_RST_POWERON && resetReason != ESP_RST_DEEPSLEEP && resetReason != ESP_RST_SW) {
    char entry[80];
    snprintf(entry, sizeof(entry), "[boot] reason=%s(%d) heap=%u min=%u",
             resetReasonStr(resetReason), (int)resetReason,
             (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap());
    diagLog(entry);
  }

  if (resetReason == ESP_RST_DEEPSLEEP) {
    if (wakeup_reason == ESP_SLEEP_WAKEUP_GPIO) {
      DBG("Wake: button");
    } else {
      DBG("Deep sleep wake (no button) → back to sleep");
#if SERIAL_ENABLED
      Serial.flush();
#endif
      delay(100);
      pinMode(BUTTON_PIN, INPUT_PULLUP);
      esp_deep_sleep_enable_gpio_wakeup(BIT(BUTTON_PIN), ESP_GPIO_WAKEUP_GPIO_LOW);
      relayPadsHoldEnable();  // [FIX-ESP-55] relé-lábak rögzítése (ne lebegjenek alvás alatt)
      esp_deep_sleep_start();
    }
  } else if (resetReason == ESP_RST_POWERON) {
    DBG("Power-on → sleep (wait for button)");
    // [FIX-ESP-56] Áramtalanítás utáni indulás = mindent OFF-ról kezdünk (gombra várunk).
    // A perzisztens "görgő/fokozat aktív volt" jelzés törlése, különben egy alvás közbeni
    // brownout boot-helyreállítása az NVS-ből visszakapcsolná a görgőt.
    savedZone = 0;
    savedZoneMagic = SAVED_ZONE_MAGIC;
    savedMain = 0;
    savedMainMagic = SAVED_MAIN_MAGIC;
    persistRelayStateOff();
#if SERIAL_ENABLED
    Serial.flush();
#endif
    delay(100);
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    esp_deep_sleep_enable_gpio_wakeup(BIT(BUTTON_PIN), ESP_GPIO_WAKEUP_GPIO_LOW);
    relayPadsHoldEnable();  // [FIX-ESP-55] relé-lábak rögzítése (ne lebegjenek alvás alatt)
    esp_deep_sleep_start();
  } else {
    DBG("Fault/SW reset → resuming normal operation");
  }

#if RELAY_TEST_AT_BOOT
  // Relé-önteszt csak SW-resetnél és gombébresztésnél (hiba-resetnél nem); a loopban fut, BLE kapcsolat előtt
  relayTestPending = (resetReason == ESP_RST_SW) || (resetReason == ESP_RST_DEEPSLEEP && wakeup_reason == ESP_SLEEP_WAKEUP_GPIO);
#endif

  DBG("Button init");
  button.attachClick(handleClick);
  button.attachLongPressStop(handleLongPressStop);
  button.attachDoubleClick(handleDoubleClick);
  button.attachMultiClick(handleMultiClick);
  button.setPressMs(2000);
  button.setClickMs(400);

  DBG("Relay state restore");
  fanPrefs.begin("fan", true);  // read-only
  nvsLastSavedZone = fanPrefs.getInt("zone", -1);
  nvsLastSavedMain = fanPrefs.getInt("main", -1);  // [FIX-ESP-30] görgő (-1 = nincs)
  fanPrefs.end();

  if (lastBootResetReason == ESP_RST_BROWNOUT || lastBootResetReason == ESP_RST_UNKNOWN || lastBootResetReason == ESP_RST_INT_WDT || lastBootResetReason == ESP_RST_TASK_WDT || lastBootResetReason == ESP_RST_WDT) {

    bool mainRtcValid = (savedMainMagic == SAVED_MAIN_MAGIC && (savedMain == 0 || savedMain == 1));
    bool mainNvsValid = (nvsLastSavedMain == 0 || nvsLastSavedMain == 1);
    int mainWas;
    if (mainRtcValid) mainWas = savedMain;              // RTC friss
    else if (mainNvsValid) mainWas = nvsLastSavedMain;  // NVS fallback (brownout)
    else mainWas = -1;                                  // ismeretlen → nem indítunk

    // [FIX-ESP-39] Hurok-megszakító számláló (RTC). Érvénytelen magic → 0-ról indul.
    if (errRestoreMagic != ERR_RESTORE_MAGIC) {
      errRestoreCount = 0;
      errRestoreMagic = ERR_RESTORE_MAGIC;
    }

    if (mainWas != 1) {
      DBG("Boot after error reset, main was NOT active → staying idle");
    } else if (++errRestoreCount >= MAX_ERR_RESTORE) {
      // Túl sok gyors hibás reset (brownout-hurok gyanú) → nem állítunk vissza, idle marad; a számláló 30 s stabil futás után nullázódik
      DBG_P("Loop-break: consecutive error-restores=");
      DBG_V(errRestoreCount);
      DBG(" → staying idle");
      char e[64];
      snprintf(e, sizeof(e), "[boot] loop-break idle n=%d", errRestoreCount);
      diagLog(e);
    } else {
      DBG("Boot after BROWNOUT/UNKNOWN/WDT, main was active → resuming");
      enableRelays();
      activateMain();
      bool rtcValid = (savedZoneMagic == SAVED_ZONE_MAGIC && savedZone >= 0 && savedZone <= 3);
      bool nvsValid = (nvsLastSavedZone >= 0 && nvsLastSavedZone <= 3);
      int restoreZone;

      if (rtcValid) {
        restoreZone = savedZone;
        DBG_P("Restoring fan zone (RTC valid, freshest): ");
        DBG_VLN(restoreZone);
      } else if (nvsValid) {
        restoreZone = nvsLastSavedZone;
        DBG_P("Restoring fan zone (RTC invalid, NVS fallback): ");
        DBG_VLN(restoreZone);
      } else {
        restoreZone = 2;
        DBG("Both RTC and NVS invalid → defaulting to zone 2");
      }
      setFanZone(restoreZone, SRC_BUTTON);
      // [FIX-ESP-40] Fan-relé azonnali bekapcsolása bootkor: a setFanZone csak indítja a váltást, a handleZoneChange RELAY_SWITCH_DELAY_MS után hat → kivárjuk, majd hívjuk
      delay(RELAY_SWITCH_DELAY_MS + 5);
      handleZoneChange();
    }
  }

  DBG("BLE init");
  BLEDevice::init("FanController");
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new MyServerCallbacks());

  BLEService* pService = pServer->createService(SERVICE_UUID);
  pCharacteristic = pService->createCharacteristic(
    CHARACTERISTIC_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_NOTIFY);

  pCharacteristic->setCallbacks(new MyCallbacks());
  // [FIX-ESP-61] A CCCD (0x2902) leírót csak a Bluedroid stack várja kézzel. Az
  // arduino-esp32 3.3.x alapértelmezett stackje a NimBLE, ami a NOTIFY property mellé
  // AUTOMATIKUSAN létrehozza — ott a BLE2902 osztály `[[deprecated]]`, a hozzáadás
  // pedig no-op (a könyvtár felismeri a 0x2902-t és nem teszi a leíró-térképbe).
  // A feltétel szándékosan a NimBLE HIÁNYÁT nézi (nem a Bluedroid meglétét): így ha egy
  // core-verzióban a Bluedroid-makró neve változna, a leíró akkor is bekerül — a hiánya
  // Bluedroid alatt működésképtelen notify-t adna. 3.1.x és 3.3.x alatt is helyes.
#if !defined(CONFIG_NIMBLE_ENABLED)
  pCharacteristic->addDescriptor(new BLE2902());
#endif
  pService->start();

  otaInitService(pServer);

  BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  pAdvertising->setMinPreferred(0x06);
  pAdvertising->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();

  DBG("BLE ready");

  lastActivityTime = millis();
  lastHeartbeat = millis();

  printBootDiag();

  DBG("Boot done");
  digitalWrite(LED_YELLOW, LOW);
}

// ===================== LOOP =====================
void loop() {
  esp_task_wdt_reset();
  unsigned long now2 = millis();

#if RELAY_TEST_AT_BOOT
  // [FIX-ESP-59] A bootteszt MAIN-t OFF-ra kényszerít és végigkapcsolja a fan-reléket:
  // ezt CSAK közvetlenül boot után, üzem előtt szabad megtenni. Eddig csak a
  // `!bleConnected` kapuzta, a `relayTestPending` viszont a végtelenségig függőben
  // maradt: ha a telefon a boot utáni pillanatban visszacsatlakozott, a teszt ÓRÁKKAL
  // később, az első BLE-bontáskor sült el — járó görgő mellett lekapcsolta a MAIN-t,
  // miközben a `mainActive` igaz maradt. Ez azonnal téves "STUCK"-ot ad
  // (NC-bekötésnél az AC hiánya = "behúzva"), tehát indokolatlan failsafe + leállás.
  // Most: időablak + üzemi állapot esetén elévül.
  if (relayTestPending) {
    if (relaySenseBypass || mainActive || relaysEnabled || now2 >= RELAY_TEST_WINDOW_MS) {
      relayTestPending = false;  // elévült/nem futtatható — csendben elhagyjuk
    } else if (!bleConnected) {
      relayTestPending = false;
      relayBootTest();
    }
  }
#endif

  // [FIX-ESP-39] ERR_RESTORE_CLEAR_MS stabil futás után nullázzuk a hibás-reset számlálót (csak a gyors, ismétlődő reseteket számoljuk)
  if (!errRestoreCleared && now2 >= ERR_RESTORE_CLEAR_MS) {
    errRestoreCount = 0;
    errRestoreMagic = ERR_RESTORE_MAGIC;
    errRestoreCleared = true;
  }

  // OTA health-check: csak OTA_VERIFY_HEALTHY_MS stabil futás után validál; ha előbb újraindul, a bootloader visszagörget
  if (otaPendingVerify && now2 >= OTA_VERIFY_HEALTHY_MS) {
    esp_err_t r = esp_ota_mark_app_valid_cancel_rollback();
    otaPendingVerify = false;
    if (r == ESP_OK) {
      DBG("OTA health-check OK → firmware VALID (rollback lemondva)");
      logStableVersion();  // OTA után stabil → rögzítjük a verziót
    } else {
      DBG_P("OTA mark valid FAILED: ");
      DBG_VLN(esp_err_to_name(r));
    }
  }

  if (now2 - lastCheck >= checkInterval) {
    lastCheck = now2;
    stateMachineStep();
  }

#if FAN_SENSE_ENABLE
  if (!relaySenseBypass) {
    if (!otaIsRunning()) monitorFanRelays();
  }
#endif

  otaLoop();

  if (!otaIsRunning()) handleDiagRequest();

  if (!otaIsRunning()) saveZoneToNvsIfStable();
}

// ===================== STATE MACHINE =====================
void stateMachineStep() {
  if (otaIsRunning()) {
    return;
  }

  switch (currentState) {
    case STATE_NORMAL:
      normalMode();
      break;
    case STATE_FAILSAFE:
      failSafeMode();
      break;
  }
}

void normalMode() {
  unsigned long nowNormalMode = millis();

  failStartSet = false;
  failStart = 0;

  // Aktivitás: MAIN be ÉS megy a ventilátor — automatikus (BLE) módban BLE-kapcsolat is kell, manuál módban nem. A roller önmagában nem aktivitás.
  bool hasActivity =
    mainActive && ((bleConnected && currentZone != 0) || (manualMode && manualZoneIndex != 0));

  bool prevActive = wasActive;
  wasActive = hasActivity;

  if (hasActivity && !prevActive) {
    DBG("Activity detected");
  }

  if (hasActivity) {
    lastActivityTime = nowNormalMode;
  }

  static Timer inactivityTimer{ 0, INACTIVITY_MS };

  if (hasActivity) {
    inactivityTimer.last = nowNormalMode;
  }

  if (inactivityTimer.elapsed(nowNormalMode)) {
    if (!bleConnected && !manualMode) {
      DBG("Idle → sleep");
      enterDeepSleep("idle-timeout");
    }
  }

  static Timer printTimer2{ 0, printInterval };
  if (printTimer2.elapsed(nowNormalMode)) {
    unsigned long diff = nowNormalMode - lastActivityTime;
    long remainingMs = (long)INACTIVITY_MS - (long)diff;
    if (remainingMs < 0) remainingMs = 0;
    long remainingMin = remainingMs / 60000;

    static long lastPrintedMin = -1;
    if (remainingMin != lastPrintedMin) {
      lastPrintedMin = remainingMin;
      DBG_P("To sleep (min): ");
      DBG_VLN(remainingMin);
      DBG_P("Free heap: ");
      DBG_V(ESP.getFreeHeap());
      DBG_P(" / min: ");
      DBG_VLN(ESP.getMinFreeHeap());
    }
  }

  static bool lowHeapLogged = false;
  uint32_t freeHeapNow = ESP.getFreeHeap();
  if (freeHeapNow < LOW_HEAP_THRESHOLD) {
    if (!lowHeapLogged && !diagStreaming) {
      char e[72];
      snprintf(e, sizeof(e), "[lowmem] heap=%u min=%u t=%lus",
               (unsigned)freeHeapNow, (unsigned)ESP.getMinFreeHeap(),
               (unsigned long)(nowNormalMode / 1000));
      diagLog(e);
      DBG("LOW HEAP logged to diag");
      lowHeapLogged = true;
    }
  } else if (freeHeapNow > LOW_HEAP_THRESHOLD + 4096) {
    lowHeapLogged = false;
  }

  static Timer bleRestartTimer{ 0, BLE_RESTART_DELAY };
  static bool bleRestartMsgShownLocal = false;

  if (bleNeedsRestart && bleEnabled) {
    if (!bleRestartMsgShownLocal) {
      DBG("BLE restart start");
      bleRestartMsgShownLocal = true;
    }

    if (bleRestartTime == 0) {
      bleRestartTime = nowNormalMode;
      bleRestartTimer.last = nowNormalMode;
    }

    if (bleRestartTimer.elapsed(nowNormalMode)) {
      DBG("BLE restart done");
      pServer->getAdvertising()->start();
      bleNeedsRestart = false;
      bleRestartTime = 0;
      bleRestartMsgShownLocal = false;
    }

  } else {
    bleRestartMsgShownLocal = false;
  }

  static Timer bleZoneTimeout{ 0, BLE_ZONE_TIMEOUT_MS };
  static bool bleZoneTimeoutMsgShownLocal = false;

  if (!bleConnected && currentZone != 0 && !manualMode) {

    if (bleDisconnectTime != 0) {

      if (bleZoneTimeout.last == 0)
        bleZoneTimeout.last = bleDisconnectTime;

      if (!bleZoneTimeoutMsgShownLocal) {
        DBG("BLE lost, zone timeout start");
        bleZoneTimeoutMsgShownLocal = true;
      }

      if (bleZoneTimeout.elapsed(nowNormalMode)) {
        DBG("Zone timeout → all OFF");
        setFanZone(0, SRC_NONE);
        deactivateMain();
        disableRelays();
        bleZoneTimeoutMsgShownLocal = false;
      }
    }

  } else {
    bleZoneTimeout.last = nowNormalMode;
    bleZoneTimeoutMsgShownLocal = false;
  }

  button.tick();
  handleLEDs(nowNormalMode);
  handleZoneChange();
  handleBleCommand();

  int f1 = digitalRead(RELAY_FAN1);
  int f2 = digitalRead(RELAY_FAN2);
  int f3 = digitalRead(RELAY_FAN3);
  if ((f1 == LOW) + (f2 == LOW) + (f3 == LOW) >= 2) {
    char e[48];
    int n = snprintf(e, sizeof(e), "[relay]");
    if (f1 == LOW) n += snprintf(e + n, sizeof(e) - n, " 1");
    if (f2 == LOW) n += snprintf(e + n, sizeof(e) - n, " 2");
    if (f3 == LOW) n += snprintf(e + n, sizeof(e) - n, " 3");
    snprintf(e + n, sizeof(e) - n, " ACTIVE ST zone=%d", currentZone);
    DBG_VLN(e);
    if (!diagStreaming) diagLog(e);
    zeroStateForFailsafe();  // [FIX-ESP-33] nullázás MÉG a STATE_FAILSAFE előtt
    currentState = STATE_FAILSAFE;
    return;
  }

#if FAN_SENSE_ENABLE
  if (!relaySenseBypass) {
    checkFanRelayMismatch();
    if (currentState == STATE_FAILSAFE) return;
  }
#endif

  yield();
}

// [FIX-ESP-56] A perzisztens "görgő/fokozat aktív volt" jelzés nullázása NVS-ben.
// Ezt olvassa a boot-helyreállítás BROWNOUT/UNKNOWN/WDT reset után: ha 1 marad,
// az eszköz magától visszakapcsolja a görgőt — akkor is, amikor épp aludnia kéne.
void persistRelayStateOff() {
  if (otaIsRunning()) return;
  if (nvsLastSavedZone == 0 && nvsLastSavedMain == 0) return;
  fanPrefs.begin("fan", false);
  fanPrefs.putInt("zone", 0);
  fanPrefs.putInt("main", 0);
  fanPrefs.end();
  nvsLastSavedZone = 0;
  nvsLastSavedMain = 0;
  lastNvsSaveTime = millis();
}

void zeroStateForFailsafe() {
  portENTER_CRITICAL(&zoneMux);
  currentZone = 0;
  pendingZone = 0;
  zoneChanging = false;
  zoneChangeInProgress = false;
  savedZone = 0;
  savedZoneMagic = SAVED_ZONE_MAGIC;
  savedMain = 0;
  savedMainMagic = SAVED_MAIN_MAGIC;
  portEXIT_CRITICAL(&zoneMux);
  mainActive = false;
  nvsZonePending = false;

  persistRelayStateOff();
}

void zeroStateForBypass() {

  savedZone = 0;
  savedZoneMagic = SAVED_ZONE_MAGIC;
  savedMain = 0;
  savedMainMagic = SAVED_MAIN_MAGIC;

  persistRelayStateOff();
}

void failSafeMode() {
  if (!failStartSet) {
    failStart = millis();
    failStartSet = true;

    zeroStateForFailsafe();
    DBG("FAILSAFE entry → main+fan state zeroed (RTC+NVS)");
  }

  fanRelaysOff();
  digitalWrite(RELAY_MAIN, HIGH);
  digitalWrite(RELAY_EN, LOW);
  relaysEnabled = false;  // [FIX-ESP-60] a tápengedély fizikailag LOW → a flag se maradjon true

  unsigned long nowfailSafeMode = millis();

  static Timer failPrintTimer{ 0, 1000 };
  if (failPrintTimer.elapsed(nowfailSafeMode)) {
    DBG("FAILSAFE active");
  }

  if (nowfailSafeMode - lastBlink >= blinkInterval) {
    lastBlink = nowfailSafeMode;
    blinkState = !blinkState;

    digitalWrite(LED_RED, blinkState);
    digitalWrite(LED_YELLOW, blinkState);
  }

  if (nowfailSafeMode - failStart >= FAILSAFE_TIMEOUT_MS) {
    DBG("Failsafe timeout → sleep");
    enterDeepSleep("failsafe-timeout");
  }
}

// ===================== BLE CMD HANDLER =====================
void handleBleCommand() {
  int zone = -1;
  int mainCmd = -1;

  portENTER_CRITICAL(&bleCmdMux);
  if (bleCmd.hasCommand) {
    zone = bleCmd.zone;
    bleCmd.hasCommand = false;
  }
  if (bleCmd.hasMainCommand) {
    mainCmd = bleCmd.mainCommand;
    bleCmd.hasMainCommand = false;
  }
  portEXIT_CRITICAL(&bleCmdMux);

  if (zone != -1) {
    setFanZone(zone, SRC_BLE);
  }

  if (mainCmd != -1) {
    if (mainCmd == 1) {
      if (!relaysEnabled) enableRelays();
      activateMain();
    } else {
      deactivateMain();
      if (currentZone == 0) disableRelays();
    }
  }
}

// ===================== ZONE CONTROL =====================
// Megj.: a zónaváltás MAIN nélkül is megengedett (a relé kapcsol); a téves
// reléfigyelést MAIN OFF alatt a checkFanRelayMismatch kezeli (!mainActive → kilép).
void setFanZone(int zone, CommandSource source) {
  if (otaIsRunning()) {
    DBG("Zone change blocked (OTA)");
    return;
  }

  unsigned long now = millis();
#if DEBUG
  int fromZone = currentZone;
#endif

  portENTER_CRITICAL(&zoneMux);

  // [FIX-ESP-53] wrap-safe forrás-zárolás (a monitorFanRelays grace-ével azonos idióma):
  // a nyers now/sourceLockedUntil összevetés a millis()-túlcsorduláskor egy 2 s-os
  // ablakra megfordult volna (a zárolás korán lejár, majd tévesen újra aktívnak látszik).
  // A számítás a kritikus szekción BELÜL marad, mint az eredeti összehasonlítás.
  const bool sourceLockActive = ((long)(sourceLockedUntil - now) > 0);

  if (!sourceLockActive) {
    activeSource = SRC_NONE;
  }

  if (zoneChanging || zoneChangeInProgress) {
    portEXIT_CRITICAL(&zoneMux);
    DBG("Zone change blocked");
    return;
  }

  zoneChanging = true;
  zoneChangeInProgress = true;

  if (activeSource != SRC_NONE && source != SRC_NONE && sourceLockActive) {
    if (source < activeSource) {
      zoneChanging = false;
      zoneChangeInProgress = false;
      portEXIT_CRITICAL(&zoneMux);
      DBG("Zone change rejected");
      return;
    }
  }

  if (source != SRC_NONE) {
    activeSource = source;
    sourceLockedUntil = now + SOURCE_LOCK_MS;
  }

  if (zone < 0) zone = 0;
  if (zone > 3) zone = 3;

  if (zone == currentZone) {
    zoneChanging = false;
    zoneChangeInProgress = false;
    portEXIT_CRITICAL(&zoneMux);
    DBG("Zone already set");
    return;
  }

  fanRelaysOff();

  pendingZone = zone;
  zoneChangeStart = now;

  portEXIT_CRITICAL(&zoneMux);

#if FAN_SENSE_ENABLE
  fanSenseGraceUntil = now + RELAY_SWITCH_DELAY_MS + FAN_SENSE_GRACE_MS;
#endif

  DBG_P("Zone change: ");
  DBG_V(fromZone);
  DBG_P(" -> ");
  DBG_VLN(zone);
}

void handleZoneChange() {
  unsigned long nowhandleZoneChange = millis();

  unsigned long localZoneChangeStart;
  int localPendingZone;

  portENTER_CRITICAL(&zoneMux);
  if (!zoneChangeInProgress) {
    portEXIT_CRITICAL(&zoneMux);
    return;
  }
  localZoneChangeStart = zoneChangeStart;
  localPendingZone = pendingZone;
  portEXIT_CRITICAL(&zoneMux);

  // Moduláris (wrap-safe) eltelt-idő: millis() túlcsordulásnál is pontos,
  // a break-before-make védőidő sosem maradhat ki
  if ((unsigned long)(nowhandleZoneChange - localZoneChangeStart) < RELAY_SWITCH_DELAY_MS) {
    return;
  }

  portENTER_CRITICAL(&zoneMux);

  currentZone = localPendingZone;

  savedZone = localPendingZone;
  savedZoneMagic = SAVED_ZONE_MAGIC;

  switch (localPendingZone) {
    case 1: digitalWrite(RELAY_FAN1, LOW); break;
    case 2: digitalWrite(RELAY_FAN2, LOW); break;
    case 3: digitalWrite(RELAY_FAN3, LOW); break;
    case 0: break;
  }

  zoneChanging = false;
  zoneChangeInProgress = false;

  portEXIT_CRITICAL(&zoneMux);

#if FAN_SENSE_ENABLE
  fanSenseGraceUntil = nowhandleZoneChange + FAN_SENSE_GRACE_MS;
  fanMismatchSince[0] = fanMismatchSince[1] = fanMismatchSince[2] = 0;
  fanNoacWarned[0] = fanNoacWarned[1] = fanNoacWarned[2] = false;
#endif

  zoneStableSince = nowhandleZoneChange;
  nvsZonePending = true;

  switch (localPendingZone) {
    case 1: DBG("Fan1 ON (33%)"); break;
    case 2: DBG("Fan2 ON (66%)"); break;
    case 3: DBG("Fan3 ON (100%)"); break;
    case 0: DBG("All fans OFF"); break;
  }
}

void saveZoneToNvsIfStable() {
  unsigned long now = millis();

  int z;
  portENTER_CRITICAL(&zoneMux);
  z = currentZone;
  portEXIT_CRITICAL(&zoneMux);
  int mainNow = mainActive ? 1 : 0;  // bool, atomi olvasás

  bool stableSave = nvsZonePending && (now - zoneStableSince >= NVS_SAVE_STABLE_MS);
  bool forceSave = (now - lastNvsSaveTime >= NVS_FORCE_SAVE_MS) && (z != nvsLastSavedZone);
  if (stableSave) nvsZonePending = false;  // a stabil-pending elintézve, nem pörgünk rá

  bool zoneNeedsWrite = (stableSave || forceSave) && (z != nvsLastSavedZone);
  bool mainNeedsWrite = (mainNow != nvsLastSavedMain);

  if (!zoneNeedsWrite && !mainNeedsWrite) return;

  fanPrefs.begin("fan", false);
  if (zoneNeedsWrite) {
    fanPrefs.putInt("zone", z);
    nvsLastSavedZone = z;
    lastNvsSaveTime = now;
  }
  if (mainNeedsWrite) {
    fanPrefs.putInt("main", mainNow);
    nvsLastSavedMain = mainNow;
  }
  fanPrefs.end();

  if (zoneNeedsWrite) {
    DBG_P("NVS zone saved: ");
    DBG_V(z);
    DBG_VLN((forceSave && !stableSave) ? " (force 5min)" : " (stable 30s)");
  }
  if (mainNeedsWrite) {
    DBG_P("NVS main saved: ");
    DBG_VLN(mainNow);
  }
}

// ===================== FAN RELÉ KIMENET FIGYELÉS (H11AA1M) =====================
#if FAN_SENSE_ENABLE
void monitorFanRelays() {
  unsigned long now = millis();
  bool inGrace = ((long)(fanSenseGraceUntil - now) > 0);

  for (int i = 0; i < 3; i++) {
    int raw = digitalRead(fanSensePins[i]);

    // AC a sense-ágon = volt-e LOW (opto-vezetés) az ablakban; a HIGH-tüskéket ignoráljuk
    if (raw == LOW) {
      fanSenseLastLow[i] = now;
      fanSenseSeen[i] = true;
    }
    bool acOnSense = fanSenseSeen[i] && ((unsigned long)(now - fanSenseLastLow[i]) < AC_SENSE_WINDOW_MS);

    // Bekötés-függő leképezés „relé behúzva"-ra (NC: AC ⇒ nincs behúzva; NO: AC ⇒ behúzva).
#if FAN_SENSE_AC_MEANS_ENGAGED
    bool rawEngaged = acOnSense;
#else
    bool rawEngaged = !acOnSense;
#endif

    if (rawEngaged != fanRelayEngaged[i]) {
      if (fanSenseChangeSince[i] == 0) fanSenseChangeSince[i] = now;
      if ((unsigned long)(now - fanSenseChangeSince[i]) >= AC_SENSE_DEBOUNCE_MS) {
        fanRelayEngaged[i] = rawEngaged;
        fanSenseChangeSince[i] = 0;
        if (!inGrace) {
          DBG_P("Relay");
          DBG_V(i + 1);
          DBG_VLN(rawEngaged ? F(" ACTIVE") : F(" INACTIVE"));
        }
      }
    } else {
      fanSenseChangeSince[i] = 0;
    }
  }
}

void checkFanRelayMismatch() {

  if (relaySenseBypass) return;  // teljes tiltás
  unsigned long now = millis();

  // RELAY_MAIN OFF → nincs táp/AC a fan-ágakon, a sense értelmezhetetlen
  // (AC_MEANS_ENGAGED=0-nál minden "behúzva"-nak látszik → téves STUCK). Ne értékeljünk.
  if (!mainActive) {
    for (int i = 0; i < 3; i++) {
      fanMismatchSince[i] = 0;
      fanNoacWarned[i] = false;
    }
    return;
  }

  bool inGrace = ((long)(fanSenseGraceUntil - now) > 0);

  for (int i = 0; i < 3; i++) {
    bool expectedEngaged = relaysEnabled && (currentZone == (i + 1));
    bool engaged = fanRelayEngaged[i];  // TRUE = a relé behúzva (NC-érzékelés)

    bool stuck = (!expectedEngaged && engaged);  // a zóna OFF-ot vár, de a relé BEHÚZVA (NC nyitva) → beragadt relé
    bool noac = (expectedEngaged && !engaged);   // a zóna ON-t vár, de a relé NINCS behúzva → relé/biztosíték/hálózat hiba

#if FAN_SENSE_FAILSAFE_ON_STUCK
    if (stuck && !inGrace) {
      char e[48];
      snprintf(e, sizeof(e), "[relay] %d STUCK zone=%d", i + 1, currentZone);
      DBG_VLN(e);
      if (!diagStreaming) diagLog(e);

      zeroStateForFailsafe();  // [FIX-ESP-33] nullázás MÉG a STATE_FAILSAFE előtt
      currentState = STATE_FAILSAFE;
      return;
    }
#endif

#if FAN_SENSE_WARN_ON_NOAC
    if (noac && !inGrace) {
      if (fanMismatchSince[i] == 0) fanMismatchSince[i] = now;
      if (!fanNoacWarned[i] && (unsigned long)(now - fanMismatchSince[i]) >= FAN_SENSE_MISMATCH_CONFIRM_MS) {
        DBG_P("FIGYELEM: Fan");
        DBG_V(i + 1);
        DBG(" zona ON, de a rele nincs behuzva (nincs NC-visszajelzes) - tovabb fut");

        fanNoacWarned[i] = true;  // egyszer figyelmeztetünk, amíg fennáll
      }
    } else {
      fanMismatchSince[i] = 0;
      fanNoacWarned[i] = false;
    }
#endif
  }
}
#endif  // FAN_SENSE_ENABLE

// ===================== MAIN CONTROL =====================
void activateMain() {
  digitalWrite(RELAY_MAIN, LOW);
  mainActive = true;
  savedMain = 1;
  savedMainMagic = SAVED_MAIN_MAGIC;
#if FAN_SENSE_ENABLE
  // Táp visszatért → az AC-nak idő kell stabilizálódni; grace + mismatch-állapot nullázás
  fanSenseGraceUntil = millis() + FAN_SENSE_GRACE_MS;
  fanMismatchSince[0] = fanMismatchSince[1] = fanMismatchSince[2] = 0;
  fanNoacWarned[0] = fanNoacWarned[1] = fanNoacWarned[2] = false;
#endif
  DBG("Main ON");
}

void deactivateMain() {
  digitalWrite(RELAY_MAIN, HIGH);
  mainActive = false;
  savedMain = 0;
  savedMainMagic = SAVED_MAIN_MAGIC;
  // MAIN OFF → a ventilátor táp nélkül marad: fan-relék OFF + zóna nullázása (folyamatban lévő váltás törlése is)
  fanRelaysOff();
  portENTER_CRITICAL(&zoneMux);
  currentZone = 0;
  pendingZone = 0;
  zoneChanging = false;
  zoneChangeInProgress = false;
  savedZone = 0;
  savedZoneMagic = SAVED_ZONE_MAGIC;
  portEXIT_CRITICAL(&zoneMux);
  DBG("Main OFF");
}

// ===================== RELAY CONTROL =====================
void enableRelays() {
  fanRelaysOff();
  digitalWrite(RELAY_MAIN, HIGH);
  delay(10);
  digitalWrite(RELAY_EN, HIGH);
  delay(10);
  relaysEnabled = true;
#if FAN_SENSE_ENABLE
  fanSenseGraceUntil = millis() + FAN_SENSE_GRACE_MS;
  fanMismatchSince[0] = fanMismatchSince[1] = fanMismatchSince[2] = 0;
  fanNoacWarned[0] = fanNoacWarned[1] = fanNoacWarned[2] = false;
#endif
  DBG("Relays ON");
}

void disableRelays() {
  fanRelaysOff();
  digitalWrite(RELAY_MAIN, HIGH);
  delay(10);
  digitalWrite(RELAY_EN, LOW);
  delay(10);
  relaysEnabled = false;
  mainActive = false;  // [FIX-ESP-56] tápengedély nélkül a görgő-relé fizikailag sem lehet behúzva
#if FAN_SENSE_ENABLE
  fanSenseGraceUntil = millis() + FAN_SENSE_GRACE_MS;
  fanMismatchSince[0] = fanMismatchSince[1] = fanMismatchSince[2] = 0;
  fanNoacWarned[0] = fanNoacWarned[1] = fanNoacWarned[2] = false;
#endif
  DBG("Relays OFF");
}

#if RELAY_TEST_AT_BOOT
#if FAN_SENSE_ENABLE
// ms ideig vár, közben AC-t mintázik (LOW = opto vezet). onFan = épp bekapcsolt fan
// indexe (-1 = egyik sem). MAIN OFF mellett bármilyen AC → beragadt MAIN; hogy melyik
// vonalon, azt a bekötés (FAN_SENSE_AC_MEANS_ENGAGED) dönti el.
static void relayTestWait(unsigned long ms, int onFan, int* acHits, int* totalSamples) {
  unsigned long t0 = millis();
  while ((millis() - t0) < ms) {
#if FAN_SENSE_AC_MEANS_ENGAGED
    // NO-bekötés: AC az ÉPP BEKAPCSOLT fan make-érintkezőjén → MAIN beragadt
    if (onFan >= 0) {
      (*totalSamples)++;
      if (digitalRead(fanSensePins[onFan]) == LOW) (*acHits)++;
    }
#else
    // NC-bekötés (bontó): AC bármely ÉPP KIKAPCSOLT fan bontó-érintkezőjén → MAIN beragadt
    for (int i = 0; i < 3; i++) {
      if (i != onFan) {
        (*totalSamples)++;
        if (digitalRead(fanSensePins[i]) == LOW) (*acHits)++;
      }
    }
#endif
    delayMicroseconds(500);
  }
}
#endif

// Bootkori relé-önteszt: FAN1→FAN2→FAN3 sorban be/ki, RELAY_MAIN OFF; ha a bontón AC → MAIN beragadt
void relayBootTest() {
  DBG("Relay boot-test: start (RELAY_MAIN kihagyva)");

  // Minden relé OFF (aktív-LOW → HIGH=OFF), RELAY_MAIN is OFF, majd táp be
  fanRelaysOff();
  digitalWrite(RELAY_MAIN, HIGH);
  delay(10);
  digitalWrite(RELAY_EN, HIGH);
  delay(10);

#if FAN_SENSE_ENABLE
  int acHits = 0;        // bontón mért AC-minták; MAIN OFF mellett bármilyen → beragadt MAIN
  int totalSamples = 0;  // [FIX-ESP-43] összes mintavétel (arányos küszöbhöz)
#endif

  const uint8_t fans[3] = { RELAY_FAN1, RELAY_FAN2, RELAY_FAN3 };
  for (int i = 0; i < 3; i++) {
    esp_task_wdt_reset();
    // Garancia: egyszerre csak EGY fan aktív — előbb mindet OFF, majd csak az egyet ON (a ciklusvégi GAP ad elengedési időt)
    fanRelaysOff();
    DBG_P("Relay test FAN");
    DBG_V(i + 1);
    DBG(" ON");
    digitalWrite(fans[i], LOW);  // csak ez az egy ON
#if FAN_SENSE_ENABLE
    relayTestWait(RELAY_TEST_ON_MS, i, &acHits, &totalSamples);  // fan i bekapcsolva
#else
    delay(RELAY_TEST_ON_MS);
#endif
    digitalWrite(fans[i], HIGH);  // OFF
#if FAN_SENSE_ENABLE
    relayTestWait(RELAY_TEST_GAP_MS, -1, &acHits, &totalSamples);  // mind kikapcsolva
#else
    delay(RELAY_TEST_GAP_MS);
#endif
  }

  // Biztos OFF + táp ki
  fanRelaysOff();
  digitalWrite(RELAY_MAIN, HIGH);
  delay(10);
  digitalWrite(RELAY_EN, LOW);
  relaysEnabled = false;
  mainActive = false;  // [FIX-ESP-59] a teszt a MAIN-t OFF-ra hajtotta → az állapot ne hazudjon
#if FAN_SENSE_ENABLE
  fanSenseGraceUntil = millis() + FAN_SENSE_GRACE_MS;
  if (totalSamples > 0 && acHits > totalSamples / 10) {  // [FIX-ESP-43] arányos küszöb: >10% hit → beragadt MAIN
    char e[64];
    snprintf(e, sizeof(e), "[relay] main stuck! hits=%d/%d", acHits, totalSamples);
    DBG_VLN(e);
    diagLog(e);
    zeroStateForFailsafe();  // beragadt MAIN → failsafe (mint a relé-mismatchnél)
    currentState = STATE_FAILSAFE;
  }
#endif
  DBG("Relay boot-test: done");
}
#endif

// ===================== LED HANDLING =====================
// Szabályos villogás: LED_BLINK_INTERVAL fél-periódussal billeg.
static void ledBlink(uint8_t pin, unsigned long now, unsigned long& lastToggle, bool& state) {
  if (now - lastToggle > LED_BLINK_INTERVAL) {
    state = !state;
    digitalWrite(pin, state ? HIGH : LOW);
    lastToggle = now;
  }
}

// Életjel: HEARTBEAT_INTERVAL-onként egy HEARTBEAT_PULSE hosszúságú felvillanás.
static void ledHeartbeat(uint8_t pin, unsigned long now, unsigned long& lastAt, bool& inPulse) {
  if (!inPulse) {
    if (now - lastAt >= HEARTBEAT_INTERVAL) {
      digitalWrite(pin, HIGH);
      inPulse = true;
      lastAt = now;
    } else {
      digitalWrite(pin, LOW);
    }
  } else if (now - lastAt >= HEARTBEAT_PULSE) {
    digitalWrite(pin, LOW);
    inPulse = false;
  }
}

// A piros és a sárga ág eddig szó szerint ugyanazt a villogás/életjel-szerkezetet
// másolta le, csak más lábbal és más állapotváltozókkal (~100 sor). A viselkedés
// azonos, csak a két minta van megnevezve.
void handleLEDs(unsigned long currentMillis) {
  if (otaIsRunning()) return;  // OTA alatt az otaLoop() villogtatja a LED-eket

  // PIROS = BLE-állapot
  if (bleConnected) {
    digitalWrite(LED_RED, HIGH);  // kapcsolat él → folyamatos
  } else if (manualMode) {
    digitalWrite(LED_RED, LOW);  // kézi mód → BLE szándékosan ki
  } else if (bleEnabled) {       // (ide már csak !bleConnected mellett jutunk)
    ledBlink(LED_RED, currentMillis, lastRedToggle, redLedState);  // hirdet, kapcsolatra vár
  } else {
    ledHeartbeat(LED_RED, currentMillis, lastHeartbeat_red, heartbeatPulse_red);
  }

  // SÁRGA = relé-/görgő-állapot
  if (relaysEnabled && mainActive) {
    ledBlink(LED_YELLOW, currentMillis, lastYellowToggle, yellowLedState);
  } else {
    ledHeartbeat(LED_YELLOW, currentMillis, lastHeartbeat, heartbeatPulse);
  }
}

// ===================== DEEP SLEEP =====================
void enterDeepSleep(const char* reason) {
  DBG("====================================");
  DBG("Enter deep sleep");
  DBG_P("Reason: ");
  DBG_VLN(reason);
  DBG("====================================");

  // OTA health-check: a kontrollált deep sleep elérése = működő firmware → validálunk (PENDING_VERIFY-ben ébredés különben rollbackot váltana)
  if (otaPendingVerify) {
    esp_ota_mark_app_valid_cancel_rollback();
    otaPendingVerify = false;
    DBG("OTA health-check OK (pre-sleep) → firmware VALID (rollback lemondva)");
    logStableVersion();  // OTA után stabil → rögzítjük a verziót
  }

  if (bleEnabled) {
    DBG("BLE stop");
    if (bleConnected) {
      pServer->disconnect(pServer->getConnId());  // [FIX-ESP-58] nem hardkódolt 0
      delay(500);  // [FIX-ESP-23] BLE stack teljes kimaradása
    }
    BLEDevice::stopAdvertising();
    delay(300);  // [FIX-ESP-23] advertising shutdown
    bleConnected = false;
    bleEnabled = false;
  }

  DBG("Relays OFF before sleep");
  // [FIX-ESP-56] A deep sleep KONTROLLÁLT kikapcsolás: a görgőt/fokozatot nem csak
  // fizikailag kell lekapcsolni, hanem a "volt-e aktív" jelzést is törölni kell
  // (RTC_NOINIT + NVS). Enélkül a jelzés túléli az alvást, és egy későbbi
  // BROWNOUT/WDT/UNKNOWN reset boot-helyreállítása visszakapcsolná a görgőt —
  // az eszköz "alszik", a relé mégis meghúzva marad.
  deactivateMain();         // RELAY_MAIN + fan-relék OFF, savedMain/savedZone = 0 (RTC)
  persistRelayStateOff();   // NVS: main=0, zone=0
  disableRelays();          // tápengedély LOW
  delay(200);  // [FIX-ESP-23] GPIO settle time relé OFF után

  DBG("LEDs OFF");
  digitalWrite(LED_RED, LOW);
  digitalWrite(LED_YELLOW, LOW);
  delay(200);  // [FIX-ESP-23] GPIO settle time LED OFF után

  DBG("Deep sleep on BTN");
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);  // korábbi wakeup sourceok törlése
  pinMode(BUTTON_PIN, INPUT_PULLUP);                      // [FIX-ESP-55] definiált szint az ébresztő lábon (a pad-hold ezt is rögzíti)
  esp_deep_sleep_enable_gpio_wakeup(BIT(BUTTON_PIN), ESP_GPIO_WAKEUP_GPIO_LOW);

  // [FIX-ESP-55] Relé-lábak rögzítése: alvás alatt is HAJTOTT marad a
  // RELAY_EN=LOW + minden relé HIGH (OFF) szint, nem lebeg.
  relayPadsHoldEnable();

  delay(500);  // [FIX-ESP-23] ESP stabilizáció a deep sleep előtt
#if SERIAL_ENABLED
  Serial.flush();
#endif
  esp_deep_sleep_start();
}

// ===================== OTA LOOP =====================
void otaLoop() {
  if (!pOtaTx || !pOtaRx) return;

  // [FIX-ESP-53] wrap-safe határidő (v.ö. [FIX-ESP-49]): a nyers `millis() >= deadline`
  // a millis()-túlcsordulás körül azonnal igazzá válna (az 5 s várakozás kimaradna)
  if (otaPendingReboot && (long)(millis() - otaRebootAt) >= 0) {
    rebootEspWithReason("OTA done");
  }

  if (otaMode != OTA_NORMAL_MODE) {
    unsigned long now = millis();
    if (now - otaLedTimer >= 50) {
      otaLedTimer = now;
      otaLedState = !otaLedState;
      digitalWrite(LED_RED, otaLedState ? HIGH : LOW);
      digitalWrite(LED_YELLOW, otaLedState ? LOW : HIGH);
    }
  }

  switch (otaMode) {

    case OTA_NORMAL_MODE:
      if (otaDeviceConnected) {
        if (otaSendSize) {
          unsigned long x = FLASH.totalBytes();
          unsigned long y = FLASH.usedBytes();
          uint8_t fSize[] = {
            OTA_TX_FS_INFO,
            (uint8_t)(x >> 16),
            (uint8_t)(x >> 8),
            (uint8_t)x,
            (uint8_t)(y >> 16),
            (uint8_t)(y >> 8),
            (uint8_t)y
          };
          pOtaTx->setValue(fSize, 7);
          pOtaTx->notify();
          delay(50);
          otaSendSize = false;
        }
      }
      break;

    case OTA_UPDATE_MODE:

      if (otaWriteFile) {
        if (!otaBuf) {
          otaWriteFile = false;
          break;
        }
        uint8_t* buf = otaBuf;
        int blen = otaWriteLen;

        uint32_t crc = crc32_zlib(buf, (size_t)blen);

        if (crc != otaExpectedCrc) {
          otaWriteFile = false;
          otaPartRetry++;
          DBG_P("OTA CRC fail part=");
          DBG_V(otaCur);
          DBG_P(" got=0x");
          DBG_V(crc, HEX);
          DBG_P(" exp=0x");
          DBG_V(otaExpectedCrc, HEX);
          DBG_P(" try=");
          DBG_VLN(otaPartRetry);

          if (otaPartRetry <= MAX_PART_RETRY) {
            char e[72];
            snprintf(e, sizeof(e), "[ota] crc retry part=%d try=%d", otaCur, otaPartRetry);
            diagLog(e);
            otaExpectedPart = otaCur;  // [FIX-ESP-35] ugyanezt a partot várjuk vissza
            uint8_t rq[] = { OTA_TX_REQ_PART, (uint8_t)(otaCur / 256), (uint8_t)(otaCur % 256) };
            pOtaTx->setValue(rq, 3);
            pOtaTx->notify();
            delay(50);
          } else {
            otaAbort("CRC fail part " + String(otaCur));
          }
          break;
        }

        otaPartRetry = 0;
        otaWriteBinary(FLASH, "/update.bin", buf, blen);  // otaWriteFile=false, otaReceivedBytes += blen

        if (otaMode != OTA_UPDATE_MODE) break;

        if (otaCur + 1 == otaParts) {
          uint8_t com[] = { OTA_TX_COMPLETE, (uint8_t)((otaCur + 1) / 256), (uint8_t)((otaCur + 1) % 256) };
          pOtaTx->setValue(com, 3);
          pOtaTx->notify();
          delay(50);
          if (otaBuf) {
            free(otaBuf);
            otaBuf = nullptr;
          }
          otaMode = OTA_INSTALL_MODE;
        } else {
          otaExpectedPart = otaCur + 1;  // [FIX-ESP-35] ezt várjuk vissza
          uint8_t rq[] = { OTA_TX_REQ_PART, (uint8_t)((otaCur + 1) / 256), (uint8_t)((otaCur + 1) % 256) };
          pOtaTx->setValue(rq, 3);
          pOtaTx->notify();
          delay(50);
        }
      }

      break;

    case OTA_INSTALL_MODE:

      if (otaInstallWaiting) {
        if ((long)(millis() - otaInstallWaitUntil) >= 0) {  // [FIX-ESP-53] wrap-safe
          otaInstallWaiting = false;
          if (otaReceivedBytes == otaTotalBytes && otaTotalBytes > 0) {
            uint32_t savedTotal = otaTotalBytes;
            otaTotalBytes = 0;
            otaReceivedBytes = 0;
            updateFromFS(FLASH);
            (void)savedTotal;  // ha esetleg debug-hoz kéne
          } else {
            // [FIX-ESP-41] size mismatch → abort instead of infinite retry loop
            char msg[64];
            snprintf(msg, sizeof(msg), "size mismatch exp=%u got=%u",
                     (unsigned)otaTotalBytes, (unsigned)otaReceivedBytes);
            otaAbort(msg);
          }
        }
        break;  // Várakozás alatt nem futtatjuk le az alábbi logikát
      }

      if (otaReceivedBytes == otaTotalBytes && otaTotalBytes > 0) {
        DBG("OTA file complete");
        otaInstallWaiting = true;
        otaInstallWaitUntil = millis() + 2000;

      } else if (otaTotalBytes > 0) {
        DBG("OTA incomplete");
        DBG_P("Expected: ");
        DBG_VLN(otaTotalBytes);
        DBG_P("Received: ");
        DBG_VLN(otaReceivedBytes);
        // [FIX-ESP-41] Defensive: abort immediately on size mismatch instead of silent retry loop
        char msg[64];
        snprintf(msg, sizeof(msg), "size mismatch exp=%u got=%u",
                 (unsigned)otaTotalBytes, (unsigned)otaReceivedBytes);
        otaAbort(msg);
      }
      break;
  }
}
