import pytest

from amadeus.database import _database_path_error, _ensure_database_path_ready


def test_ensure_database_path_ready_creates_writable_parent(tmp_path):
    db_path = tmp_path / "nested" / "amadeus.sqlite3"

    _ensure_database_path_ready(db_path)

    assert db_path.parent.is_dir()


def test_ensure_database_path_ready_rejects_file_as_parent(tmp_path):
    not_a_directory = tmp_path / "data"
    not_a_directory.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        _ensure_database_path_ready(not_a_directory / "amadeus.sqlite3")

    assert "/srv/amadeus-neo/data" in str(exc.value)


def test_database_path_error_mentions_docker_host_fix():
    message = _database_path_error("/app/data/amadeus.sqlite3")

    assert "sudo install -d -m 0770 -o 10001 -g 10001 /srv/amadeus-neo/data" in message
