#!/bin/bash
# ---------------------------------------------------------------------------
# FanController_OTA_debug fordítása Seeed XIAO ESP32-C3 vagy ESP32-C6 boardra.
# A toolchaint a .claude/hooks/session-start.sh telepíti (Claude Code on the web).
# Helyi gépen futtatás előtt győződj meg róla, hogy az arduino-cli + esp32 core +
# OneButton + a custom partíció elérhető (lásd a hookot).
#
# Cél kiválasztása a TARGET környezeti változóval (alapértelmezett: c3):
#   TARGET=c3 ./build.sh        # XIAO ESP32-C3 (alapértelmezett)
#   TARGET=c6 ./build.sh        # XIAO ESP32-C6
#
# Használat:
#   ./build.sh                  # fordítás (c3)
#   TARGET=c6 ./build.sh        # fordítás c6-ra
#   ./build.sh --clean          # tiszta build
#   ./build.sh -v               # részletes kimenet
# A pinkiosztást a firmware a cél-chip (CONFIG_IDF_TARGET_ESP32C6) szerint
# automatikusan választja — a TARGET csak a fordítási boardot (FQBN) állítja.
# ---------------------------------------------------------------------------
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

SKETCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_VERSION="3.1.3"
# app0 partíció mérete a partitions_custom.csv-ből (0x150000) → méretkorlát-ellenőrzés
MAX_APP_SIZE="1376256"

TARGET="${TARGET:-c3}"
case "$TARGET" in
  c3) FQBN="esp32:esp32:XIAO_ESP32C3" ;;
  c6) FQBN="esp32:esp32:XIAO_ESP32C6" ;;
  *) echo "Ismeretlen TARGET='$TARGET' (használható: c3 | c6)"; exit 1 ;;
esac
echo "Build target: $TARGET ($FQBN)"

# A custom partíció elérhetővé tétele a core számára (idempotens).
PART_DIR="$HOME/.arduino15/packages/esp32/hardware/esp32/${CORE_VERSION}/tools/partitions"
if [ -d "$PART_DIR" ]; then
  cp -f "$SKETCH_DIR/partitions_custom.csv" "$PART_DIR/partitions_custom.csv"
fi

exec arduino-cli compile \
  --fqbn "$FQBN" \
  --build-property "build.partitions=partitions_custom" \
  --build-property "upload.maximum_size=${MAX_APP_SIZE}" \
  "$@" \
  "$SKETCH_DIR/FanController_OTA_debug.ino"
