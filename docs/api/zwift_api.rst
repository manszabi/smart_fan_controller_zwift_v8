zwift_api – Zwift-lekérdező segédprocessz
=========================================

A ``smart_fan_controller.zwift_api`` csomag külön folyamatként fut: a
Zwift HTTPS API-t lekérdezve UDP-n továbbítja az adatokat a fő programnak.

zwift_api.api – OAuth2 és REST kliens
-------------------------------------

.. automodule:: smart_fan_controller.zwift_api.api

zwift_api.decoder – minimál protobuf dekóder
--------------------------------------------

.. automodule:: smart_fan_controller.zwift_api.decoder
   :private-members: _parse_protobuf_player_state, _proto_to_int

zwift_api.runtime – adattár, UDP-küldő, polling ciklus
------------------------------------------------------

.. automodule:: smart_fan_controller.zwift_api.runtime
   :private-members: _backoff_seconds, _sleep_remainder

zwift_api.logsetup – a segédprocessz naplózása
----------------------------------------------

.. automodule:: smart_fan_controller.zwift_api.logsetup
