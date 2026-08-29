import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REFERENCE = '''
def wrap(text, width):
    if width <= 0: raise ValueError("width must be positive")
    words, lines, cur = text.split(), [], ""
    for w in words:
        if not cur: cur = w
        elif len(cur)+1+len(w) <= width: cur += " "+w
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines
'''


def _load_wrap(path):
    spec = importlib.util.spec_from_file_location("ww", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.wrap


class FixtureTest(unittest.TestCase):
    def test_stub_fails_contract(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shutil.copy(HERE / "fixture" / "wordwrap.py", d / "wordwrap.py")
            shutil.copy(HERE / "fixture" / "test_contract.py", d / "test_contract.py")
            rc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(d / "test_contract.py")],
                cwd=d,
                capture_output=True,
            ).returncode
            self.assertNotEqual(0, rc)

    def test_reference_satisfies_contract_and_heldout(self):
        from held_out.cases import CASES

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "wordwrap.py").write_text(REFERENCE)
            shutil.copy(HERE / "fixture" / "test_contract.py", d / "test_contract.py")
            rc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(d / "test_contract.py")],
                cwd=d,
                capture_output=True,
            ).returncode
            self.assertEqual(0, rc)
            wrap = _load_wrap(d / "wordwrap.py")
            # held_out ships sha256(expected), not plaintext — the reference impl's
            # output must hash to the stored digest (same canon form as verifier._canon).
            for args, expected_hash in CASES:
                canon = json.dumps(wrap(*args), ensure_ascii=False, separators=(",", ":"))
                self.assertEqual(hashlib.sha256(canon.encode()).hexdigest(), expected_hash)
