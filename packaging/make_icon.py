"""Render the app icon to packaging/tuneblade.ico."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tuneblade.gui import make_icon  # noqa: E402

out = os.path.join(os.path.dirname(__file__), "tuneblade.ico")
make_icon(256).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
