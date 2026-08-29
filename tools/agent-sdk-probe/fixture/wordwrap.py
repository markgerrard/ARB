def wrap(text, width):
    """Greedy word-wrap `text` into lines of at most `width` chars.

    - Never split a word (a word longer than width gets its own overflowing line).
    - Collapse whitespace runs to single spaces; ignore leading/trailing.
    - width <= 0 raises ValueError. Returns list[str], no empty/trailing-space lines.
    """
    raise NotImplementedError
