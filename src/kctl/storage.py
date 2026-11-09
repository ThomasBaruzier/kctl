from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import re
import shutil
import stat
import sys

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .core import (
  Cancelled,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  explanation,
  join_path,
  relative_parts,
  require,
  token,
)


DIRECTORY_FLAGS = (
  os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
READ_FLAGS = (
  os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
CREATE_FLAGS = (
  os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)


def checked_file(info: os.stat_result) -> None:
  if not (
    stat.S_ISREG(info.st_mode)
    and info.st_uid == os.getuid()
    and stat.S_IMODE(info.st_mode) == 0o600
    and info.st_nlink == 1
  ):
    raise ProtectionFailure(
      "unsafe managed file: expected owned regular file, mode 0600, one link"
    )


def checked_directory(info: os.stat_result) -> None:
  if not (
    stat.S_ISDIR(info.st_mode)
    and info.st_uid == os.getuid()
    and stat.S_IMODE(info.st_mode) == 0o700
  ):
    raise ProtectionFailure(
      "unsafe managed directory: expected owned directory, mode 0700"
    )


def checked_ancestor(info: os.stat_result, path: Path) -> None:
  android = (
    (sys.platform == "android" or hasattr(sys, "getandroidapilevel"))
    and re.fullmatch(
      r"/data(?:/data|/(?:user|user_de)(?:/\d+)?)?", str(path)
    )
    and info.st_uid in {0, 1000}
  )
  sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
  protected = (
    not info.st_mode & 0o002
    and (not info.st_mode & 0o020 or android and info.st_gid == 1000)
  )

  if not (
    stat.S_ISDIR(info.st_mode)
    and (info.st_uid in {0, os.getuid()} or android)
    and (sticky or protected)
  ):
    raise ProtectionFailure("unsafe managed ancestor")


@contextmanager
def storage_io() -> Iterator[None]:
  try:
    yield
  except OSError as error:
    raise StorageFailure(
      "archive I/O failed: " + explanation(error)
    ) from None


class _LocalFile(io.FileIO):
  def __init__(self, descriptor: int, mode: str):
    super().__init__(descriptor, mode)
    self.failure: StorageFailure | None = None

  @contextmanager
  def _io(self, *, seeking: bool = False) -> Iterator[None]:
    try:
      yield
    except OSError as error:
      if seeking and error.errno == errno.EINVAL:
        raise
      self.failure = StorageFailure(
        "archive I/O failed: " + explanation(error)
      )
      raise self.failure from None

  def readinto(self, buffer: Any) -> int:
    with self._io():
      return super().readinto(buffer)

  def readall(self) -> bytes:
    with self._io():
      return super().readall()

  def write(self, raw: Any) -> int:
    with self._io():
      return super().write(raw)

  def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
    with self._io(seeking=True):
      return super().seek(offset, whence)

  def tell(self) -> int:
    with self._io():
      return super().tell()


@contextmanager
def _opened(descriptor: int, *, write: bool = False) -> Iterator[Any]:
  raw = None

  with storage_io():
    try:
      raw = _LocalFile(descriptor, "wb" if write else "rb")
      stream = io.BufferedWriter(raw) if write else io.BufferedReader(raw)
    except BaseException:
      if raw is None:
        os.close(descriptor)
      else:
        raw.close()
      raise

  try:
    yield stream
  finally:
    with storage_io():
      stream.close()


@contextmanager
def private_input(path: Path) -> Iterator[Any]:
  with storage_io():
    descriptor = os.open(path, READ_FLAGS)

  with _opened(descriptor) as stream:
    with storage_io():
      checked_file(os.fstat(stream.fileno()))
    yield stream


@contextmanager
def decoder_input(path: Path) -> Iterator[Any]:
  with private_input(path) as stream:
    raw = stream.raw

    try:
      yield stream
    except Exception as error:
      if raw.failure is None or isinstance(error, Cancelled):
        raise
      raise raw.failure from None

    if raw.failure is not None:
      raise raw.failure


def _read_descriptor(descriptor: int, limit: int) -> bytes:
  with _opened(descriptor) as stream:
    with storage_io():
      info = os.fstat(stream.fileno())
      checked_file(info)

    require(info.st_size <= limit, "managed file too large")
    raw = stream.read(limit + 1)

  require(len(raw) <= limit, "managed file too large")
  return raw


class Tree:
  def __init__(self, root: Path, create: bool = True):
    self.root = Path(os.path.abspath(root.expanduser()))
    self.fd = -1
    self.lock_fd: int | None = None
    require(self.root != Path("/"), "cannot use filesystem root")

    descriptor = os.open("/", DIRECTORY_FLAGS)
    current = Path("/")

    try:
      for component in self.root.parts[1:]:
        try:
          child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
        except FileNotFoundError:
          require(create, "managed directory missing")
          try:
            os.mkdir(component, 0o700, dir_fd=descriptor)
            os.fsync(descriptor)
          except FileExistsError:
            pass
          child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)

        os.close(descriptor)
        descriptor = child
        current /= component
        checked_ancestor(os.fstat(descriptor), current)

      checked_directory(os.fstat(descriptor))
      self.fd = descriptor

    except BaseException:
      os.close(descriptor)
      raise

  def close(self) -> None:
    if self.lock_fd is not None:
      os.close(self.lock_fd)
      self.lock_fd = None

    if self.fd >= 0:
      os.close(self.fd)
      self.fd = -1

  @contextmanager
  def parent(
    self, relative: str, create: bool = False
  ) -> Iterator[tuple[int, str]]:
    parts = relative_parts(relative)
    descriptor = os.dup(self.fd)

    try:
      for component in parts[:-1]:
        try:
          child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
        except FileNotFoundError:
          if not create:
            raise
          try:
            os.mkdir(component, 0o700, dir_fd=descriptor)
            os.fsync(descriptor)
          except FileExistsError:
            pass
          child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)

        try:
          checked_directory(os.fstat(child))
        except BaseException:
          os.close(child)
          raise

        os.close(descriptor)
        descriptor = child

      yield descriptor, parts[-1]

    finally:
      os.close(descriptor)

  def path(self, relative: str) -> Path:
    path = self.root
    parts = relative_parts(relative)

    for index, component in enumerate(parts):
      path /= component

      try:
        info = path.lstat()
      except FileNotFoundError:
        continue

      if index < len(parts) - 1 or stat.S_ISDIR(info.st_mode):
        checked_directory(info)
      else:
        checked_file(info)

    return path

  def exists(self, relative: str) -> bool:
    try:
      with self.parent(relative) as (parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        check = checked_directory if stat.S_ISDIR(info.st_mode) else checked_file
        check(info)
      return True
    except FileNotFoundError:
      return False

  def mkdir(self, relative: str) -> Path:
    with self.parent(relative, create=True) as (parent, name):
      try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
      except FileExistsError:
        pass

      checked_directory(os.stat(name, dir_fd=parent, follow_symlinks=False))

    return self.root / relative

  def lock(self, relative: str = ".lock") -> None:
    require(self.lock_fd is None, "already locked")

    with self.parent(relative) as (parent, name):
      descriptor = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600,
        dir_fd=parent,
      )

    try:
      checked_file(os.fstat(descriptor))
      fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      os.close(descriptor)
      raise StorageFailure("directory locked by another process") from None
    except BaseException:
      os.close(descriptor)
      raise

    self.lock_fd = descriptor

  def read(self, relative: str, limit: int) -> bytes:
    require(type(limit) is int and limit >= 0, "invalid read limit")

    with self.parent(relative) as (parent, name):
      descriptor = os.open(name, READ_FLAGS, dir_fd=parent)

    return _read_descriptor(descriptor, limit)

  @contextmanager
  def atomic(self, relative: str) -> Iterator[Any]:
    with self.parent(relative, create=True) as (parent, name):
      self._check_destination(parent, name)
      temporary = ".write-" + token()
      descriptor = os.open(
        temporary, CREATE_FLAGS, 0o600, dir_fd=parent
      )
      installed = False

      try:
        with os.fdopen(descriptor, "wb") as stream:
          yield stream
          stream.flush()
          os.fsync(stream.fileno())

        self._check_destination(parent, name)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        installed = True
        os.fsync(parent)

      except OSError as error:
        detail = explanation(error)
        if installed:
          raise StorageFailure(
            "file replaced but directory synchronization failed: " + detail
          ) from None
        raise StorageFailure("archive I/O failed: " + detail) from None

      finally:
        if not installed:
          with storage_io():
            try:
              os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
              pass

  @staticmethod
  def _check_destination(parent: int, name: str) -> None:
    try:
      checked_file(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
      pass

  def write(self, relative: str, raw: bytes) -> None:
    with self.atomic(relative) as stream:
      stream.write(raw)

  def remove(self, relative: str) -> None:
    try:
      with self.parent(relative) as (parent, name):
        try:
          checked_file(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
          return

        os.unlink(name, dir_fd=parent)
        os.fsync(parent)

    except FileNotFoundError:
      return

  def move(self, source: str, target: str) -> None:
    with self.parent(source) as (src, src_name):
      info = os.stat(src_name, dir_fd=src, follow_symlinks=False)
      check = checked_directory if stat.S_ISDIR(info.st_mode) else checked_file
      check(info)

      with self.parent(target, create=True) as (dst, dst_name):
        try:
          os.stat(dst_name, dir_fd=dst, follow_symlinks=False)
        except FileNotFoundError:
          pass
        else:
          raise StorageFailure("publication destination exists")

        os.rename(src_name, dst_name, src_dir_fd=src, dst_dir_fd=dst)
        os.fsync(src)
        os.fsync(dst)

  def clean_atomic(self, relative: str | None = None) -> None:
    directory = self.root if relative is None else self.path(relative)

    try:
      checked_directory(directory.lstat())
    except FileNotFoundError:
      return

    for path in directory.iterdir():
      if re.fullmatch(r"\.write-[0-9a-f]{32}", path.name):
        self.remove(path.relative_to(self.root).as_posix())


def read_private(path: Path, limit: int) -> bytes:
  require(type(limit) is int and limit >= 0, "invalid read limit")
  return _read_descriptor(os.open(path, READ_FLAGS), limit)


def _staging_parents(directory: Path) -> None:
  missing = []
  current = directory

  while True:
    try:
      checked_directory(current.lstat())
      break
    except FileNotFoundError:
      missing.append(current)
      current = current.parent

  for path in reversed(missing):
    try:
      path.mkdir(mode=0o700)
    except FileExistsError:
      pass
    checked_directory(path.lstat())


@contextmanager
def staging_output(path: Path) -> Iterator[Any]:
  with storage_io():
    _staging_parents(path.parent)
    descriptor = os.open(path, CREATE_FLAGS, 0o600)

  with _opened(descriptor, write=True) as stream:
    yield stream

    with storage_io():
      stream.flush()
      os.fsync(stream.fileno())


def write_new(path: Path, raw: bytes) -> None:
  with staging_output(path) as stream:
    stream.write(raw)


def sync_directory(path: Path) -> None:
  descriptor = os.open(path, DIRECTORY_FLAGS)

  try:
    checked_directory(os.fstat(descriptor))
    os.fsync(descriptor)
  finally:
    os.close(descriptor)


def sync_tree(path: Path) -> None:
  info = path.lstat()

  if stat.S_ISREG(info.st_mode):
    descriptor = os.open(path, READ_FLAGS)
    try:
      checked_file(os.fstat(descriptor))
      os.fsync(descriptor)
    finally:
      os.close(descriptor)
    return

  checked_directory(info)

  for directory, directories, files in os.walk(
    path, topdown=False, followlinks=False
  ):
    checked_directory(Path(directory).lstat())

    for name in directories:
      checked_directory((Path(directory) / name).lstat())

    for name in files:
      sync_tree(Path(directory) / name)

    sync_directory(Path(directory))


def remove_work(root: Path, relative: str) -> None:
  require(
    re.fullmatch(r"\.ctl/work/[0-9a-f]{32}", relative),
    "invalid work directory",
  )
  path = root

  try:
    checked_directory(path.lstat())
    for component in relative_parts(relative):
      path /= component
      checked_directory(path.lstat())
  except FileNotFoundError:
    return

  for directory, directories, files in os.walk(path, followlinks=False):
    checked_directory(Path(directory).lstat())

    for name in directories:
      checked_directory((Path(directory) / name).lstat())

    for name in files:
      checked_file((Path(directory) / name).lstat())

  shutil.rmtree(path)
  sync_directory(path.parent)


def signature(path: Path) -> dict[str, int]:
  info = path.lstat()
  checked_file(info)
  return {
    "size": info.st_size,
    "mtime": info.st_mtime_ns,
    "ctime": info.st_ctime_ns,
    "inode": info.st_ino,
    "device": info.st_dev,
  }


def file_digest(path: Path) -> str:
  hasher = hashlib.sha256()
  descriptor = os.open(path, READ_FLAGS)

  with _opened(descriptor) as stream:
    with storage_io():
      checked_file(os.fstat(stream.fileno()))

    while chunk := stream.read(1024 * 1024):
      hasher.update(chunk)

  return hasher.hexdigest()


def checked_local(root: Path, relative: str) -> Path:
  checked_directory(root.lstat())
  path = root
  parts = relative_parts(relative)

  for index, component in enumerate(parts):
    path /= component
    info = path.lstat()
    check = checked_file if index == len(parts) - 1 else checked_directory
    check(info)

  return path


def document_source(root: Path, record: dict[str, Any]) -> Path:
  return root / record["folder"] / record["original"]


def source_signature(root: Path, record: dict[str, Any]) -> dict[str, int]:
  try:
    path = checked_local(
      root, join_path(record["folder"], record["original"])
    )
    return signature(path)
  except FileNotFoundError:
    raise SourceProblem(
      "original missing; refresh the archive with sync --refresh"
    ) from None


def verify_source(root: Path, record: dict[str, Any]) -> dict[str, int]:
  current = source_signature(root, record)

  if current["size"] != record["bytes"]:
    raise SourceProblem(
      "original size changed; refresh the archive with sync --refresh"
    )

  if current != record["signature"]:
    if file_digest(document_source(root, record)) != record["sha256"]:
      raise SourceProblem(
        "original contents changed; refresh the archive with sync --refresh"
      )
    if source_signature(root, record) != current:
      raise SourceProblem("original changed during verification")

  return current


def check_job_source(spec: dict[str, Any]) -> None:
  current = source_signature(Path(spec["root"]), spec["document"])
  if current != spec["signature"]:
    raise SourceProblem("original changed during processing")
