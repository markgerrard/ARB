from __future__ import annotations


class MediationError(RuntimeError):
    pass


KNOWN_TOOLS = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "LS",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Bash",
        # Recognized unconditionally so configured ceilings parse cleanly; inert until
        # ARB_MEMORY_LOCAL_MCP injects the local server and augments the runtime ceiling.
        "mcp__arb-memory-local__memory_search",
        "mcp__arb-memory-local__memory_get",
        "mcp__arb-memory-local__memory_recent",
    }
)
MUTATING = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"})


def parse_ceiling(csv: str | None) -> frozenset[str]:
    tools = frozenset(tool.strip() for tool in (csv or "").split(",") if tool.strip())
    if not tools:
        raise MediationError("agent-sdk tool ceiling is empty; set BRIDGE_AGENT_SDK_TOOLS")
    unknown = sorted(tools - KNOWN_TOOLS)
    if unknown:
        raise MediationError(f"agent-sdk tool ceiling contains unknown tool(s): {', '.join(unknown)}")
    return tools


def decide(tool_name: str, *, ceiling: frozenset[str], policy: str) -> tuple[bool, str]:
    if tool_name not in ceiling:
        return False, f"{tool_name} outside ceiling"
    if tool_name not in KNOWN_TOOLS:
        return False, f"{tool_name} unknown"
    if policy != "trusted" and tool_name in MUTATING:
        return False, f"{tool_name} denied for non-trusted policy {policy!r}"
    return True, "allowed"


def gated_option_kwargs() -> dict[str, object]:
    # NORMATIVE: can_use_tool fires only on the "ask" path; allowed_tools and
    # setting_sources must stay empty or the CLI can bypass the bridge gate.
    return {"permission_mode": "default", "allowed_tools": [], "setting_sources": []}
