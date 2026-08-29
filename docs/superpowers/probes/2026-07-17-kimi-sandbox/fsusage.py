"""fs_usage parsing + pid-tree attribution + fail-closed contamination gate — v4 §4.

fs_usage has no PPID column and no ancestry (r2 sol P1-4). So attribution is:
reconstruct the pid tree from a `ps -axo pid,ppid,comm` snapshot, keep only rows whose
pid is in kimi's subtree, and gate the rest:
  - a row whose proc NAME is foreign (not in the discovered binary set) => real
    contamination; FAIL CLOSED when it exceeds contam_max (default 0 under quiesce).
  - a row whose name is KNOWN but whose pid isn't in the ps snapshot (short-lived,
    raced the sampler) => attribution lag; recorded, not fatal.

The proc column is `name.pid`; the name may be blank when the process clobbered its
argv (`.6890269`). The exec path is the first `/`-rooted token after `execve`.
"""
import re
from dataclasses import dataclass


class ContaminationError(RuntimeError):
    """Unattributed foreign-named rows exceeded the fail-closed threshold."""


@dataclass
class Event:
    pid: int
    proc_name: str
    binary: str | None = None   # exec rows
    path: str | None = None     # filesys rows


_PROC_COL = re.compile(r"([A-Za-z0-9_.\-]*)\.(\d+)$")


def _split_proc(token: str):
    """`Python.8351744` -> ('Python', 8351744); `.6890269` -> ('', 6890269)."""
    m = _PROC_COL.match(token)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def parse_exec_line(line: str) -> Event | None:
    parts = line.split()
    if len(parts) < 3 or "execve" not in parts[:3]:
        return None
    proc = _split_proc(parts[-1])
    if proc is None:
        return None
    name, pid = proc
    binary = next((p for p in parts if p.startswith("/")), None)
    return Event(pid=pid, proc_name=name, binary=binary)


def parse_filesys_line(line: str) -> Event | None:
    parts = line.split()
    if len(parts) < 3:
        return None
    proc = _split_proc(parts[-1])
    if proc is None:
        return None
    name, pid = proc
    path = next((p for p in parts if p.startswith("/")), None)
    if path is None:
        return None
    return Event(pid=pid, proc_name=name, path=path)


def _in_subtree(pid: int, root: int, ps_tree: dict[int, int]) -> bool:
    seen = set()
    cur = pid
    while cur and cur not in seen:
        if cur == root:
            return True
        seen.add(cur)
        cur = ps_tree.get(cur, 0)
    return False


def attribute(events, *, ps_tree, kimi_root, discovered_names,
              contam_max: int = 0, return_lag: bool = False):
    """Keep kimi-tree rows; gate the rest. Returns (kept, contamination[, lag])."""
    events = [e for e in events if e is not None]
    kept, contamination, lag = [], 0, 0
    for e in events:
        if _in_subtree(e.pid, kimi_root, ps_tree):
            kept.append(e)
        elif e.proc_name in discovered_names:
            lag += 1                     # known name, lost lineage — attribution lag
        else:
            contamination += 1           # foreign name — real contamination
    if contamination > contam_max:
        raise ContaminationError(
            f"{contamination} foreign-named rows exceed contam_max={contam_max}")
    if return_lag:
        return kept, lag
    return kept, contamination
