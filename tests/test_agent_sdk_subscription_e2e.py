import pytest


@pytest.mark.e2e
@pytest.mark.skip(reason="requires a live Claude Code subscription seat and operator-owned account state")
def test_codex_to_subscription_opus_verdict_roundtrip():
    """Live gate:

    Preconditions: Redis bus is running; an agent-sdk seat is stood up with
    `CLAUDE_CODE_OAUTH_TOKEN` in its env and launched as
    `agent-redis-bridge --engine agent-sdk --model opus-4.8 --agent-sdk-oneshot
    --agent-sdk-tools Read,Grep,Glob,LS`.

    Command shape: dispatch from a codex sender to that seat with a review task,
    e.g. `agent-dispatch --from codex-project-c-dev --to agent-sdk-project-c-dev-example
    "review this diff and return VERDICT: ..."`.

    Assertion: the reply consumed by codex is from the opus48 seat id, contains
    the verdict text, and the task event stream includes
    `agent_sdk_subscription_audit` with `bridge_opus_inside_claude_code_opus`
    false for codex.
    """
