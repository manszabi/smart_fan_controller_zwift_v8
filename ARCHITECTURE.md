# Smart Fan Controller v8 - Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          MAIN THREAD                                    │
│                  (Qt event loop + signal handling)                       │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                    HUD Window (PySide6)                           │  │
│  │                  Star Trek LCARS Theme                            │  │
│  │          500ms refresh, sound effects, always-on-top              │  │
│  └──────────────────────────┬────────────────────────────────────────┘  │
│                             │ reads UISnapshot (thread-safe)            │
└─────────────────────────────┼───────────────────────────────────────────┘
                              │
┌─────────────────────────────┼───────────────────────────────────────────┐
│                    ASYNCIO THREAD (daemon)                               │
│                                                                         │
│  ┌──────────────────────────┴────────────────────────────────────────┐  │
│  │                    FanController.run()                             │  │
│  │                   (main orchestrator)                              │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─────────────────── INPUT HANDLERS ──────────────────────────────┐   │
│  │                                                                  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │   │
│  │  │  ANT+ Input  │  │  BLE Input   │  │   Zwift Input         │  │   │
│  │  │  (Thread)    │  │  (async)     │  │   (async)             │  │   │
│  │  │              │  │              │  │                       │  │   │
│  │  │ ┌──────────┐ │  │ ┌──────────┐ │  │  ┌─────────────────┐ │  │   │
│  │  │ │Power     │ │  │ │Power     │ │  │  │ ZwiftAuth       │ │  │   │
│  │  │ │Meter     │ │  │ │Service   │ │  │  │ (OAuth2)        │ │  │   │
│  │  │ │(openant) │ │  │ │(0x1818)  │ │  │  ├─────────────────┤ │  │   │
│  │  │ └──────────┘ │  │ └──────────┘ │  │  │ ZwiftAPIClient  │ │  │   │
│  │  │ ┌──────────┐ │  │ ┌──────────┐ │  │  │ (HTTPS polling) │ │  │   │
│  │  │ │Heart     │ │  │ │HR        │ │  │  ├─────────────────┤ │  │   │
│  │  │ │Rate      │ │  │ │Service   │ │  │  │ProtobufDecoder  │ │  │   │
│  │  │ │(openant) │ │  │ │(0x180D)  │ │  │  │ → queue         │ │  │   │
│  │  │ └──────────┘ │  │ └──────────┘ │  │  └─────────────────┘ │  │   │
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
│  │  │              PROCESSING PIPELINE                         │    │   │
│  │  │                                                          │    │   │
│  │  │  ┌──────────────────┐     ┌──────────────────┐          │    │   │
│  │  │  │ PowerProcessor   │     │  HRProcessor     │          │    │   │
│  │  │  │                  │     │                   │          │    │   │
│  │  │  │ PowerAverager    │     │  HRAverager       │          │    │   │
│  │  │  │ (rolling mean)   │     │  (rolling mean)   │          │    │   │
│  │  │  │       │          │     │       │           │          │    │   │
│  │  │  │       ▼          │     │       ▼           │          │    │   │
│  │  │  │ zone_for_power() │     │ zone_for_hr()    │          │    │   │
│  │  │  │       │          │     │       │           │          │    │   │
│  │  │  └───────┼──────────┘     └───────┼───────────┘          │    │   │
│  │  │          │                        │                      │    │   │
│  │  │          ▼                        ▼                      │    │   │
│  │  │  ┌────────────────────────────────────────────┐          │    │   │
│  │  │  │         apply_zone_mode()                  │          │    │   │
│  │  │  │  (power_only / hr_only / higher_wins)      │          │    │   │
│  │  │  └────────────────────┬───────────────────────┘          │    │   │
│  │  │                       │                                  │    │   │
│  │  │                       ▼                                  │    │   │
│  │  │  ┌────────────────────────────────────────────┐          │    │   │
│  │  │  │         CooldownController                 │          │    │   │
│  │  │  │  Zone UP   → instant                       │          │    │   │
│  │  │  │  Zone DOWN → cooldown timer                │          │    │   │
│  │  │  │  Adaptive: halve (big drop) / double       │          │    │   │
│  │  │  └────────────────────┬───────────────────────┘          │    │   │
│  │  │                       │                                  │    │   │
│  │  │  ┌────────────────────┴───────────────────────┐          │    │   │
│  │  │  │         DropoutChecker                     │          │    │   │
│  │  │  │  No data > timeout → Z0 + reset averagers  │          │    │   │
│  │  │  └────────────────────┬───────────────────────┘          │    │   │
│  │  └───────────────────────┼──────────────────────────────────┘    │   │
│  │                          │                                       │   │
│  └──────── PROCESSING ──────┼───────────────────────────────────────┘   │
│                             │                                           │
│                             ▼                                           │
│                       zone_queue (0-3)                                  │
│                             │                                           │
│  ┌──────────────────────────┼───────────────────────────────────────┐   │
│  │                OUTPUT    ▼                                       │   │
│  │  ┌────────────────────────────────────────────┐                 │   │
│  │  │       BLEFanOutputController               │                 │   │
│  │  │                                            │                 │   │
│  │  │  scan → connect → authenticate (PIN)       │                 │   │
│  │  │  → write "LEVEL:N" to GATT (FFE0/FFE1)    │                 │   │
│  │  │  → auto-reconnect on disconnect            │                 │   │
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
              │  BLE Server (FFE0)     │
              │  "LEVEL:N" → Relays    │
              │                        │
              │  Z0: All OFF           │
              │  Z1: FAN1 (33%)        │
              │  Z2: FAN1+FAN2 (66%)   │
              │  Z3: FAN1+2+3 (100%)   │
              │                        │
              │  + OTA, WebSerial      │
              │  + WiFi AP/STA         │
              │  + Deep Sleep (30min)  │
              │  + Manual Button       │
              └────────────────────────┘
