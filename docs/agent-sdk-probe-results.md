# agent-sdk mutation probe results

| model | status | reasons |
|---|---|---|
| minimax-m3 | PASS | contract + held-out (hash) green; only wordwrap.py changed |
| kimi | PASS | contract + held-out (hash) green; only wordwrap.py changed |
| glm-5.2 | PASS | contract + held-out (hash) green; only wordwrap.py changed |

## Trace Excerpts

### minimax-m3

```
TOOL_USE Bash
TOOL_USE Read
TOOL_USE Bash
TOOL_USE Read
TOOL_USE Read
TOOL_USE Write
TOOL_USE Bash
TOOL_USE Bash
TOOLS=Bash,Read,Bash,Read,Read,Write,Bash,Bash
he clearer case: width=9, current="the", word="quick" — 3+1+5=9, which equals width, so we keep it on the same line. Using `>=` would have incorrectly broken it.
- **Seeding `current` with the first word** and iterating from `words[1:]` avoids a special "is this the first word on the line?" branch — the loop is uniform, and the trailing `lines.append(current)` handles the last line.
`─────────────────────────────────────────────────`

**Summary of the implementation:**

| Contract requirement | How it's met |
|---|---|
| Greedy, max `width` chars per line | Accumulate while `len(current) + 1 + len(word) <= width` |
| Never split a word | If adding a word would exceed width, break to a new line and put the word there alone |
| Collapse whitespace, strip ends | `text.split()` does both in one step |
| `width <= 0` → `ValueError` | Guard at the top |
| No empty lines, no trailing spaces | Only words are ever emitted, joined with single spaces |

`test_contract.py`: **3 passed in 0.00s** ✓


```

### kimi

```
TOOL_USE Skill
TOOL_USE Read
TOOL_USE Read
TOOL_USE Read
TOOL_USE Bash
TOOL_USE Bash
TOOL_USE Read
TOOL_USE Read
TOOL_USE Bash
TOOL_USE Bash
TOOL_USE Edit
TOOL_USE Bash
TOOLS=Skill,Read,Read,Read,Bash,Bash,Read,Read,Bash,Bash,Edit,Bash
, avoiding extra logic beyond the docstring’s guarantees.
`─────────────────────────────────────────────────`

All three contract tests pass. `wordwrap.py` now implements `wrap` to satisfy the required behavior.`★ Insight ─────────────────────────────────────`
- `str.split()` with no arguments is the simplest way to satisfy two contract rules at once: it collapses any run of whitespace and discards leading/trailing whitespace, returning the normalized word stream.
- The greedy algorithm only needs one piece of state (`current`). A new line starts when adding a space plus the next word would exceed `width`. The overflow rule is automatic: if `current` is empty, we place even an oversized word on its own line rather than splitting it.
- I kept the `ValueError` explicit and minimal, avoiding extra logic beyond the docstring’s guarantees.
`─────────────────────────────────────────────────`

All three contract tests pass. `wordwrap.py` now implements `wrap` to satisfy the required behavior.


```

### glm-5.2

```
TOOL_USE Read
TOOL_USE Read
TOOL_USE Bash
TOOL_USE Read
TOOL_USE Read
TOOL_USE Edit
TOOL_USE Bash
TOOL_USE Bash
TOOLS=Read,Read,Bash,Read,Read,Edit,Bash,Bash
`wrap` is implemented in `wordwrap.py` and all three contract tests pass (`test_basic`, `test_single`, `test_invalid_width`). Only `wordwrap.py` was edited.All 3 tests pass.

`★ Insight ─────────────────────────────────────`
- The implementation avoids an off-by-one trap: the fit check is `<= width` (not `< width`), so a line that is *exactly* `width` long is accepted. That's why `"the quick"` (9 chars at width 9) and `"brown fox"` (9 chars) each form a complete line rather than overflowing.
- Returning `[]` for empty input is the correct edge case: with no words, there are no lines to emit, and the contract forbids empty/trailing-space lines. The trailing `if current: lines.append(current)` ensures the last accumulated line is flushed exactly once and never duplicated.
`─────────────────────────────────────────────────`

**Done.** `wrap` is implemented in `wordwrap.py` and all three contract tests pass (`test_basic`, `test_single`, `test_invalid_width`). Only `wordwrap.py` was edited.


```

