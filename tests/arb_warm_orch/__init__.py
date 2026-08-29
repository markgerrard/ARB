from pathlib import Path

_src_pkg = Path(__file__).parents[2] / "src" / "arb_warm_orch"
if _src_pkg.exists():
    __path__.append(str(_src_pkg))