```

---

## Data Flow (Sequence)

```
Sensor/Zwift → Input Handler → raw queue → Processor → Averager → Zone Calc
    → Zone Mode → Cooldown → zone_queue → BLE Output → ESP32 → Fans
                                              ↓
                                         HUD (display)
```

1. **Input**: ANT+/BLE/Zwift provides raw power (W) and heart rate (bpm)
2. **Averaging**: Rolling mean buffer smooths data (configurable per source)
3. **Zone Calculation**: Power/HR mapped to zones 0-3 based on FTP/max HR
4. **Zone Mode**: Combines power + HR zones (`power_only`, `hr_only`, `higher_wins`)
5. **Cooldown**: Zone UP = instant, Zone DOWN = configurable delay with adaptive logic
6. **Dropout**: No data for N seconds → force Z0, reset averagers
7. **Output**: Send `LEVEL:N` over BLE to ESP32
8. **Display**: HUD shows live data every 500ms with sound effects

---

## Zone Definitions

| Zone | Fan Level | Power Range           | HR Range              |
|------|-----------|-----------------------|-----------------------|
| Z0   | OFF       | 0W (no pedaling)      | < resting HR          |
| Z1   | Low (33%) | 1W → z1_max% of FTP  | resting → z1_max% HR  |
| Z2   | Med (66%) | z1%+1 → z2_max% FTP  | z1%+1 → z2_max% HR   |
| Z3   | Max (100%)| > z2_max% of FTP     | > z2_max% of max HR   |

---

## Cooldown State Machine

```
                    new_zone > current
INACTIVE ──────────────────────────────────→ apply immediately
    │
    │ new_zone < current
    ▼
ACTIVE (timer running)
    │
    ├── drop ≥2 zones or zone→0  → HALVE cooldown time
    ├── pending zone rises       → DOUBLE cooldown time
    │
    └── timer expired            → apply pending zone → INACTIVE
