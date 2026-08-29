"""Tests for buzz relay operational checks.

The `__path__` splice mirrors tests/arb_registration/__init__.py: this directory
shadows the src package of the same name, so without it `import buzz_ops.nip_oa`
resolves here and the import-provenance guard correctly refuses the run.
"""

from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[2] / "src" / "buzz_ops"))
