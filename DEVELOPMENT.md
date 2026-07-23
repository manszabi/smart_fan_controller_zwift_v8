# Fejlesztői útmutató – Smart Fan Controller

Ez a dokumentum a kódbázison dolgozó fejlesztőknek szól: hogyan állítsd fel
a környezetet, hogyan futtasd a teszteket, milyen szabályokat követ a kód,
és hogyan valósíts meg tipikus bővítéseket (új adatforrás, új beállítás,
új HUD-elem, új hang).

Kapcsolódó dokumentumok:

| Dokumentum | Tartalom |
|---|---|
| `README.md` | Áttekintés, telepítés, indítás |
| `ARCHITECTURE.md` | A futásidejű architektúra (szálak, taskok, adatfolyam) |
| `CONFIGURATION.md` | A settings.json teljes, mezőnkénti referenciája |
| `CHANGELOG.md` | Verziónkénti változások |
| `docs/` | Sphinx API-referencia (docstringekből generált) |
| `mukodes.odt` / `manual.odt` | Működési leírás / felhasználói kézikönyv |

---

## 1. Fejlesztői környezet felállítása

Követelmény: **Python 3.11+** (a kód 3.11-es nyelvi elemeket használ:
`enum.StrEnum`, beépített `TimeoutError`-alias, modern asyncio).

```bash
# 1. Virtuális környezet
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. A csomag telepítése fejlesztői (editable) módban, minden extrával
pip install -e ".[all]"

# 3. Teszt-függőségek
pip install pytest
```

Minimál környezet (pl. CI, Raspberry Pi): a futtatáshoz **egyetlen külső
csomag sem kötelező** – a program a hiányzó könyvtárak (bleak, openant,
requests, PySide6, pywinauto) függvényében automatikusan kapcsolja ki az
érintett funkciókat. A tesztek is lefutnak csupasz Pythonnal (lásd lent).

Windows-kényelmi szkriptek: `setup_windows.bat` (venv + függőségek),
`run.bat` (indítás), `build_exe.bat` (PyInstaller exe a
`smart_fan_controller.spec` alapján).

---

## 2. Tesztek futtatása

```bash
pytest tests/            # teljes készlet (~3 s)
pytest tests/ -q -rs     # tömör kimenet + skip-okok
pytest tests/test_core.py -k cooldown -v   # célzott futtatás
```

A készlet felépítése:

| Fájl | Tartalom |
|---|---|
| `tests/test_core.py` | Domain-logika (zónák, cooldown, átlagolás), config-validáció, logging, BLE fan reconnect forgatókönyvek |
| `tests/test_pipeline.py` | A teljes async adatsík (minta → zóna parancs → dropout), UDP fogadó, protobuf dekóder, backoff |
| `tests/test_hud_ui.py` | HUD UI tesztek – **csak valódi PySide6-tal** futnak (offscreen Qt platformmal); nélküle 1 skip |
| `tests/conftest.py` | Stub modulok (PySide6/bleak/openant/pywinauto) a headless futtatáshoz |

Fontos a `conftest.py` működése: a stubok **csak akkor** jönnek létre, ha a
valódi csomag nincs telepítve – telepített PySide6 mellett a UI tesztek a
valódi Qt-t használják. Új külső importot bevezetve ellenőrizd, hogy a
stubok között szerepel-e a szükséges név (különben a headless CI törik).

Elvárás minden változtatásnál: **a teljes készlet zöld**, és minden
hibajavításhoz tartozik regressziós teszt.

---

## 3. Kód-konvenciók és rétegszabályok

### Rétegek és függőségi irányok

A csomag rétegei csak „lefelé” függhetnek egymástól:

```
app → controller → processors → core
                 → handlers   → core, config
                 → ui         → config, core (csak olvasás)
core   → (semmi, csak stdlib)          ← a legszigorúbb réteg!
config → (semmi, csak stdlib)
```

- **`core/` tiszta marad:** ide semmilyen Qt-, BLE-, ANT+- vagy
  hálózat-függés nem kerülhet. Csak standard library. Ettől gyors és
  környezetfüggetlen a tesztelés.
- **`config/`** szintén csak stdlib; a validáció itt történik, a felhasznált
  értékek a többi rétegben már megbízhatóak.
- A **`ui/`** réteg a vezérlőtől csak olvas (a `UISnapshot`-on és pár
  handler-attribútumon keresztül) – soha nem hívja a feldolgozó logikát.

### Konkurencia-szabályok

- Az adatfeldolgozás **egyetlen asyncio eseményhurokban** fut (külön
  daemon-szálon). Async kódban blokkoló hívás (fájl-I/O-n túl) tilos –
  blokkoló munkára `asyncio.to_thread` vagy külön szál való (lásd ANT+).
