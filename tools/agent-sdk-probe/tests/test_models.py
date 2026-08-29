import os
import unittest
from unittest.mock import patch

from models import ModelSpec, MODELS, load_key, MissingKeyError


class ModelsTest(unittest.TestCase):
    def test_matrix_three_no_qwen(self):
        self.assertEqual({m.name for m in MODELS}, {"minimax-m3", "kimi", "glm-5.2"})

    def test_m3_known(self):
        m3 = next(m for m in MODELS if m.name == "minimax-m3")
        self.assertEqual(
            (m3.base_url, m3.model_id, m3.key_env),
            ("https://api.minimax.io/anthropic", "MiniMax-M3", "AGENT_SDK_MINIMAX_KEY"),
        )

    def test_load_key_reads_env(self):
        with patch.dict(os.environ, {"T_KEY": "secret123"}, clear=False):
            self.assertEqual(load_key(ModelSpec("x", "u", "i", "T_KEY")), "secret123")

    def test_load_key_missing_named(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ABSENT_KEY", None)
            with self.assertRaises(MissingKeyError) as c:
                load_key(ModelSpec("x", "u", "i", "ABSENT_KEY"))
            self.assertIn("ABSENT_KEY", str(c.exception))
