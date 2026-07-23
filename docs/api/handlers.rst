handlers – adatforrás- és kimenet-kezelők
=========================================

A ``smart_fan_controller.handlers`` csomag a külvilággal kommunikáló
rétegeket tartalmazza: BLE ventilátor-kimenet és BLE szenzorok, ANT+ vevő,
Zwift UDP fogadó.

handlers._ble – BLE ventilátor és szenzorok
-------------------------------------------

.. automodule:: smart_fan_controller.handlers._ble
   :private-members: _BLESensorInputHandler, _scan_ble_with_autodiscovery,
                     _report_gatt_characteristics, _log_ble_devices_to_file

handlers._ant – ANT+ vevő
-------------------------

.. automodule:: smart_fan_controller.handlers._ant

handlers.zwift_udp – Zwift UDP fogadó
-------------------------------------

.. automodule:: smart_fan_controller.handlers.zwift_udp
