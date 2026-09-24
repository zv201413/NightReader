"""Run the public, generated-PDF suite with temporary user preferences."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.environ['NO_AT_BRIDGE'] = '1'
with tempfile.TemporaryDirectory(prefix='nightreader-tests-') as tmp:
    os.environ['XDG_CONFIG_HOME'] = tmp
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print('Release tests must run under Xvfb with xdotool; skips are failures.')
    raise SystemExit(not result.wasSuccessful() or bool(result.skipped))
