import unittest

from agent_redis_bridge.engines.agent_sdk_mediation import (
    MediationError,
    decide,
    gated_option_kwargs,
    parse_ceiling,
)


class MediationTest(unittest.TestCase):
    def test_ceiling_empty_refuses(self):
        with self.assertRaises(MediationError):
            parse_ceiling("")
        with self.assertRaises(MediationError):
            parse_ceiling(" , ")

    def test_ceiling_rejects_unknown_tools(self):
        with self.assertRaisesRegex(MediationError, "Frobnicate"):
            parse_ceiling("Read,Frobnicate,Write")

    def test_trusted_allows_in_ceiling_denies_outside(self):
        ceiling = parse_ceiling("Read,Write,Bash")
        self.assertTrue(decide("Write", ceiling=ceiling, policy="trusted")[0])
        self.assertFalse(decide("WebFetch", ceiling=ceiling, policy="trusted")[0])

    def test_nontrusted_denies_mutating(self):
        ceiling = parse_ceiling("Read,Write,Bash")
        self.assertFalse(decide("Write", ceiling=ceiling, policy="human")[0])
        self.assertTrue(decide("Read", ceiling=ceiling, policy="human")[0])

    def test_unknown_denies(self):
        self.assertFalse(decide("Frobnicate", ceiling=parse_ceiling("Read"), policy="trusted")[0])

    def test_normative_option_kwargs(self):
        kwargs = gated_option_kwargs()
        self.assertEqual(kwargs["permission_mode"], "default")
        self.assertEqual(kwargs["allowed_tools"], [])
        self.assertEqual(kwargs["setting_sources"], [])
