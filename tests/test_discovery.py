import os
import subprocess
import sys

from pathlib import Path

import pytest


@pytest.mark.parametrize("selection", [[], ["."]])
def test_project_collection_does_not_import_archived_code(tmp_path, selection):
  project = tmp_path / "project"
  project.mkdir()
  configuration = Path(__file__).resolve().parents[1] / "pyproject.toml"
  (project / "pyproject.toml").write_bytes(configuration.read_bytes())
  (project / "tests").mkdir()
  (project / "tests/test_project.py").write_text(
    "def test_project():\n  assert True\n",
    encoding="utf-8",
  )

  archive = project / "dump"
  archive.mkdir()
  (archive / "documents").mkdir()
  imported = project / "archive-imported"
  external = (
    "from pathlib import Path\n"
    f"Path({str(imported)!r}).write_text('imported')\n"
    "raise RuntimeError('archived code must not be imported')\n"
  )
  (archive / "conftest.py").write_text(external, encoding="utf-8")
  (archive / "documents/test_downloaded.py").write_text(
    external, encoding="utf-8"
  )

  environment = {
    name: value
    for name, value in os.environ.items()
    if not name.startswith("PYTEST_")
  }
  environment.update({
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
  })
  result = subprocess.run(
    [
      sys.executable,
      "-B",
      "-m",
      "pytest",
      "--collect-only",
      "-q",
      "-p",
      "no:cacheprovider",
      *selection,
    ],
    cwd=project,
    env=environment,
    capture_output=True,
    text=True,
    timeout=30,
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert "tests/test_project.py::test_project" in result.stdout
  assert "test_downloaded" not in result.stdout
  assert "1 test collected" in result.stdout
  assert not imported.exists()
