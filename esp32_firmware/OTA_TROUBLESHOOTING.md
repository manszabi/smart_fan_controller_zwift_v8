# OTA "Decryption error" – diagnózis és megoldás

## Tünet
```
OTA result: Update.end FAILED: Decryption error
```
A firmware 100%-ig átmegy BLE-n, majd az `Update.end()` "Decryption error"-ral elhasal.

## A VALÓDI ok (megerősítve)

A partíciós tábla **azonos** a `partitions_custom.csv`-vel → **partíció-eltérés kizárva.**

A "Decryption error" az arduino-esp32 `Update` könyvtárból jön. A könyvtár
alapból `U_AES_DECRYPT_AUTO` módban van. A `_writeBuffer()` az **első** csomagnál
így dönt:

> ha `_command == U_FLASH` ÉS `U_AES_DECRYPT_AUTO` ÉS **az első byte NEM 0xE9**
> (`ESP_IMAGE_HEADER_MAGIC`) → titkosított image-nek hiszi → megpróbálja
> visszafejteni → nincs kulcs → **`UPDATE_ERROR_DECRYPT` = "Decryption error"**

**Tehát a feltöltött bináris első byte-ja nem 0xE9.** Ez NEM titkosítási
probléma, hanem **rossz vagy sérült firmware fájl**:

| Lehetséges ok | Magyarázat |
|---|---|
| **Rossz fájl** | Nem az app `*.ino.bin`-t küldted. Pl. `*.merged.bin`, `*.bootloader.bin`, `*.partitions.bin`, vagy egy `.zip`/`.gz`. |
| **Tömörített bináris** | Ha a kliens gzip-eli, az első byte 0x1F 0x8B → nem 0xE9. |
| **Sérült átvitel** | A BLE→SPIFFS írás eleje elcsúszott/megsérült. |
| **Vezérlőbyte a fájl elején** | Egy protokoll-byte beszivárgott az `/update.bin` elejére. |

## Mit kell feltölteni?

Az Arduino IDE / arduino-cli export ezeket állítja elő:

| Fájl | Kezdő byte | OTA-ra jó? |
|---|---|---|
| `FanController_OTA_debug.ino.bin` | **0xE9** | ✅ **EZT küldd** (app image) |
| `...ino.merged.bin` | 0xE9 (bootloader) | ❌ teljes 4MB, rossz tartalom az app partíción |
| `...ino.bootloader.bin` | 0xE9 | ❌ nem app |
| `...ino.partitions.bin` | nem 0xE9 | ❌ → pont "Decryption error" |

> Arduino IDE-ben: **Sketch → Export Compiled Binary**, majd a `build/...esp32c3.../`
> mappából a sima `*.ino.bin`-t töltsd fel (NEM a merged-et).

## Ellenőrzés a feltöltés előtt (PC-n)

Az első byte-ot bármilyen hexnézővel megnézheted:
```bash
# Linux/macOS:
xxd FanController_OTA_debug.ino.bin | head -1
# Az első két karakter "e9" legyen, pl.:  00000000: e900 0210 ...

# vagy:
od -An -tx1 -N1 FanController_OTA_debug.ino.bin
# kimenet:  e9
```
Ha nem `e9`-cel kezdődik, rossz fájlt választottál.

## Firmware-oldali védelem (v7.6.4, FIX-ESP-16)

A `performUpdate()` mostantól az `Update.begin()` ELŐTT ellenőrzi az első
byte-ot (`updateSource.peek()`):

- ha **nem 0xE9** → érthető hibát küld vissza BLE-n a homályos
  "Decryption error" helyett:
  ```
  ERR: rossz firmware (magic=0x.., nem app .bin)
  ```
- a hiba a diag naplóba is bekerül:
  ```
  [ota] bad magic=0x.. size=....
  ```
  → később `diag_client.py`-vel lekérdezhető.

Így a következő próbálkozásnál azonnal látszik, hogy a fájllal van baj
(nem a titkosítással/partícióval).

## Ha az OTA szolgáltatás meg sem jelenik (v7.14.0)

A v7.14.0-tól a bootkori **CRC32 önteszt** (`crc32("123456789")==0xCBF43926`)
**release buildben is** fut. Ha **bukik** (a fordítás/optimalizálás elrontotta a
`crc32_zlib` rutint), a firmware **nem indítja el az OTA BLE-szolgáltatást** — mert a
per-part CRC-ellenőrzés megbízhatatlan lenne. Tünet: a fő `FanController` BLE-szolgáltatás
látszik és működik, de **OTA szolgáltatás nincs** (a küldő nem találja).

- A diag naplóban (`DIAG?` / `diag_client.py`):
  `[boot] CRC32 self-test FAIL -> OTA off. Just serial update!`
- Megoldás: a firmware-t **USB-soros** úton flasheld (az OTA ilyenkor szándékosan tiltott).
  Az eszköz egyébként normálisan működik (ventilátor, diag-lekérdezés).

## Lépések sorrendben

1. **Frissítsd a firmware-t v7.6.4-re** USB-n (ez tartalmazza a magic-check-et).
   - Mivel az OTA jelenleg elhasal, ezt az egyszeri lépést USB-soros feltöltéssel kell.
2. **A PC-n ellenőrizd** a feltölteni kívánt `*.ino.bin` első byte-ját (`e9`).
3. **Indítsd újra az OTA-t** a helyes `*.ino.bin`-nel.
4. Ha még mindig elhasal: kérd le a diag naplót (`python3 diag_client.py`) –
   a `[ota] bad magic=0x..` megmutatja, mi érkezett valójában a fájl elejére
   (rossz fájl vs. sérült átvitel).

## Ha a magic 0xE9, mégis hibázik

Akkor nem ide tartozó hiba. Kérd a teljes soros logot (115200 baud) OTA közben:
```
updateSize = ...
First byte (magic) = 0xE9
Update.begin OK
Update.writeStream returned: ...   <- egyezzen updateSize-zal
Update.end() returned: ...
Error code: ...
Error string: ...
```
és abból tovább diagnosztizálható (méret, MD5, flash write stb.).

> **Megjegyzés a soros kimenetről (v7.13.0-tól):** ez a `performUpdate()`-log a
> `DEBUG` csatornán megy. A `Serial` **csak akkor** indul el (`Serial.begin`), ha a
> forrás elején a `DEBUG`, `OTA_DEBUG` vagy `BOOT_DIAG` valamelyike `1` (alapból
> `DEBUG=1`). A per-csomag OTA-részletekhez (`FS write…`, `0xFC part…`) az
> `OTA_DEBUG=1` is kell. Ha mindhárom `0`, **nincs soros kimenet** — ekkor a hibát a
> BLE-válaszból (`ERR: …`) és a `diag.log`-ból (`DIAG?`) lehet kiolvasni.
