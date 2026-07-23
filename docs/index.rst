Smart Fan Controller – API-referencia
=====================================

Zwift-integrált okos ventilátor-vezérlő: ANT+, BLE és Zwift API
szenzoradatok alapján automatikusan szabályozza a BLE ventilátort (ESP32).
Ez a referencia a kód docstringjeiből generálódik.

Kapcsolódó dokumentumok a repó gyökerében: ``README.md`` (áttekintés),
``ARCHITECTURE.md`` (futásidejű architektúra), ``CONFIGURATION.md``
(settings.json referencia), ``DEVELOPMENT.md`` (fejlesztői útmutató),
``CHANGELOG.md`` (verziótörténet).

Generálás::

    pip install sphinx
    sphinx-build -b html docs docs/_build/html

.. toctree::
   :maxdepth: 2
   :caption: Modulok

   api/core
   api/config
   api/processors
   api/handlers
   api/controller_app
   api/ui
   api/zwift_api

Rétegek áttekintése
-------------------

A csomag rétegei csak „lefelé" függenek egymástól::

    app → controller → processors → core
                     → handlers   → core, config
                     → ui         → config, core (csak olvasás)
    core   → (csak stdlib)
    config → (csak stdlib)

Index
-----

* :ref:`genindex`
* :ref:`modindex`
