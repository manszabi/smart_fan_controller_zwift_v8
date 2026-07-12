LCARS hangeffektek (WAV)

A HUD hangjai ebből a mappából töltődnek be. Tömörítetlen PCM WAV
formátum szükséges (a Qt QSoundEffect csak ezt támogatja).

Szükséges fájlok ebben a mappában:
  zone_up.wav            zónaváltás felfelé
  zone_down.wav          zónaváltás lefelé
  zone_standby.wav       standby-ba lépés
  sensor_dropout.wav     szenzor jelvesztés (vészjelzés)
  sensor_reconnect.wav   szenzor visszacsatlakozás
  zwift_connect.wav      Zwift adat érkezik
  zwift_disconnect.wav   Zwift jelvesztés
  fan_tx.wav             ventilátor parancs elküldve
  hud_startup.wav        HUD indítás (tricorder kinyitás)
  hud_shutdown.wav       HUD bezárás (tricorder becsukás)

Ha egy fájl hiányzik, a program hang nélkül fut tovább, és a logba
figyelmeztetés kerül a hiányzó fájl pontos útvonalával.

A gyári (szintetizált LCARS) hangok újragenerálása a projekt gyökeréből:
  python tools/generate_lcars_sounds.py

Bármelyik fájl lecserélhető saját WAV-ra (pl. eredeti LCARS hangminták) –
a fájlnévnek a fenti listával kell egyeznie. A generáló szkript a meglévő
fájlokat alapból nem írja felül (csak a --force kapcsolóval).
