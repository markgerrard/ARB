"""Scripts in scripts/ run on Linux hosts AND on the Mac seat.

Third instance of a Linux-ism breaking the Mac seat: `/bin/grep` is Linux-only
and killed the Mac watcher on 2026-08-09 *after* its BLPOP, consuming an
envelope and losing it to a broken pipe; then on 2026-08-11 `chmod ... --`
shipped in both halves of the transport fix. Linux never sees either, because
GNU coreutils accepts what BSD does not — so these land green on the host that
authored them and red on the host that runs them.

These are lints, not behaviour tests: they run on Linux and catch the class
before it reaches a Mac.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from macos_primitives_covered import COVERED, NON_STOCK

SCRIPTS = Path(__file__).parents[1] / "scripts"

# BSD chmod does not accept `--` as end-of-options: `chmod 600 -- "$f"` fails
# with "chmod: --: No such file or directory". mkdir/cat/ln/rm/cp parse options
# with getopt(3) and do accept it, so this is chmod-specific.
CHMOD_END_OF_OPTIONS = re.compile(r"\bchmod\s+[0-7]{3,4}\s+--\s")

# GNU-only absolute paths. A Mac has /usr/bin/grep, not /bin/grep.
LINUX_ONLY_ABSOLUTE_BINARIES = re.compile(r"(?<![\w/])/bin/(grep|sed|awk)\b")


def _code_lines(script: Path):
    """Yield (number, line) for code only.

    Comment lines are excluded deliberately: the scripts document the very
    anti-patterns these lints forbid, and a lint that flags its own
    explanation is a false positive that trains people to ignore it.
    """
    for number, line in enumerate(script.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield number, line


def _shell_scripts() -> list[Path]:
    found = []
    for path in sorted(SCRIPTS.rglob("*")):
        if not path.is_file() or path.suffix in {".py", ".json", ".md"}:
            continue
        try:
            head = path.open("rb").readline()
        except OSError:
            continue
        if head.startswith(b"#!") and (b"sh" in head or b"bash" in head):
            found.append(path)
    return found


def test_there_are_shell_scripts_to_lint():
    """Guard the guard: a glob that silently matches nothing always passes."""
    assert _shell_scripts(), "found no shell scripts — the lint below would be vacuous"


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_chmod_does_not_use_end_of_options(script):
    offenders = [
        f"{script.name}:{number}: {line.strip()}"
        for number, line in _code_lines(script)
        if CHMOD_END_OF_OPTIONS.search(line)
    ]
    assert not offenders, (
        "BSD chmod rejects `--`; this fails on the Mac seat while passing here. "
        "Guard the operand's shape instead (prefix a relative path with ./).\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_no_linux_only_absolute_binary_paths(script):
    offenders = [
        f"{script.name}:{number}: {line.strip()}"
        for number, line in _code_lines(script)
        if LINUX_ONLY_ABSOLUTE_BINARIES.search(line)
    ]
    assert not offenders, (
        "/bin/{grep,sed,awk} does not exist on macOS; call the bare name and let "
        "PATH resolve it.\n" + "\n".join(offenders)
    )


# --- Docs ship commands too, and a snippet is what actually gets pasted -------
#
# The docs carried a correct /bin/grep WARNING ten lines from a command block
# that still contained the literal /bin/grep. The warning is not what gets
# copied. Fixed 2026-08-11; this keeps it fixed. Only FENCED CODE is checked --
# the prose must stay free to name the anti-pattern.

DOC_ROOTS = [Path(__file__).parents[1] / "docs", Path(__file__).parents[1] / "skills"]


def _fenced_command_lines(path: Path):
    """Yield (number, line) for lines inside ``` fences only."""
    inside = False
    for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            yield number, line


def _docs() -> list[Path]:
    found = []
    for root in DOC_ROOTS:
        if root.exists():
            found.extend(sorted(root.rglob("*.md")))
    return found


def test_there_are_docs_to_lint():
    assert _docs(), "found no docs — the lint below would be vacuous"


def test_no_linux_only_binary_paths_in_doc_code_blocks():
    offenders = []
    for doc in _docs():
        for number, line in _fenced_command_lines(doc):
            if LINUX_ONLY_ABSOLUTE_BINARIES.search(line):
                rel = doc.relative_to(Path(__file__).parents[1])
                offenders.append(f"{rel}:{number}: {line.strip()[:90]}")
    assert not offenders, (
        "A doc code block hardcodes a Linux-only binary path. On macOS this exits 127 "
        "and the pipeline dies -- and for a watcher it can die AFTER the BLPOP, consuming "
        "an envelope and losing it. Use `command grep`, which suppresses shell-function "
        "lookup and still resolves through PATH.\n" + "\n".join(offenders)
    )


# One definition. The lint and its parity test both format this; a change here
# is a change both of them see.
NON_STOCK_CALL = r"(?<![\w-]){binary}\s"


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_non_stock_binaries_are_guarded_somewhere_in_their_script(script):
    """A binary absent from a stock macOS must never be called bare.

    sha256sum exists on some Macs (mini-dev has /sbin/sha256sum) and on every
    Linux box, so an unguarded call is green everywhere it is tested and red
    on a clean Mac. scripts/arb-pi-orch:48 shows the guarded form.

    Honest limit, and the reason for the name: `guarded` below is computed over
    the WHOLE file, so one `command -v X` anywhere satisfies every call of X in
    that script. A SECOND, unguarded call site in an already-guarded file is
    invisible to this check. The lint it replaced had the same limit and said so;
    an earlier name here claimed call-site scoping the code does not implement.
    Narrowing to a window was considered and rejected — the three-way guarded
    form legitimately spans four lines, so a window either splits it or is wide
    enough to re-admit the same gap.
    """
    text = script.read_text()
    offenders = []
    for name in NON_STOCK:
        guarded = f"command -v {name}" in text
        for number, line in _code_lines(script):
            if re.search(NON_STOCK_CALL.format(binary=re.escape(name)), line) and not guarded:
                offenders.append(f"{script.name}:{number}: {line.strip()}")
    assert not offenders, (
        "these call a binary that is not stock macOS without a `command -v` "
        "guard; they pass on Linux and on any Mac that happens to have it.\n"
        "Use the three-way form from scripts/arb-pi-orch:46-52 "
        "(shasum, then sha256sum, then openssl dgst).\n"
        + "\n".join(offenders)
    )


def test_an_absolute_path_call_is_still_a_call():
    """A non-stock binary reached by absolute path is exactly this lint's case.

    /opt/homebrew/bin/sha256sum does not exist on a stock Mac, so an absolute-path
    invocation fails there just as a bare one does. The narrow lint this replaced
    caught it; the general lint must not regress that.
    """
    pattern = NON_STOCK_CALL.format(binary=re.escape("sha256sum"))
    assert re.search(pattern, "/opt/homebrew/bin/sha256sum foo"), (
        "absolute-path call not detected — the lookbehind is excluding '/' again"
    )
    assert not re.search(pattern, "mysha256sum foo"), (
        "over-matched a binary whose name merely ends in sha256sum"
    )


# Words that appear in command position but are not external binaries whose
# macOS behaviour needs asserting. Each entry carries its reason; a false
# positive costs one line here, a false negative is silent.
#
# Honest limit: COMMAND_POSITION below requires trailing whitespace, so a
# command at end-of-line or inside some substitutions is NOT matched. The
# allowlist is therefore loud about NEW names it does see, but the regex
# itself is conservative and can miss forms. Step 3 of this task checks it
# against the one false-positive shape already measured; it does not prove
# the extractor is complete, and no claim is made that it is.
COMMAND_POSITION_ALLOWLIST: dict[str, str] = {
    "if": "shell keyword", "then": "shell keyword", "else": "shell keyword",
    "elif": "shell keyword", "fi": "shell keyword", "for": "shell keyword",
    "while": "shell keyword", "do": "shell keyword", "done": "shell keyword",
    "case": "shell keyword", "esac": "shell keyword", "return": "shell builtin",
    "exit": "shell builtin", "echo": "shell builtin", "printf": "shell builtin",
    "cd": "shell builtin", "set": "shell builtin", "export": "shell builtin",
    "local": "shell builtin", "read": "shell builtin", "shift": "shell builtin",
    "trap": "shell builtin", "eval": "shell builtin", "exec": "shell builtin",
    "source": "shell builtin", "command": "shell builtin", "test": "shell builtin",
    "true": "shell builtin", "false": "shell builtin", "wait": "shell builtin",
    "python3": "interpreter, not a coreutil whose flags differ",
    "redis-cli": "vendored client, same binary on both platforms",
    "git": "same flags on both platforms",
    "jq": "same flags on both platforms",
    # --- shell builtins re-triaged from the generic bucket (real, not external) ---
    "kill": "shell builtin (bash's kill, not external kill(1); scripts/pi-project-b-1-supervisor:67)",
    "pwd": "shell builtin; scripts/arb-pi-orch:76 `... && pwd`",
    "declare": "shell builtin; scripts/review-brief:39 `declare -a SPECS=() ...`",
    "unset": "shell builtin; scripts/agent-redis-bridge-systemd:115 `unset ARB_MEMORY_LOCAL_MCP`",
    "umask": "shell builtin; scripts/codex-inbox-once:7 `umask 077`",

    # --- cross-platform tools: same binary/behaviour on both platforms ---
    "claude": "real invocation, Claude Code CLI (not a coreutil); scripts/cc-channel-probe:42 `claude --version`, scripts/arb-memory-local-mcp-register:15; identical binary/behaviour on both platforms",
    "tmux": "real invocation, and NOT on a stock macOS (Homebrew only: /opt/homebrew/bin/tmux, no /usr/bin/tmux) — but scripts/pi-project-b-1-supervisor:135 `tmux new-session -d -s ...` is Linux-host-only (WORKDIR=/home/<user>/AgentRedisBridge), so it never runs on the Mac seat. It is not in NON_STOCK because NON_STOCK demands a stock equivalent and tmux has none; if a Mac-reachable script ever calls tmux, it needs a `command -v` guard, not an entry here",
    "go": "real invocation, Go toolchain (not a coreutil); scripts/dispatch-dev:110 `go build -o go-client .`; same CLI on both platforms",
    "ssh": "real invocation, OpenSSH client shipped identically on both platforms; scripts/arb-watch:38 `ssh -f -N -M -S \"$CTRL\" -L ...`, flags used (-f -N -M -S -L -O) are standard OpenSSH, identical",
    "openssl": "real invocation, only `dgst -sha256` used (scripts/agent-inbox-watcher:94); identical output contract on both platforms and the call site already normalises via `awk '{print $NF}'`",

    # --- real coreutil invocations, flags confirmed identical on BSD and GNU ---
    "cat": "real invocation, bare and with `--` (scripts/agent-inbox-watcher:100 `cat -- \"${source}\" > ...`); getopt(3)-based, accepts `--` on both platforms per the CHMOD_END_OF_OPTIONS comment above (chmod is the sole `--` exception)",
    "basename": "real invocation, bare + optional suffix / `--` (scripts/arb-pi-orch:76 `basename -- \"$PI_BIN\"`, scripts/claude-tail-ensure:34 `basename \"$existing\" .plist`); identical on BSD and GNU",
    "dirname": "real invocation, bare (scripts/agent-dispatch:76 `dirname \"${BASH_SOURCE[0]}\"`); identical on BSD and GNU",
    "cp": "real invocation, only `--` used (scripts/codex-inbox-once:51 `cp -- \"${source}\" \"${temporary}\"`); getopt(3)-based, accepts `--` on both platforms",
    "env": "real invocation, `-u NAME` used (scripts/agent-dispatch:619 `env -u ARB_MEMORY_REDIS_URL -u ARB_AUDIT_REDIS_URL ...`); `-u` is documented in both BSD env(1) and GNU env(1), identical",
    "find": "real invocation, `-name -newer -print -quit` (scripts/dispatch-dev:105 `find \"$GO_DIR\" -name '*.go' -newer \"$GO_BIN\" -print -quit`); all four primaries confirmed present in BSD find(1) (macOS), identical to GNU findutils",
    "id": "real invocation, only `-u` used (scripts/claude-tail-ensure:35 `id -u`); identical on BSD and GNU, and only reached inside the Darwin branch of claude-tail-ensure's `case \"$(uname -s)\"`",
    "ln": "real invocation, only `--` used (scripts/agent-inbox-watcher:103 `ln -- \"${temporary}\" \"${destination}\"`); getopt(3)-based, accepts `--` on both platforms",
    "ls": "real invocation, only `-t` used (scripts/claude-hooks/handoff-hint.sh:7 `ls -t \"$dir\"/*.md ... | head -1`); `-t` (sort by mtime, newest first) is identical on BSD and GNU",
    "mkdir": "real invocation, only `-p` used (scripts/claude-tail-ensure:41 `mkdir -p \"$HOME/Library/LaunchAgents\"`); identical on BSD and GNU",
    "mktemp": "real invocation, `-d`/`-u` with an explicit XXXXXX template (scripts/agent-inbox-watcher:69 `mktemp -d`, scripts/arb-watch:35 `mktemp -u \"${TMPDIR:-/tmp}/arb-watch-ctrl.XXXXXX\"`); both flags documented identically in BSD mktemp(1) and GNU mktemp(1) for these forms. BSD's bare `mktemp -d` (no template) resolves under the Darwin per-user temp dir rather than GNU's $TMPDIR/tmp.XXXXXXXXXX default (verified live on this host), but every call site here only consumes the returned path as an opaque string, so the default-location difference has no observable effect",
    "pgrep": "real invocation, only `-f` used (scripts/pi-project-b-1-supervisor:77 `pgrep -f 'agent-inbox-watcher...'`); `-f` (match full argument list) is documented identically in BSD pgrep(1) and GNU procps pgrep",
    "ps": "real invocation, only `-o ppid=` / `-o comm=` used (scripts/pi-project-b-1-supervisor:91-92 `ps -o ppid= -p \"$pid\"`, `ps -o comm= -p ...`); the `keyword=` empty-header idiom is documented identically in BSD ps(1) and GNU procps ps",
    "readlink": "real invocation, bare form, no `-f` (scripts/arb-pi-orch:17 `readlink \"$SOURCE\"`, only called inside a `[[ -L \"$SOURCE\" ]]` guard); bare readlink on a real symlink is identical on BSD and GNU",
    "rm": "real invocation, `-f`/`--` used (scripts/agent-inbox-watcher:104 `rm -f -- \"${temporary}\"`); getopt(3)-based, accepts `--` on both platforms",
    "seq": "real invocation, bare `first last` (scripts/cc-channel-probe:59 `seq 1 20`, scripts/pi-project-b-1-supervisor:138 `seq 1 60`); identical on BSD and GNU for this form",
    "sleep": "real invocation, bare integer seconds (scripts/agent-inbox-watcher:116 `sleep \"${delay}\"`, scripts/codex-inbox-once:85 `sleep 1`); identical on BSD and GNU",
    "sort": "real invocation, only `-u` used (scripts/cc-channel-probe:90 `sort -u | wc -l`); `-u` (unique) is identical on BSD and GNU",
    "tr": "real invocation, `-d`, ranges, and POSIX classes (scripts/agent-inbox-watcher:96 `tr -d '[:space:]'`, scripts/agent-dispatch:474 `tr 'A-Z' 'a-z'`); all forms used here are POSIX and identical on BSD and GNU",
    "touch": "real invocation, bare (scripts/claude-hooks/context-nudge.sh:21 `touch \"$sentinel\"`); identical on BSD and GNU",
    "uname": "real invocation, only `-s` used (scripts/claude-tail-ensure:106 `case \"$(uname -s)\" in`); `-s` is identical on BSD and GNU",
    "uuidgen": "real invocation, bare (scripts/agent-dispatch:474 `uuidgen | tr 'A-Z' 'a-z'`); identical output format on BSD and GNU/util-linux uuidgen",
    "head": "real invocation, `-1` (scripts/claude-hooks/handoff-hint.sh:7 `... | head -1`); bare-number and `-n` forms confirmed identical in BSD head(1) and GNU head",
    "tail": "real invocation, `-5`/`-n1`/`-1` (scripts/cc-channel-probe:77 `tail -5 \"$log\"`, scripts/agent-dispatch:669 `| tail -n1`); bare-number and `-n` forms confirmed identical in BSD tail(1) and GNU tail",

    # --- platform-exclusive tools: absent on the other platform, no divergence possible ---
    "launchctl": "real invocation, macOS-only launchd control tool (scripts/claude-tail-ensure:35, inside setup_macos() which only runs on Darwin); no Linux/GNU counterpart to diverge from",
    "systemctl": "real invocation, Linux-only systemd control tool (scripts/claude-tail-ensure:97, inside setup_linux(), guarded by `command -v systemctl`; scripts/verify-bridge-supervision:76, a Linux-host-only supervision check); absent on macOS entirely, so no BSD-vs-GNU flag divergence applies",
    "loginctl": "real invocation, Linux-only (scripts/claude-tail-ensure:102 `loginctl enable-linger \"$USER\"`, inside setup_linux()); absent on macOS entirely",

    # --- backslash-continuation false positives: arguments, not fresh commands ---
    "cancel": "positional argument to `python3 -m agent_redis_bridge.ctl` on a `\\`-continued line (scripts/dispatch-dev:88-90); the regex reads the continuation line as a fresh command, not a real invocation",
    "error": "redis-cli HSET field-name argument on a `\\`-continued line (scripts/agent-dispatch:646-648); not a real invocation",
    "task_id": "redis-cli HSET field-name argument on a `\\`-continued line (scripts/agent-dispatch:511-512); not a real invocation",

    "type": "argument inside a jq filter string, e.g. scripts/agent-inbox-watcher:174 `jq -e 'type == \"array\" ...'`; jq's own expression language, not shell command position",

    # --- harvested from an embedded Python heredoc; Python source, not shell ---
    "at": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "base": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "cmd": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "def": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "delay": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "except": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "fail": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "from": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "import": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "log": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "logf": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "m": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "meta": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "msg": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "notify": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "offsets": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "ok": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "p": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "pending": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "processed": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "raise": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "rows": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "run": "two non-command sites, neither an invocation: Python source inside an embedded heredoc (scripts/arb-bg-wake-selftest:34, `python3 - <<'PY' ... PY`), and prose inside a log string (scripts/claude-tail-ensure:103 `log \"... (run 'sudo loginctl enable-linger $USER' once ...)\"`, where the `(` reads as command position)",
    "run_dir": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "sched": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "summary": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "t0": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",
    "value": "harvested from an embedded Python heredoc (scripts/arb-bg-wake-selftest:70-122 `python3 - <<'PY' ... PY`, or scripts/watch-go-dispatches' equivalent block); Python source, not a shell command",

    # --- bash var inside $(( arithmetic expansion )); not command position ---
    "fail_streak": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "i": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "loops": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "now": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "ok_count": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "out_count": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "size": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "stale": "bash variable inside a `$(( ... ))` arithmetic expansion, e.g. scripts/pi-project-b-1-supervisor:184 `stale=$((stale + 1))`, scripts/claude-hooks/context-nudge.sh:17 `band=$(( (size - THRESHOLD) / BAND ))`; the leading `(` reads as command position but this is arithmetic, not a command",
    "total": "prose inside an echoed help string (scripts/agent-dispatch:106 `echo \"  --turn-timeout ... total dispatch duration may span multiple turns.\" >&2`); the preceding `;` reads as command position, but this is English, not a command",

    # --- single-letter loop/local variables ---
    "a": "single-letter variable/word in prose, not a binary",

    # --- prose inside a comment/echo/printf/log string, not a command ---
    "all": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "any": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "asserting": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:159",
    "authority": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:354",
    "auto-setup": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-tail-ensure:109",
    "background": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "below": "prose inside a comment/echo/printf/log string, not a command; scripts/cc-channel-probe:50",
    "bridge": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:444",
    "channel": "prose inside a comment/echo/printf/log string, not a command; scripts/cc-channel-probe:53",
    "clears": "prose inside a comment/echo/printf/log string, not a command; scripts/verify-bridge-supervision:176",
    "client": "prose inside a comment/echo/printf/log string, not a command; scripts/dispatch-dev:127",
    "codex": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:93",
    "confirm": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:94",
    "consumer": "prose inside a comment/echo/printf/log string, not a command; scripts/pi-project-b-1-supervisor:94",
    "container": "prose inside a comment/echo/printf/log string, not a command; scripts/arb-watch:44",
    "default": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:105",
    "every": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "evidence": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:158",
    "expected": "prose inside a comment/echo/printf/log string, not a command; scripts/arb-messages-gate:29",
    "expecting": "prose inside a comment/echo/printf/log string, not a command; scripts/verify-bridge-supervision:150",
    "explicit": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:310",
    "extension": "prose inside a comment/echo/printf/log string, not a command; scripts/pi-project-b-1-supervisor:155",
    "falling": "prose inside a comment/echo/printf/log string, not a command; scripts/dispatch-dev:110",
    "fix": "prose inside a comment/echo/printf/log string, not a command; scripts/verify-bridge-supervision:181",
    "free-form": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:321",
    "gate": "prose inside a comment/echo/printf/log string, not a command; scripts/arb-messages-gate:24",
    "got": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:560",
    "heartbeat": "prose inside a comment/echo/printf/log string, not a command; scripts/pi-project-b-1-supervisor:130",
    "manifest": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "missing": "prose inside a comment/echo/printf/log string, not a command; scripts/arb-messages-gate:29",
    "never": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "new": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:120",
    "no": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:81",
    "not": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:146",
    "of": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:150",
    "or": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:311",
    "overrides": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:90",
    "panel-audit": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:164",
    "paths": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "pid": "prose inside a comment/echo/printf/log string, not a command; scripts/pi-project-b-1-supervisor:58",
    "pin": "prose inside a comment/echo/printf/log string, not a command; scripts/cc-channel-probe:111",
    "place": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-tail-ensure:40",
    "port": "prose inside a comment/echo/printf/log string, not a command; scripts/arb-watch:39",
    "publish": "prose inside a comment/echo/printf/log string, not a command; scripts/dispatch-dev:139",
    "registered": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:450",
    "repeated": "prose inside a comment/echo/printf/log string, not a command; scripts/cc-channel-probe:106",
    "required": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:83",
    "review": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:179",
    "see": "prose inside a comment/echo/printf/log string, not a command; scripts/verify-bridge-supervision:174",
    "sequential": "prose inside a comment/echo/printf/log string, not a command; scripts/cc-channel-probe:110",
    "start": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-tail-ensure:72",
    "started": "prose inside a comment/echo/printf/log string, not a command; scripts/verify-bridge-supervision:181",
    "task": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/precompact-preserve.sh:6",
    "the": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:365",
    "this": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:466",
    "to": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:157",
    "ultra": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:560",
    "update": "prose inside a comment/echo/printf/log string, not a command; scripts/arb-messages-gate:15",
    "using": "prose inside a comment/echo/printf/log string, not a command; scripts/dispatch-dev:112",
    "worktree_release": "prose inside a comment/echo/printf/log string, not a command; scripts/agent-dispatch:95",
    "written": "prose inside a comment/echo/printf/log string, not a command; scripts/claude-hooks/handoff-hint.sh:13",
    "your": "prose inside a comment/echo/printf/log string, not a command; scripts/review-brief:152",
}

