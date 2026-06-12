import pytest

import amadeus.database


@pytest.fixture
def temp_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "amadeus-test.sqlite3"
    monkeypatch.setattr(amadeus.database, "DB_PATH", db_path)
    return db_path
