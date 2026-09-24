import re
import shutil
import subprocess
import unittest
from pathlib import Path


def nvim_supported():
    """contrib/nvim needs Neovim 0.10 (vim.system, inline virtual text)."""
    if not shutil.which('nvim'):
        return False
    version = subprocess.run(['nvim', '--version'], capture_output=True, text=True).stdout
    match = re.match(r'NVIM v(\d+)\.(\d+)', version)
    return bool(match) and (int(match[1]), int(match[2])) >= (0, 10)


@unittest.skipUnless(nvim_supported(), 'Neovim 0.10 or newer unavailable')
class NeovimTests(unittest.TestCase):
    def test_buffer_insertion(self):
        root = Path(__file__).resolve().parents[1]
        command = ['nvim', '--headless', '-u', 'NONE', '-i', 'NONE', '-l', str(root / 'tests/nvim-tests.lua')]
        run = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