# A word in command position: line start, or after | || && ; ( or $(
COMMAND_POSITION = re.compile(r"(?:^|[|;&(]|\$\()\s*([a-z][\w.-]*)\s")


def _binaries_in_command_position(script: Path) -> set[str]:
    text = script.read_text()
    defined = set(re.findall(r"^\s*([\w.-]+)\s*\(\)\s*\{", text, re.M))
    names = set()
    for _number, line in _code_lines(script):
        for match in COMMAND_POSITION.finditer(line):
            names.add(match.group(1))
    return names - defined


def test_binaries_in_command_position_extractor_contract(tmp_path):
    """Pin the extractor's contract in isolation, on a fixture it cannot get
    wrong by accident. Previously this logic was exercised only via the
    full-corpus scan in test_every_invoked_binary_has_a_macos_behaviour_test,
    whose signal was masked once the allowlist carried a false blanket reason
    for 26 real invocations — a defect in the extractor could have hidden
    behind a defect in the data with nothing to tell them apart.
    """
    script = tmp_path / "fixture.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "# comment mentions mkdir here, must not be extracted\n"
        "grep() { :; }\n"
        "grep -q foo\n"
        "echo hello | tail\n"
        "echo \"use touch to update mtime\"\n"
        "ls -la\n"
    )
    result = _binaries_in_command_position(script)
    assert result == {"echo", "ls"}, (
        f"extractor contract drifted: got {sorted(result)}, expected exactly "
        "{'echo', 'ls'} — grep is shadowed by its own function definition "
        "(function-shadowing case), mkdir appears only in a comment "
        "(comment-exclusion case), tail sits at end-of-line with no trailing "
        "whitespace (the regex's documented honest limit), and touch is an "
        "argument value inside an echo string, never in command position "
        "(flag/argument-value case)"
    )


