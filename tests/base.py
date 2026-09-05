import tempfile
import unittest
from pathlib import Path

from app.db import use_isolated_test_db


class GatewayTestCase(unittest.TestCase):
    """Every test gets its own throwaway SQLite file so tests never
    interfere with each other or with a real running gateway's data."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        use_isolated_test_db(db_path)

    def tearDown(self):
        self._tmpdir.cleanup()
