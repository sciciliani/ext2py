import subprocess
import sys

import ext2py
from ext2py.cli import main


def test_version_is_string():
    assert isinstance(ext2py.__version__, str)


def test_cli_help_runs():
    assert main([]) == 0


def test_module_entry_point():
    out = subprocess.run(
        [sys.executable, "-m", "ext2py", "--version"],
        capture_output=True, text=True, check=True,
    )
    assert ext2py.__version__ in out.stdout
