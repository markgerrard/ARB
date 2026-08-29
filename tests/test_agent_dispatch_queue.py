import re
import unittest
from pathlib import Path

DISPATCH = Path(__file__).parents[1] / "scripts" / "agent-dispatch"
SRC = DISPATCH.read_text()


def _live_bash(src: str) -> str:
    """Strip full-line bash comments so token presence binds to live code.

    A line that is only whitespace + ``# …`` is removed. Inline trailing
    comments on code lines are left alone (the code token still runs).
    """
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )


LIVE = _live_bash(SRC)


class AgentDispatchQueueTest(unittest.TestCase):
    def test_default_timeout_raised_to_3600(self):
        self.assertRegex(SRC, r"(?m)^TIMEOUT=3600\b")

    def test_writes_queued_status_BEFORE_request_enqueue(self):
        """Lifecycle branch: HSET queued before RPUSH (unchanged)."""
        self.assertIn("task:${ID}:status", SRC)
        self.assertIn("state queued", SRC)
        enqueue_idx = SRC.index('RPUSH "${PREFIX}agent:${TO}:inbox"')
        status_idx = SRC.index("task:${ID}:status")
        self.assertLess(
            status_idx,
            enqueue_idx,
            "queued status must be HSET before the request enqueue to avoid clobbering 'running'",
        )

    def test_ordinary_path_status_before_authority_and_refusal_cleanup(self):
        """Ordinary path (rem1 R4): write queued BEFORE dispatch_cli; failed on refuse."""
        # Pre-minted request-id is what makes reorder safe
        self.assertIn('--request-id "$ID"', SRC)
        # Live ordinary path comment marks the status-before-authority reorder
        marker = SRC.index("write queued status BEFORE authority enqueue")
        rest = SRC[marker:]
        rest_live = _live_bash(rest)
        # Bind to the LIVE redis-cli HSET command block (comment-blind: token in
        # a # line must not satisfy — E13 proven defect class).
        queued_hset = re.search(
            r'(?m)^[^\n#]*redis-cli\s+"\$\{REDIS_FLAGS\[@\]\}"\s+HSET\s+'
            r'"\$\{PREFIX\}task:\$\{ID\}:status"[\s\\n]*'
            r'task_id\s+"\$ID"\s+state\s+queued\b',
            rest_live,
        )
        self.assertIsNotNone(
            queued_hset,
            "ordinary path must HSET state=queued via redis-cli (not a comment token)",
        )
        # Re-locate in full SRC for order vs AUTH_OUT (marker-relative).
        queued_hset_full = re.search(
            r'(?m)^[^\n#]*redis-cli\s+"\$\{REDIS_FLAGS\[@\]\}"\s+HSET\s+'
            r'"\$\{PREFIX\}task:\$\{ID\}:status"[\s\\n]*'
            r'task_id\s+"\$ID"\s+state\s+queued\b',
            rest,
        )
        self.assertIsNotNone(queued_hset_full)
        status_idx = marker + queued_hset_full.start()
        auth_out_idx = SRC.index('AUTH_OUT=$(env -u ARB_MEMORY_REDIS_URL', marker)
        self.assertLess(
            status_idx,
            auth_out_idx,
            "ordinary path must HSET state=queued before invoking dispatch_cli",
        )
        # Refusal branch after the live AUTH_OUT: assert redis-cli HSET state=failed
        # on a live redis-cli line inside the AUTH_RC != 0 arm — not anywhere-in-source.
        auth_rest = SRC[auth_out_idx:]
        refusal = re.search(
            r'if\s+\[\s*"\$AUTH_RC"\s+-ne\s+0\s*\];\s*then([\s\S]*?)\n\s*fi\b',
            auth_rest,
        )
        self.assertIsNotNone(refusal, "ordinary path must have AUTH_RC refusal branch")
        refusal_body = refusal.group(1)
        refusal_live = _live_bash(refusal_body)
        failed_hset = re.search(
            r'(?m)^[^\n#]*redis-cli\s+"\$\{REDIS_FLAGS\[@\]\}"\s+HSET\s+'
            r'"\$\{PREFIX\}task:\$\{ID\}:status"[\s\\n]*'
            r'task_id\s+"\$ID"\s+state\s+failed\b',
            refusal_live,
        )
        self.assertIsNotNone(
            failed_hset,
            "refusal branch must redis-cli HSET state=failed (not a free-floating token)",
        )
        self.assertIn("dispatch_authority_refused", refusal_body)
        # Dead AUTH_ENV array must be gone (strip is env -u only)
        self.assertNotIn("AUTH_ENV=()", SRC)

    def test_ordinary_path_routes_worktree_via_typed_channel(self):
        """Ordinary path (rem3 W2d): worktree via --worktree-json, not EXTRA.

        Anchored to live (non-comment) command tokens so a comment cannot
        satisfy the claim (E13: prefixing AUTH_ARGS+= with # stayed green).
        """
        # Must NOT pack worktree into EXTRA (pre-fix transport).
        # worktree_arm still builds payload.worktree directly — that path is
        # '{operation:$operation,worktree:$wt}', not '$e + {worktree:$wt}'.
        self.assertNotIn(
            "$e + {worktree:$wt}",
            SRC,
            "ordinary path must not merge worktree into EXTRA (typed-key guard drops it)",
        )
        extra_worktree_merge = re.search(
            r'EXTRA=\$\(\s*jq\b[\s\S]*?\$e\s*\+\s*\{worktree:\$wt\}',
            LIVE,
        )
        self.assertIsNone(
            extra_worktree_merge,
            "no EXTRA=$(jq … worktree) merge may remain on the ordinary path",
        )

        # Live AUTH_ARGS append of --worktree-json (not a free-floating comment).
        # (?m)^[^\n#]* anchors to a non-comment line start (lead's E13 probe).
        typed_append = re.search(
            r'(?m)^[^\n#]*AUTH_ARGS\+=\(\s*--worktree-json\s+"\$WT_JSON"\s*\)',
            SRC,
        )
        self.assertIsNotNone(
            typed_append,
            "ordinary path must AUTH_ARGS+=(--worktree-json \"$WT_JSON\") on a live line",
        )
        # WT_JSON build for ordinary path is gated on empty OPERATION + set WORKTREE.
        # Gap must not cross the block's closing fi (non-greedy within then…fi).
        wt_gate = re.search(
            r'^[^\n#]*if\s+\[\s*-z\s+"\$OPERATION"\s*\]\s*&&\s*\[\s*-n\s+"\$WORKTREE"\s*\]\s*;\s*then\b'
            r'(?:(?!\n\s*fi\b)[\s\S])*?'
            r'^[^\n#]*AUTH_ARGS\+=\(\s*--worktree-json\s+"\$WT_JSON"\s*\)',
            SRC,
            flags=re.M,
        )
        self.assertIsNotNone(
            wt_gate,
            "ordinary --worktree-json append must be gated on empty OPERATION + set WORKTREE",
        )
        # Pin jq key names emitted into WT_JSON on the ORDINARY path only
        # (rem5 F3(b) / sol jq rename): the lifecycle worktree_arm block carries
        # the same expressions, so a whole-SRC search stayed green when the
        # ordinary-path key at agent-dispatch:601 was renamed. Scope to the
        # ordinary-branch block (empty OPERATION + set WORKTREE).
        ordinary_block = re.search(
            r'^[^\n#]*if\s+\[\s*-z\s+"\$OPERATION"\s*\]\s*&&\s*\[\s*-n\s+"\$WORKTREE"\s*\]\s*;\s*then\b'
            r'(?:(?!\n\s*fi\b)[\s\S])*?'
            r'\n\s*fi\b',
            SRC,
            flags=re.M,
        )
        self.assertIsNotNone(
            ordinary_block,
            "ordinary-path worktree block (empty OPERATION + set WORKTREE) must exist",
        )
        ordinary_src = ordinary_block.group(0)
        self.assertIsNotNone(
            re.search(r'(?m)^[^\n#]*\{name:\$name,cleanup:\$cleanup\}', ordinary_src),
            "ordinary-path jq must emit {name:$name,cleanup:$cleanup} on a live line",
        )
        self.assertIsNotNone(
            re.search(r'(?m)^[^\n#]*\{base_ref:\$base\}', ordinary_src),
            "ordinary-path jq must emit {base_ref:$base} on a live line",
        )
