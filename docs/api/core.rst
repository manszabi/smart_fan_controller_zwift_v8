core – tiszta domain-logika
===========================

A ``smart_fan_controller.core`` csomag a mellékhatás-mentes magot
tartalmazza: zónaszámítás, gördülő átlagolás, cooldown, megosztott állapot,
naplózás és segédfüggvények. Csak a standard librarytől függ.

core.zones – zónaszámítás és validáció
--------------------------------------

.. automodule:: smart_fan_controller.core.zones

core.averaging – gördülő átlagolás
----------------------------------

.. automodule:: smart_fan_controller.core.averaging
   :private-members: _RollingAverager

core.cooldown – cooldown-vezérlő
--------------------------------

.. automodule:: smart_fan_controller.core.cooldown

core.state – megosztott állapot
-------------------------------

.. automodule:: smart_fan_controller.core.state

core.printers – szabályozott konzol-kimenet
-------------------------------------------

.. automodule:: smart_fan_controller.core.printers

core.logging_setup – naplózási infrastruktúra
---------------------------------------------

.. automodule:: smart_fan_controller.core.logging_setup

core.helpers – segédfüggvények
------------------------------

.. automodule:: smart_fan_controller.core.helpers
