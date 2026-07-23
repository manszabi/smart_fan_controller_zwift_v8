@echo off
cd /d "%~dp0"
python ota.py "d4:f9:8d:03:6d:6a" "firmware.bin"
pause