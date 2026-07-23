config – beállítás-modellek és betöltő
======================================

A ``smart_fan_controller.config`` csomag a ``settings.json`` type-safe
modelljeit (dataclassok, enumok) és a betöltő/mentő logikát tartalmazza.
Hibás érték soha nem dob kivételt: figyelmeztetés + alapértelmezés.

config.schemas – enumok és dataclass-modellek
---------------------------------------------

.. automodule:: smart_fan_controller.config.schemas

config.loader – betöltés, mentés, származtatott lekérdezések
------------------------------------------------------------

.. automodule:: smart_fan_controller.config.loader
   :private-members: _resolve_buffer_settings, _write_json_atomic,
                     _ensure_default_settings_file, _backup_incorrect_settings
