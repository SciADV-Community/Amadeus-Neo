import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_FILE = ROOT / "docker-compose.yml"
GHCR_COMPOSE_FILE = ROOT / "docs" / "docker-compose.ghcr.yml"
DOCKER_PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


@pytest.mark.parametrize(
    "path",
    [DOCKERFILE, COMPOSE_FILE, GHCR_COMPOSE_FILE, DOCKER_PUBLISH_WORKFLOW, TESTS_WORKFLOW],
)
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


@pytest.mark.parametrize("path", [COMPOSE_FILE, GHCR_COMPOSE_FILE])
def test_docker_compose_limits_writes_and_privileges(path):
    content = path.read_text(encoding="utf-8")

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

    assert "source: /srv/amadeus-neo/data" in content
    assert "target: /app/data" in content


@pytest.mark.parametrize("path", [COMPOSE_FILE, GHCR_COMPOSE_FILE])
def test_docker_compose_uses_latest_published_image_instead_of_local_build(path):
    content = path.read_text(encoding="utf-8")

    assert "image: ghcr.io/sciadv-community/amadeus-neo:latest" in content
    assert "build:" not in content


@pytest.mark.parametrize("path", [COMPOSE_FILE, GHCR_COMPOSE_FILE])
def test_docker_compose_uses_inline_environment_instead_of_env_file(path):
    content = path.read_text(encoding="utf-8")

    assert "env_file:" not in content
    assert "    environment:" in content
    assert '      DISCORD_TOKEN: "replace-me"' in content
    assert '      AMADEUS_DB_PATH: "/app/data/amadeus.sqlite3"' in content
    assert '      AMADEUS_PRIVACY_POLICY_URL: ""' in content
    assert '      AMADEUS_TERMS_OF_SERVICE_URL: ""' in content


def test_docker_publish_workflow_build_depends_on_unit_tests():
    content = DOCKER_PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "  unit-tests:" in content
    assert "      - name: Run tests" in content
    assert "        run: python -m pytest" in content
    assert "  build:" in content
    assert "    needs: unit-tests" in content


def test_docker_publish_workflow_ignores_docs_and_tests_only_changes():
    content = DOCKER_PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "    paths:" in content
    assert '      - "amadeus/**"' in content
    assert '      - "cogs/**"' in content
    assert '      - "main.py"' in content
    assert '      - "requirements.txt"' in content
    assert '      - "Dockerfile"' in content
    assert '      - "docs/**"' not in content
    assert '      - "tests/**"' not in content
    assert '      - "CHANGELOG.md"' not in content


def test_tests_workflow_ignores_docs_and_changelog_only_changes():
    content = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "    paths:" in content
    assert '      - "amadeus/**"' in content
    assert '      - "cogs/**"' in content
    assert '      - "main.py"' in content
    assert '      - "pytest.ini"' in content
    assert '      - "requirements.txt"' in content
    assert '      - "requirements-dev.txt"' in content
    assert '      - "tests/**"' in content
    assert '      - "docs/**"' not in content
    assert '      - "CHANGELOG.md"' not in content
