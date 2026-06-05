import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_FILE = ROOT / "docker-compose.yml"


@pytest.mark.parametrize("path", [DOCKERFILE, COMPOSE_FILE])
def test_container_config_files_have_clean_line_formatting(path):
    content = path.read_text(encoding="utf-8")

    assert content.endswith("\n")
    for line in content.splitlines():
        assert "\t" not in line
        assert line == line.rstrip()


def test_docker_compose_syntax_is_valid_when_docker_is_available():
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed in this environment.")

    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_dockerfile_runs_as_unprivileged_user_with_readonly_runtime_support():
    content = DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in content
    assert "PYTHONDONTWRITEBYTECODE=1" in content
    assert "PYTHONUNBUFFERED=1" in content
    assert "groupadd --system --gid 10001 amadeus" in content
    assert "useradd --system --uid 10001 --gid amadeus" in content
    assert "mkdir -p /app/data" in content
    assert "COPY --chown=amadeus:amadeus . ." in content
    assert "USER 10001:10001" in content
    assert 'CMD ["python", "main.py"]' in content


def test_docker_compose_limits_writes_and_privileges():
    content = COMPOSE_FILE.read_text(encoding="utf-8")

    expected_lines = [
        '    user: "10001:10001"',
        "    read_only: true",
        "    cap_drop:",
        "      - ALL",
        "    security_opt:",
        "      - no-new-privileges:true",
        "    tmpfs:",
        "      - /tmp:rw,noexec,nosuid,nodev,size=64m",
        "        target: /app/data",
        "        read_only: false",
    ]

    for line in expected_lines:
        assert line in content

    assert "source: ./data" in content
    assert "target: /app/data" in content
