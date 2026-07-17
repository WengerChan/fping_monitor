"""Shared pytest fixtures."""
import os
import sys
import tempfile
from pathlib import Path

# Make project root importable when running ``pytest`` from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from database import Database


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "state.db")


@pytest.fixture
def db(tmp_db_path):
    return Database(tmp_db_path)