def test_extractor_does_not_harvest_timeout_from_flags_and_comments():
    """Regression pin for a real defect measured in this plan: an earlier
    attempt at this lint reported 31 `timeout` invocations across scripts/
    against a true count of zero bare `timeout` commands — every hit was a
    comment, a `--turn-timeout` flag, or a `TURN_TIMEOUT`/`TIMEOUT` variable.
    scripts/agent-dispatch alone carries several of those shapes (see its
    `--turn-timeout` help text and `TIMEOUT` variable), so it is the fixture:
    if COMMAND_POSITION ever regresses to a word-frequency harvest instead of
    a real command-position match, this is where it resurfaces first.
    """
    script = SCRIPTS / "agent-dispatch"
    assert script.is_file(), "fixture script missing: scripts/agent-dispatch"
    names = _binaries_in_command_position(script)
    assert "timeout" not in names, (
        "the extractor is matching `timeout` outside command position again "
        "(comments / --turn-timeout flags / TIMEOUT vars) — tighten "
        "COMMAND_POSITION before trusting the coverage guard"
    )


def test_every_invoked_binary_has_a_macos_behaviour_test():
    """Bind the corpus to the tree so a new dependency cannot land unnoticed.

    Static, and deliberately in THIS file rather than the Darwin-only corpus:
    the contributors who introduce these bugs work on Linux, and a check that
    only fires on a Mac would never reach them.
    """
    uncovered: dict[str, str] = {}
    for script in _shell_scripts():
        for name in _binaries_in_command_position(script):
            if name in COVERED or name in COMMAND_POSITION_ALLOWLIST:
                continue
            uncovered.setdefault(name, script.name)
    assert not uncovered, (
        "these binaries are invoked by shell scripts but have no macOS "
        "behaviour test in tests/test_macos_primitives.py. Add one and extend "
        "COVERED; or, if the binary is NOT on a stock macOS install and has a "
        "stock equivalent, add it to NON_STOCK instead so its call sites must "
        "be `command -v` guarded; or add an allowlist entry with a reason:\n"
        + "\n".join(f"  {n} (first seen in {s})" for n, s in sorted(uncovered.items()))
    )


