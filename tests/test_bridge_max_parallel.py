from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock


def reload_bridge_module():
    import agent_redis_bridge.bridge as bridge_module

    return importlib.reload(bridge_module)


class MaxParallelArgTest(unittest.TestCase):
    def test_max_parallel_defaults_to_one(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_MAX_PARALLEL", None)
            bridge_module = reload_bridge_module()
            parser = bridge_module.build_parser()
            args = parser.parse_args([])
        self.assertEqual(args.max_parallel, 1)

    def test_env_var_overrides_default(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_MAX_PARALLEL": "3"}):
            bridge_module = reload_bridge_module()
            parser = bridge_module.build_parser()
            args = parser.parse_args([])
        self.assertEqual(args.max_parallel, 3)

    def test_cli_flag_overrides_env(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_MAX_PARALLEL": "3"}):
            bridge_module = reload_bridge_module()
            parser = bridge_module.build_parser()
            args = parser.parse_args(["--max-parallel", "5"])
        self.assertEqual(args.max_parallel, 5)

    def test_invalid_env_value_falls_back_to_default(self) -> None:
        with mock.patch.dict(os.environ, {"BRIDGE_MAX_PARALLEL": "not-an-int"}):
            bridge_module = reload_bridge_module()
            parser = bridge_module.build_parser()
            args = parser.parse_args([])
        self.assertEqual(args.max_parallel, 1)


if __name__ == "__main__":
    unittest.main()