- Szálak közti adatátadás csak a bevett mintákkal:
  `loop.call_soon_threadsafe(...)` (ANT+ → queue), `threading.Lock`-kal
  védett snapshot (`UISnapshot` a HUD felé), `asyncio.Lock` a közös
  `ControllerState`-hez.
- A queue-k „legfrissebb nyer” elven működnek: teli queue-nál a régi adat
  eldobható (`put_nowait` + `QueueFull` lenyelése), a zónaparancs-queue
  mérete szándékosan 1.
- Minden hosszú életű task a `_guarded_task` burkolóban fut: váratlan
  kivétel logolódik, a kritikus taskok exponenciális backoff-fal
  újraindulnak. `asyncio.CancelledError`-t mindig tovább kell dobni.

### Logolás

Két logger van, eltérő céllal:

| Logger | Név | Célközönség |
|---|---|---|
| `user_logger` | `"user"` | A felhasználó: tiszta üzenetek a konzolra (és fájlba) |
| `logger` | `"zwift_fan_controller_new"` | Fejlesztő: részletes diagnosztika (fájlba; konzolra csak WARNING+) |

Szabályok: felhasználói üzenet magyarul, ✓/⚠ jelekkel; fejlesztői log lazy
%-formázással (`logger.debug("x=%s", x)`), forró útvonalon kerüld a felesleges
string-építést. A `zwift_api` segédprocessz saját loggert használ
(`"zwift_api_polling"`).

### Konfiguráció-kezelés

- Minden settings-mező a `config/schemas.py` egy dataclass-mezője, a
  `from_dict()`-ben a `_from_dict_int/_bool/_float/_nullable_str` helperekkel
  validálva. **Hibás érték soha nem dobhat kivételt** – figyelmeztetés +
  alapértelmezés a szabály.
- Kereszt-validáció a `__post_init__`-be kerül (minden példányosításnál fut).
- A settings runtime-ban nem változik (kivétel: a HUD a saját `hud`
  szekcióját frissítheti); a komponensek induláskor olvassák ki az értékeket.
- settings.json-ba írni **csak** az atomikus `_write_json_atomic`-on
  keresztül szabad, és csak célzott szekciófrissítéssel
  (`save_hud_settings_only`, `save_zwift_api_credentials` mintájára) – a
  felhasználó kézi szerkesztései sosem íródhatnak felül.

### Egyéb

- Típusjelölés: modern szintaxis (`int | None`, `dict[str, Any]`),
  `from __future__ import annotations` minden modulban.
- Docstring: Google-stílus (Args/Returns), angolul; a felhasználói üzenetek
  magyarul.
- Kommentet csak olyan megkötés kap, amit a kód önmagában nem tud kifejezni
  (pl. szálbiztonsági indoklás, platform-sajátosság).
- A verzió **egyetlen** forrása a `smart_fan_controller/__init__.py`
  `__version__` mezője (a pyproject és a HUD verzió-badge is innen olvas).
- Windows-kompatibilitás: batch fájlok CRLF-fel (`.gitattributes` kezeli);
  `subprocess`-hívásoknál `CREATE_NO_WINDOW` a konzol-villanás ellen.

---

## 4. Receptek – tipikus bővítések

### 4.1 Új adatforrás hozzáadása

Példa: egy új „foo” forrás, amely wattot és pulzust ad.

1. **Enum:** `config/schemas.py` → `DataSource`-ba új tag: `FOO = "foo"`.
   (A `VALID_DATA_SOURCES` automatikusan követi.)
2. **Beállítások:** `DatasourceConfig`-ba a forrás-specifikus mezők
   (pl. `foo_host`, valamint – ha eltérő adatritmusú – `FOO_buffer_seconds`,
   `FOO_minimum_samples`, `FOO_buffer_rate_hz`, `FOO_dropout_timeout`
   prefix-négyes). A `from_dict()`-be validáció, a `__post_init__`
   prefix-ciklusába a `"FOO"` prefix.
3. **Buffer-feloldás:** `config/loader.py` → `_resolve_buffer_settings()`
   prefix-elágazásába az új forrás.
4. **Handler:** új modul a `handlers/` alatt. A minta a
   `ZwiftUDPInputHandler`: kapja a `settings`-et és a queue-kat, `run()`
   korrutinja nem-blokkoló, a validált értékeket `put_nowait`-tel teszi a
   `raw_power_queue`/`raw_hr_queue`-ba, és karbantartja a
   `power_lastdata` / `hr_lastdata` monotonic időbélyegeket (ezekből él a
   HUD állapotsora). Exportáld a `handlers/__init__.py`-ból.
5. **Bekötés:** `controller.py` → `run()`-ban a többi forrás mintájára:
   ha a forrás ki van választva, hozd létre a handlert, és indítsd
   `_guarded_task`-ban (retry-paraméterekkel).
