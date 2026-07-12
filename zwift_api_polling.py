"""zwift_api_polling.py – thin entry for the Zwift API polling helper process.

The actual implementation moved into the ``smart_fan_controller.zwift_api``
package (decoder / api / runtime / logsetup / __main__). This file
preserves direct-run and PyInstaller entry-point compatibility.

Configuration: the ``zwift_api`` section of settings.json (shared with the
main app). The main app (FanController) launches it with
``--settings <path>``.

Standalone run:
    python zwift_api_polling.py --settings settings.json
"""
from __future__ import annotations

import sys

from smart_fan_controller.zwift_api.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