```

---

## Threading Model

| Thread          | Type    | Purpose                                      |
|-----------------|---------|----------------------------------------------|
| Main            | -       | Qt event loop (HUD), signal handling          |
| AsyncioThread   | daemon  | All async tasks (BLE, processing, control)    |
| ANT+ Thread     | daemon  | openant blocking loop (bridges via queue)     |

**Synchronization:**
- `asyncio.Queue` - data flow between input handlers and processors
- `asyncio.Lock` - protects shared controller state
- `threading.Lock` - protects UISnapshot (HUD ↔ async thread)
- `threading.Event` - shutdown coordination

---

## Current File Structure

| File | Purpose |
|------|---------|
| `swift_fan_controller_new_v8_PySide6.py` | Main app (~5300 lines): all core logic, HUD, orchestration |
| `zwift_api_polling.py` | Zwift API polling: OAuth2, protobuf decode, UDP send |
| `esp32_fan_controller.ino` | ESP32-C3 firmware: BLE server, relay control, OTA |
| `settings.json` | User configuration (auto-created with defaults) |
| `settings.example.json` / `.jsonc` | Configuration templates |
| `CONFIGURATION.md` | Settings documentation |

---

## Planned Refactoring Structure

The monolithic main file (`swift_fan_controller_new_v8_PySide6.py`, ~5300 lines) and the
separate subprocess (`zwift_api_polling.py`) will be refactored into:

```
smart_fan_controller/
├── __init__.py              # Main class exports
├── __main__.py              # Entry point (python -m smart_fan_controller)
│
├── config/
│   ├── loader.py            # load_settings(), validation, defaults
│   └── schemas.py           # Setting dataclasses/TypedDicts
│
├── core/
│   ├── controller.py        # FanController orchestrator
│   ├── zones.py             # zone_for_power(), zone_for_hr(), apply_zone_mode()
│   ├── cooldown.py          # CooldownController state machine
│   ├── averager.py          # PowerAverager, HRAverager
│   ├── dropout.py           # dropout_checker_task
│   ├── power_processor.py   # power_processor_task
│   ├── hr_processor.py      # hr_processor_task
│   └── zone_controller.py   # zone_controller_task
│
├── input/
│   ├── antplus.py           # ANTPlusInputHandler
│   ├── ble_power.py         # BLEPowerInputHandler
│   ├── ble_hr.py            # BLEHRInputHandler
│   └── zwift.py             # ZwiftInputHandler (OAuth2 + polling + protobuf → queue)
│
├── output/
│   └── ble_fan.py           # BLEFanOutputController
│
├── hud/
│   ├── window.py            # HUDWindow (PySide6 LCARS UI)
│   ├── sounds.py            # Sound generation & playback
│   └── theme.py             # LCARS colors, fonts, styling
│
└── zwift/
    ├── auth.py              # ZwiftAuth (OAuth2 token management)
    ├── api_client.py        # ZwiftAPIClient (HTTPS calls)
    ├── protobuf_decoder.py  # ProtobufDecoder (binary protobuf parsing)
    └── polling.py           # Polling loop logic
```

### Key design decisions
- **No more subprocess/UDP**: Zwift polling runs in-process as an async task, communicates
  via `asyncio.Queue` just like ANT+ and BLE input handlers
- **No UDPBroadcaster**: Removed — data flows directly through queues
- **Processor tasks split out**: `power_processor_task`, `hr_processor_task`,
  `zone_controller_task` each in their own file under `core/`
- **`input/zwift.py`** uses classes from `zwift/` module but follows the same queue pattern
  as all other input handlers
- **BLE input duplication kept**: `ble_power.py` and `ble_hr.py` may share similar
  scan/connect logic but remain separate files for simplicity

### Benefits
- **Testability**: Pure functions (zones, cooldown) easily unit-testable
- **Readability**: Each file has a single responsibility (~200-500 lines)
- **Maintainability**: Changes isolated to relevant module
- **Reusability**: Input handlers, averagers, cooldown logic reusable independently
- **Unified communication**: All data sources use the same asyncio.Queue pattern
