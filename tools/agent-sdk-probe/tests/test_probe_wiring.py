import unittest
from unittest.mock import patch

import probe
from models import ModelSpec

GENUINE = '''
def wrap(text, width):
    if width <= 0:
        raise ValueError("width must be positive")
    words, lines, cur = text.split(), [], ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines
'''


class WiringTest(unittest.TestCase):
    def test_genuine_passes_and_trace_scrubbed(self):
        spec = ModelSpec("minimax-m3", "u", "i", "AGENT_SDK_MINIMAX_KEY")
        canary = "CANARY-KEY-7e1"

        def fake_sdk(tempdir, *_args, **_kwargs):
            (tempdir / "wordwrap.py").write_text(GENUINE)
            return ("ran ok " + canary, "")

        with (
            patch.object(probe, "run_spike", lambda _s: {"ok": True}),
            patch.object(probe, "_sdk_mutation", fake_sdk),
            patch.dict("os.environ", {"AGENT_SDK_MINIMAX_KEY": canary}, clear=False),
        ):
            result = probe.run_model(spec)

        self.assertEqual(result["status"], "PASS")
        self.assertNotIn(canary, result["trace_excerpt"])
