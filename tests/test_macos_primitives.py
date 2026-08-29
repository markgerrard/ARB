"""macOS behavioural corpus for the primitives the Mac seat depends on.

The companion to tests/test_script_portability.py, which lints for TEXTUAL
hazards on every platform. This module catches the class no lint can reach: a
string that is textually correct and behaves differently on macOS. The proving
case is the advice the docs carried until 65418f4c — `$(command -v grep)`,
which on a shell FUNCTION returns the function name, resolving to precisely
the thing it was written to bypass.

Each test asserts the REPO's assumption, not a general platform fact, so it
fails when the repo's dependence breaks rather than when Apple changes
something unused.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from macos_primitives_covered import COVERED, NON_STOCK

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="behavioural assertions about macOS; the textual class is covered "
    "on every platform by tests/test_script_portability.py",
)


def _sh(script: str) -> subprocess.CompletedProcess:
    """Run one line under /bin/sh, capturing both streams."""
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)


def test_covered_is_not_empty():
    """Guard the guard: an empty COVERED makes the coverage check vacuous."""
    assert COVERED, (
        "COVERED is empty — the coverage guard in test_script_portability.py "
        "would pass without asserting anything"
    )


def test_command_v_on_a_shell_function_returns_a_name_not_a_path():
    """The defect behind the broken advice: this is why $(command -v grep) fails."""
    result = _sh("grep() { :; }; command -v grep")
    assert result.stdout.strip() == "grep", (
        "expected the function NAME; a path here would mean the docs' old "
        f"$(command -v grep) form was safe after all. got {result.stdout.strip()!r}"
    )


@pytest.mark.primitive("grep")
def test_command_grep_bypasses_the_function_and_reaches_a_binary():
    result = _sh("grep() { echo SHADOWED; }; command grep --version")
    assert "SHADOWED" not in result.stdout, "command did not suppress function lookup"
    assert result.returncode == 0, result.stderr


@pytest.mark.primitive("grep")
def test_the_resolved_grep_supports_line_buffered():
    """Every Monitor invocation depends on this flag."""
    result = _sh("printf 'a\\nb\\n' | command grep --line-buffered -E 'b'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "b"


@pytest.mark.primitive("chmod")
def test_bsd_chmod_rejects_end_of_options(tmp_path):
    """The 2026-08-11 defect: `chmod 600 -- f` fails on BSD, succeeds on GNU."""
    target = tmp_path / "f"
    target.write_text("x")
    result = _sh(f"chmod 600 -- {target}")
    assert result.returncode != 0, "BSD chmod accepted `--`; the lint's premise is gone"
    assert "--" in result.stderr


@pytest.mark.primitive("sed")
def test_bsd_sed_in_place_requires_an_empty_suffix(tmp_path):
    target = tmp_path / "f"
    target.write_text("x\n")
    assert _sh(f"sed -i 's/x/y/' {target}").returncode != 0
    assert _sh(f"sed -i '' 's/x/y/' {target}").returncode == 0
    assert target.read_text() == "y\n"


@pytest.mark.primitive("stat")
def test_stat_uses_dash_f_not_dash_c(tmp_path):
    """scripts/claude-hooks/* already try -f first and fall back to -c."""
    target = tmp_path / "f"
    target.write_text("abc")
    assert _sh(f"stat -c '%s' {target}").returncode != 0
    result = _sh(f"stat -f%z {target}")
    assert result.returncode == 0
    assert result.stdout.strip() == "3"


@pytest.mark.primitive("date")
def test_bsd_date_rejects_the_gnu_date_flag():
    assert _sh("date -d 2026-01-01").returncode != 0


@pytest.mark.primitive("awk")
def test_bsd_awk_rejects_the_gnu_in_place_flag(tmp_path):
    """`awk -i inplace` is a gawk extension; BSD awk has no -i at all.

    Named in issue #13 as one of the forms that passes every static check —
    awk is a stock macOS binary, so the non-stock lint is satisfied, and the
    coverage guard binds the NAME `awk`, not this flag. Only a behavioural
    assertion catches it.

    The failure mode, as observed on this host rather than assumed: awk warns
    `unknown option -i ignored` and carries on, so `inplace` is taken as the
    PROGRAM TEXT and the real program becomes a FILENAME —

        awk: unknown option -i ignored
        awk: can't open file {gsub(/x/,"y")} 1

    Exit status 2, input untouched. Both halves are asserted: a non-zero exit
    alone would also be satisfied by awk failing for some unrelated reason,
    which on a default-deny-ish path is the ambient outcome rather than the one
    under test.

    stdin is closed explicitly. With the program text mistaken for a filename,
    an invocation that happened to leave no readable file argument would read
    stdin, and this test would block rather than fail.
    """
    target = tmp_path / "f"
    target.write_text("x\n")

    result = subprocess.run(
        ["/bin/sh", "-c", f"awk -i inplace '{{gsub(/x/,\"y\")}} 1' {target}"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    assert result.returncode != 0, (
        "BSD awk accepted `-i inplace`; if macOS has adopted the gawk "
        "extension then scripts may now use it, but confirm on a clean Mac "
        "before relying on it"
    )
    assert target.read_text() == "x\n", (
        "awk -i inplace modified the file on macOS — the GNU idiom now works "
        "here, which changes what the portability lint should allow"
    )


@pytest.mark.primitive("wc")
def test_bsd_wc_pads_a_bare_count_with_leading_whitespace(tmp_path):
    """scripts/agent-inbox-watcher:96, scripts/codex-inbox-once:47, scripts/arb-pi-orch:53,
    and scripts/cc-channel-probe:76/89/90 all pipe `wc -c`/`wc -l` through `tr -d
    '[:space:]'` (or `tr -d ' '`) immediately. This is why: BSD wc right-justifies
    a bare count to a fixed field width even when reading from a redirect with no
    filename argument, so `wc -c < f` prints e.g. "       6", not "6". GNU wc does
    not pad a single bare count this way — documented GNU coreutils behaviour, NOT
    run live here; only the BSD/macOS half below is enforced by this test. If this
    ever stopped being true on macOS
    the `tr -d` calls would become inert, not wrong — but a future call site that
    omits the strip would silently break on whichever platform still pads.
    """
    target = tmp_path / "f"
    target.write_text("hello\n")
    result = _sh(f"wc -c < {target}")
    assert result.returncode == 0, result.stderr
    assert result.stdout != result.stdout.strip(), (
        "expected leading whitespace padding on a bare `wc -c` count; got "
        f"{result.stdout!r} — the `tr -d` guard at each call site may now be "
        "unnecessary here, but treat that as news, not as licence to drop it "
        "without checking every call site"
    )
    assert result.stdout.strip() == "6"


@pytest.mark.primitive("sha256sum")
@pytest.mark.primitive("shasum")
def test_a_stock_equivalent_exists_for_every_non_stock_binary(tmp_path):
    """NOT `the binary is present` — that is the check this host passes falsely.

    mini-dev carries /sbin/sha256sum, which is not part of a stock macOS
    install, so presence here proves nothing about a clean Mac. Assert instead
    that the stock equivalent exists and agrees byte-for-byte.
    """
    fixture = tmp_path / "payload"
    fixture.write_bytes(b"the quick brown fox\n")

    for name, stock in NON_STOCK.items():
        stock_run = _sh(f"{stock} {fixture} | awk '{{print $1}}'")
        assert stock_run.returncode == 0, (
            f"{name} is not stock macOS and its stated equivalent {stock!r} "
            f"does not run here: {stock_run.stderr.strip()}"
        )
        digest = stock_run.stdout.strip()
        assert len(digest) == 64, f"expected a sha256 hex digest, got {digest!r}"

        present = _sh(f"command -v {name}")
        if present.returncode == 0:
            other = _sh(f"{name} {fixture} | awk '{{print $1}}'").stdout.strip()
            assert other == digest, (
                f"{name} and {stock} disagree on this platform: {other} != {digest}"
            )
