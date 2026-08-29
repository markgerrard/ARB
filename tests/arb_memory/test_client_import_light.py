import os
import subprocess
import sys


def test_client_import_does_not_load_server_dependencies():
    env = dict(os.environ, PYTHONPATH="src")
    code = """
import importlib
import sys

importlib.import_module("arb_memory.client")
assert "psycopg" not in sys.modules
assert "mcp" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_run_import_does_not_load_server_dependencies():
    env = dict(os.environ, PYTHONPATH="src")
    code = """
import importlib
import sys

importlib.import_module("arb_memory.run")
assert "psycopg" not in sys.modules
assert "mcp" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
