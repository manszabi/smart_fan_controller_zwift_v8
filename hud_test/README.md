# HUD tesztkörnyezet

Önállóan indítható HUD előnézet **fake telemetriával** és egy vezérlőpanellel,
a HUD gyors vizuális teszteléséhez – Zwift, szenzorok és ventilátor nélkül.

A HUD ablak **nem másolat**: közvetlenül az éles
`smart_fan_controller.ui.hud.HUDWindow` osztály fut, egy szimulált controller
mögé kötve. A HUD bármely későbbi módosítása automatikusan itt is megjelenik,
nincs mit kézzel szinkronban tartani.

## Indítás

Windows (a projekt `setup_windows.bat`-tal telepített venv-jével):

```bat
hud_test\hud_test.bat
```

Vagy közvetlenül:

```
python hud_test/run_hud_test.py
```

Két ablak nyílik: maga a HUD, és mellette a **HUD teszt vezérlő** panel.

## Mit lehet állítani?

| Vezérlő | Hatás a HUD-on |
|---|---|
| AUTO szimuláció | Hullámzó power/pulzus, az összes zónát bejárja |
| Power / Pulzus csúszka | Kézi értékadás (AUTO kikapcsolva) |
| Power / Pulzus forrás | ANT+ / BLE / Zwift UDP – a BLE SEN., ANT+ SEN. és ZWIFT sorok és tile-ok ennek megfelelően élnek |
| Power / Pulzus jel aktív | Kikapcsolva dropout: FAIL / NO SIGNAL villogás ~5–10 s után |
| HIGHER WINS | Zóna mód váltás (be: higher_wins, ki: power_only) – HI WINS tile |
| ZPO IMM / ZHR IMM | zero_power_immediate / zero_hr_immediate tile-ok |
| BLE fan engedélyezve | Kikapcsolva: DISABLED sor |
| BLE fan kapcsolódva | Kikapcsolva: OFFLINE (villogó) |
| PIN hiba | PIN FAIL állapot |
| Cooldown indítása | 120 s visszaszámláló a COOLDOWN sorban + COOL tile |

Zónaváltáskor a szimuláció „parancsot küld" a fake ventilátornak, így a
LAST TX sor és a fan-TX hangeffekt is tesztelhető.

## Megjegyzések

- A teszt nem ír fájlt: a `save_hud_settings` ki van kapcsolva, és a
  ZwiftApp.exe-figyelés is tiltott (a HUD nem záródik be magától).
- A HUD jobb alsó sarkánál fogva átméretezhető, jobb klikkel elérhető a
  kontextus menü (opacity, hangok) – pont mint élesben.
