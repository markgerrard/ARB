from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from implbench.harness.runtime import build_production_scorer
from implbench.harness.scorer_sandbox import G4ReceiptBinding, PostImportInput


def _pid_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _scorer_binary(path: Path, *, commit_oid: str) -> None:
    bench_root = str(Path(__file__).parents[2])
    path.write_text(
        f"""#!{sys.executable}
import os
import sys
sys.path.insert(0, {bench_root!r})
from implbench.harness.scorer_sandbox import role_graph_request

if sys.argv[1:] == ["--version"]:
    print("scorer-structural-v1")
    raise SystemExit(0)

role = os.environ["IMPLBENCH_SCORER_ROLE"]
gate = sys.argv[sys.argv.index("--gate") + 1]
if role == "keyed-runner":
    fd = int(os.environ.pop("IMPLBENCH_BATTERY_KEY_FD"))
    os.read(fd, 4096) == b"structural-hidden-key" or sys.exit(41)
    role_graph_request("send", message_type="g1.request", payload={{"input": "declared"}})
    role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g1.candidate" or sys.exit(42)
    role_graph_request("send", message_type="g1.verdict", payload={{
        "g1": "PASS", "g3": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
    }})
elif role == "broker":
    role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g1.request" or sys.exit(43)
    role_graph_request("send", message_type="g1.execute", payload={{"input": "declared"}})
    role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g1.response" or sys.exit(44)
    role_graph_request("send", message_type="g1.candidate", payload={{"output": "candidate"}})
elif role == "submitted-program":
    "IMPLBENCH_BATTERY_KEY_FD" not in os.environ or sys.exit(45)
    role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g1.execute" or sys.exit(46)
    role_graph_request("send", message_type="g1.response", payload={{"output": "candidate"}})
elif role == "coordinator":
    assert "IMPLBENCH_BATTERY_KEY_FD" not in os.environ
    role_graph_request("send", message_type="g4.call", payload={{"call": "declared"}})
    assert role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g4.outcome"
    role_graph_request("send", message_type="g4.receipt", payload={{
        "commit_oid": {commit_oid!r}, "outcome_enum": "PASS",
    }})
    role_graph_request("send", message_type="g4.verdict", payload={{"g4": "PASS"}})
elif role == "suite-runner/broker":
    assert role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g4.call"
    role_graph_request("send", message_type="g4.execute", payload={{"call": "declared"}})
    assert role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g4.response"
    role_graph_request("send", message_type="g4.outcome", payload={{"outcome": "PASS"}})
elif role == "submitted-code":
    assert "IMPLBENCH_BATTERY_KEY_FD" not in os.environ
    assert role_graph_request("receive", timeout_ms=5000)["message"]["type"] == "g4.execute"
    role_graph_request("send", message_type="g4.response", payload={{"result": "ok"}})
else:
    raise SystemExit(125)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_production_scorer_executes_real_six_role_graph_structurally(
    tmp_path: Path, monkeypatch,
) -> None:
    commit_oid = "f" * 40
    public_oid = "c" * 40
    public_digest = "b" * 64
    cell_id = "cell-" + "a" * 64
    attempt_id = "attempt-" + "d" * 32
    binary = tmp_path / "scorer"
    _scorer_binary(binary, commit_oid=commit_oid)
    manifest = {
        "pins": {
            "scorer": {
                "version": "scorer-structural-v1",
                "digest": "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest(),
            },
            "public_suite": {"digest": "sha256:" + public_digest, "digest_version": "public-v1"},
        },
        "budgets": {"scorer_max_output_bytes": 4096},
    }
    monkeypatch.setenv("IMPLBENCH_SCORER_BIN", str(binary))
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", public_oid)
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "structural-hidden-key")
    uid_names = (
        "IMPLBENCH_SCORER_KEYED_RUNNER_UID", "IMPLBENCH_SCORER_BROKER_UID",
        "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID", "IMPLBENCH_SCORER_COORDINATOR_UID",
        "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID", "IMPLBENCH_SCORER_SUBMITTED_CODE_UID",
    )
    for offset, name in enumerate(uid_names, start=41001):
        monkeypatch.setenv(name, str(offset))
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    binding = G4ReceiptBinding(
        cell_id, attempt_id, commit_oid, public_oid, public_digest, "public-v1", 1, "e" * 64,
    )
    attestation = {
        "completion": {
            "cell_id": cell_id,
            "attempt_id": attempt_id,
            "receipts": [{"commit_oid": commit_oid, "controller_sequence": 1}],
        },
        "g4_receipt_bindings": (binding,),
    }

    scorer = build_production_scorer(manifest, structural_identity=True)
    result = scorer(PostImportInput(materialization, "9" * 64), attestation)

    assert result == {
        "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
        "g4_receipts": ({
            "cell_id": cell_id, "attempt_id": attempt_id, "commit_oid": commit_oid,
            "public_suite_oid": public_oid, "public_suite_digest": public_digest,
            "public_suite_digest_version": "public-v1", "outcome_enum": "PASS",
            "controller_sequence": 1, "nonce": "e" * 64,
        },),
    }
    command_rows = [row for row in scorer.last_launch_evidence if "argv" in row]
    assert len(command_rows) == 4
    assert all("implbench.harness.scorer_profile_helper" in row["argv"] for row in command_rows)
    assert all(row["profile_digest"] for row in command_rows)
    assert {uid for row in command_rows for uid in row["requested_uids"]} == set(range(41001, 41007))
    pid_rows = [row for row in scorer.last_launch_evidence if "role_pids" in row]
    pids = [pid for row in pid_rows for pid in row["role_pids"].values()]
    assert len(pids) == 6 and len(set(pids)) == 6
    assert all(_pid_absent(pid) for pid in pids)
    assert not list(tmp_path.glob(".implbench-scorer-*"))
