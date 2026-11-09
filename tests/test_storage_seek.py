import errno
import os
import zipfile

import pytest

from kctl.core import StorageFailure
from kctl.storage import private_input, storage_io


def test_seek_before_start_remains_an_os_error(private_file):
  path = private_file("short.bin", b"short")

  with private_input(path) as stream:
    with pytest.raises(OSError) as captured:
      stream.seek(-22, os.SEEK_END)

    assert captured.value.errno == errno.EINVAL
    assert stream.seek(0) == 0
    assert stream.read() == b"short"


@pytest.mark.parametrize("raw", [b"", b"PK\x03\x04broken", b"not a ZIP"])
def test_zip_reader_handles_its_own_short_file_probe(private_file, raw):
  path = private_file("broken.zip", raw)

  with private_input(path) as stream:
    with pytest.raises(zipfile.BadZipFile):
      with zipfile.ZipFile(stream):
        pass

    stream.seek(0)
    assert stream.read() == raw


def test_invalid_descriptor_seek_fails_before_cleanup(private_file):
  path = private_file("source.bin", b"source")

  with private_input(path) as stream:
    descriptor = stream.fileno()
    saved = os.dup(descriptor)

    try:
      os.close(descriptor)

      with pytest.raises(StorageFailure, match="archive I/O failed"):
        stream.seek(0, os.SEEK_END)

    finally:
      try:
        os.dup2(saved, descriptor)
      finally:
        os.close(saved)

    assert stream.seek(0) == 0
    assert stream.read() == b"source"


def test_invalid_argument_is_not_globally_exempt_from_storage_errors():
  with pytest.raises(StorageFailure, match="archive I/O failed"):
    with storage_io():
      raise OSError(errno.EINVAL, "invalid argument")
