import pytest

from kctl.storage import write_new


@pytest.fixture
def private_dir(tmp_path):
  path = tmp_path / "private"
  path.mkdir(mode=0o700)
  return path


@pytest.fixture
def private_file(private_dir):
  def create(name, raw=b"example"):
    path = private_dir / name
    write_new(path, raw)
    return path

  return create