6. **HUD (opcionális):** `ui/window.py` → `_update()`-ben új státuszsor a
   meglévő `_update_sensor_row` helperrel.
7. **Tesztek:** handler-szintű teszt a `test_pipeline.py` mintájára
   (queue-ba kerülés, validálás, dropout), config-validációs teszt a
   `test_core.py`-ba.
8. **Dokumentáció:** `CONFIGURATION.md` (új mezők), `settings.default.json`
   + `settings.example.json/.jsonc` (új alapértékek – teszt őrzi, hogy a
   sablonok szinkronban legyenek!), `README.md`, `CHANGELOG.md`.

### 4.2 Új beállítási mező hozzáadása

1. Mező + alapérték a megfelelő dataclassba (`config/schemas.py`).
2. Validáció a `from_dict()`-be (helper-függvényekkel), szükség esetén
   kereszt-validáció a `__post_init__`-be.
3. Vedd fel a `settings.default.json`-ba **és** a
   `settings.example.json` / `.jsonc` sablonokba (a szinkront automata teszt
   ellenőrzi).
4. Felhasználás a megfelelő rétegben (induláskori kiolvasás).
5. Teszt: érvényes érték, érvénytelen érték (→ default + warning), hiányzó
   kulcs (→ default, csendben).
6. `CONFIGURATION.md` frissítése.

### 4.3 Új HUD-elem hozzáadása

1. `ui/window.py` `__init__`: hozd létre a widgetet a meglévő helperekkel
   (`_make_row`, `_make_status_row`, `_make_tile`), és regisztráld a
   skálázáshoz (`_register_scalable`).
2. Frissítés a `_update()`-ben – **mindig** a `_update_label` /
   `_set_tile_state` helpereken át (ezek csak tényleges változásnál nyúlnak
   a Qt-hoz, ez tartja alacsonyan a CPU-terhelést).
3. A vezérlőtől csak olvasható adatot használj (UISnapshot, handler-
   attribútumok); ha új adat kell, azt a feldolgozó oldalon tedd elérhetővé
   szálbiztosan.
4. Kézi teszt: `python hud_test/run_hud_test.py` – a vezérlőpultos
   szimulátor az éles `HUDWindow`-t hajtja fake telemetriával, minden
   HUD-módosítás azonnal látszik benne.
5. Automata teszt: `tests/test_hud_ui.py` (valódi PySide6-tal, offscreen).

### 4.4 Új hangeffekt hozzáadása

1. Tone-definíció a `tools/generate_lcars_sounds.py` `SOUND_DEFS`
   szótárába, majd `python tools/generate_lcars_sounds.py` (a meglévő
   fájlokat csak `--force`-szal írja felül).
2. A név felvétele a `ui/sound.py` `SOUND_NAMES` tuple-jébe.
3. Lejátszás: `self._sound.play("nev")` a megfelelő eseménynél
   (`ui/window.py`). Hiányzó fájl nem hiba – az effekt némán kimarad.

### 4.5 Kiadás (release) menete

1. Verzióemelés: `smart_fan_controller/__init__.py` → `__version__`.
2. `CHANGELOG.md` új szakasz.
3. Teljes tesztfuttatás: `pytest tests/`.
4. (Windows exe esetén) `build_exe.bat` és a keletkezett exe füstpróbája.
5. Commit + tag + push.

---

## 5. API-referencia generálása (Sphinx)

A `docs/` mappa Sphinx-projektet tartalmaz, amely a kód docstringjeiből
generál böngészhető HTML referenciát.

```bash
pip install sphinx
sphinx-build -b html docs docs/_build/html
# eredmény: docs/_build/html/index.html
```

A `conf.py` a nem telepített opcionális függőségeket (PySide6, bleak,
openant, pywinauto) `autodoc_mock_imports`-szal mockolja, így a generálás
csupasz Python + Sphinx környezetben is működik. A generált `docs/_build/`
nem kerül verziókövetésbe (`.gitignore`). Új modul hozzáadásakor vedd fel a
megfelelő `docs/api/*.rst` oldalra (vagy készíts újat és fűzd be az
`index.rst` toctree-jébe).

---

## 6. Hasznos belépési pontok olvasáshoz

Ha most ismerkedsz a kódbázissal, ebben a sorrendben érdemes olvasni:

1. `ARCHITECTURE.md` – a nagy kép.
2. `core/zones.py` + `core/cooldown.py` – a domain-logika magja (tiszta
   függvények, jól tesztelt).
3. `processors/processors.py` – az adat-futószalag.
4. `controller.py` `run()` – hogyan áll össze az egész.
5. `handlers/_ble.py` – a legösszetettebb I/O-réteg (fan + szenzorok).
6. `ui/window.py` `_update()` – a HUD frissítési ciklusa.
