def test_arb_messages_keys_seal_is_monkeypatchable_through_module():
    from arb_messages import keys

    sentinel = b"patched-seal"
    orig = keys.seal
    keys.seal = lambda pt, pub: sentinel
    try:
        assert keys.seal(b"x", b"y") == sentinel
    finally:
        keys.seal = orig
