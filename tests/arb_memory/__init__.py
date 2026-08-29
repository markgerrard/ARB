from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

_src_pkg = Path(__file__).parents[2] / "src" / "arb_memory"
if _src_pkg.exists():
    __path__.append(str(_src_pkg))
