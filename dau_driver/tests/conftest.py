from __future__ import annotations

import sys
from pathlib import Path

SLOP_ROOT = Path(__file__).resolve().parents[3]
DAU_CORE_SOURCE = SLOP_ROOT / "dau-core"

if DAU_CORE_SOURCE.exists():
    sys.path.insert(0, str(DAU_CORE_SOURCE))
