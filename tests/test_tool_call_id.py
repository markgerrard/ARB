from agent_redis_bridge.tool_call_id import canonical_tool_call_id


def test_provider_id_wins_over_presentation_id():
    # tool_call_id (provider) beats tool_use_id and item_id
    assert canonical_tool_call_id(
        {"tool_call_id": "call_prov", "tool_use_id": "toolu_pres", "item_id": "item_x"}
    ) == "call_prov"


def test_tool_use_id_second_precedence():
    assert canonical_tool_call_id({"tool_use_id": "toolu_1", "item_id": "item_x"}) == "toolu_1"


def test_item_id_last_resort():
    assert canonical_tool_call_id({"item_id": "item_x"}) == "item_x"


def test_empty_and_missing_yield_empty_string():
    assert canonical_tool_call_id({}) == ""
    assert canonical_tool_call_id({"tool_call_id": "", "tool_use_id": None}) == ""


def test_non_string_values_are_skipped():
    # a non-string id is not a usable correlation key; fall through
    assert canonical_tool_call_id({"tool_call_id": 123, "tool_use_id": "toolu_2"}) == "toolu_2"