def _behavioural_subjects() -> dict[str, list[str]]:
    """Binaries claimed as the SUBJECT of a macOS behavioural test.

    Read from ``@pytest.mark.primitive(<binary>)`` rather than from test names:
    a name-matching heuristic would count
    ``test_a_stock_equivalent_exists_for_every_non_stock_binary`` as covering
    nothing, and would count any test merely mentioning a binary as covering it
    — which is the exact confusion this pair of guards exists to remove.

    Imported inside the function: tests/test_macos_primitives.py is collected by
    pytest in its own right, and importing it at module scope here would tie
    this file's import to that one's.
    """
    import importlib

    module = importlib.import_module("test_macos_primitives")
    subjects: dict[str, list[str]] = {}
    for attr, obj in vars(module).items():
        if not attr.startswith("test_") or not callable(obj):
            continue
        for mark in getattr(obj, "pytestmark", []):
            if mark.name != "primitive":
                continue
            for binary in mark.args:
                subjects.setdefault(binary, []).append(attr)
    return subjects


def test_every_covered_binary_is_the_subject_of_a_behavioural_test():
    """COVERED membership must be EARNED, not merely declared.

    The complement of test_every_invoked_binary_has_a_macos_behaviour_test.
    That one stops a NEW dependency landing without a test. This one stops a
    binary sitting in COVERED with no test behind it — which is how `awk`
    entered (issue #13): it reached COVERED through a plan's Interfaces line,
    and appeared in the corpus only as a pipe helper (`| awk '{print $1}'`)
    inside a test about sha256, never as a subject.

    Without this, COVERED reads as "safe on a Mac" while meaning only "someone
    once wrote a test mentioning this". Static, and in THIS file rather than the
    Darwin-only corpus, for the same reason as its complement: the contributors
    who introduce these bugs work on Linux.

    NOTE this binds binary NAMES, not flags — a `sed -r` added tomorrow still
    passes, because `sed` has a behavioural test about a different flag. That
    remains the open half of issue #13; this guard closes the "no test at all"
    half only.
    """
    subjects = _behavioural_subjects()

    unearned = sorted(COVERED - set(subjects))
    assert not unearned, (
        "these binaries are in COVERED but are not the subject of any macOS "
        "behavioural test, so their membership asserts nothing: "
        f"{unearned}. Add a test in tests/test_macos_primitives.py marked "
        "`@pytest.mark.primitive(<binary>)`, or drop the binary from COVERED."
    )

    stray = sorted(set(subjects) - COVERED)
    assert not stray, (
        "these binaries are marked as behavioural-test subjects but are not in "
        f"COVERED: {stray}. Either the marker is a typo, or COVERED is missing "
        "an entry the corpus already tests."
    )
