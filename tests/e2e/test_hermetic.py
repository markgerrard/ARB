import os
import socket
import subprocess

import pytest


pytestmark = pytest.mark.e2e


def test_env_redirected_to_tmp(tmp_path):
    assert os.environ["ARB_H2_SHADOW_LOG"].startswith(str(tmp_path.parent))
    assert os.environ["XDG_STATE_HOME"].startswith(str(tmp_path.parent))
    assert os.environ["HOME"].startswith(str(tmp_path.parent))


def test_socket_blocked():
    with pytest.raises((OSError, RuntimeError)):
        socket.socket().connect(("1.1.1.1", 80))


def test_subprocess_and_git_blocked_by_default():
    with pytest.raises(RuntimeError, match="subprocess blocked"):
        subprocess.run(["true"], check=True)
    with pytest.raises(RuntimeError, match="git blocked"):
        subprocess.Popen(["git", "status"])
