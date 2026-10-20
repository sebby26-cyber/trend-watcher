"""Keep user-installed packs out of unittest runs, as conftest.py does for pytest.

Imported first by every test module; setting the variables at import time covers
`python -m unittest discover -s tests`, which never loads conftest.py.
"""
import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="trend-watch-test-config-")
os.environ["TREND_WATCH_PACKS_DIR"] = ""
