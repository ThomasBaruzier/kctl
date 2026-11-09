from __future__ import annotations

import ctypes
import gzip
import hashlib
import io
import os
import stat
import tarfile
import zipfile

from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .core import (
  ASSET_LIMIT,
  CONTENT_LIMIT,
  DEPTH_LIMIT,
  ENTRY_LIMIT,
  FILE_LIMIT,
  IMAGE_LIMIT,
  OFFICE_SUFFIXES,
  TAR_SUFFIXES,
  TEXT_KINDS,
  TEXT_LIMIT,
  BudgetReached,
  Cancelled,
  Failure,
  LimitReached,
  ProtectionFailure,
  StorageFailure,
  bundle_name,
  decode_text,
  display,
  explanation,
  join_path,
  named_kind,
  numbered,
  original_name,
  parent_path,
  public_path,
  relative_parts,
  require,
  retryable,
  token,
)
from .storage import (
  checked_directory,
  checked_file,
  decoder_input,
  file_digest,
  read_private,
  staging_output,
  storage_io,
)


class _InvalidContent(Failure):
  pass


def _input_error(error: BaseException) -> tuple[str, bool]:
  if isinstance(error, _InvalidContent):
    return error.message, False

  for kind, description in (
    (zipfile.BadZipFile, "ZIP integrity check failed"),
    (tarfile.ReadError, "TAR structure could not be read"),
    (gzip.BadGzipFile, "gzip integrity check failed"),
  ):
    if isinstance(error, kind):
      return description + ": " + display(error, 512), False

  if isinstance(error, EOFError):
    return "input ended unexpectedly", False

  return explanation(error), retryable(error)


@dataclass
class Blob:
  path: Path
  size: int
  offset: int = 0

  def __post_init__(self) -> None:
    require(
      type(self.size) is int
      and type(self.offset) is int
      and 0 <= self.offset <= self.size,
      "invalid content blob boundaries",
    )

  @property
  def length(self) -> int:
    return self.size - self.offset


class LimitedWriter:
  def __init__(self, stream: Any, limit: int, allowance: Any = None):
    self.stream = stream
    self.limit = limit
    self.allowance = allowance
    self.size = 0

  def write(self, raw: bytes) -> int:
    proposed = self.size + len(raw)

    if proposed > self.limit:
      raise LimitReached("generated or extracted file exceeds size limit")

    if self.allowance is not None:
      self.allowance(proposed)

    with storage_io():
      written = self.stream.write(raw)

    if written != len(raw):
      raise StorageFailure("incomplete local write")

    self.size = proposed
    return written

  def tell(self) -> int:
    return self.size

  def flush(self) -> None:
    with storage_io():
      self.stream.flush()


def _unlink(path: Path) -> None:
  with storage_io():
    try:
      checked_file(path.lstat())
    except FileNotFoundError:
      return
    path.unlink()


def _walk_error(error: OSError) -> None:
  raise error


class Output:
  def __init__(
    self,
    root: Path,
    existing: dict[str, Blob] | None = None,
    protected: dict[str, Blob] | None = None,
    protected_images: set[str] | None = None,
  ):
    with storage_io():
      checked_directory(root.lstat())

    self.root = root
    self.protected = dict(protected or {})
    self.existing = {**(existing or {}), **self.protected}
    self.protected_images = set(protected_images or set())
    self.files: dict[str, Blob] = {}
    self.claimed: dict[str, Blob] = {}
    self.image_paths: set[str] = set()
    self.text_bytes = 0
    self.maximum = CONTENT_LIMIT - sum(
      blob.length for blob in self.protected.values()
    )

    if self.maximum < 0:
      raise BudgetReached("required preserved content exceeds root budget")
    if len(self.protected_images) > IMAGE_LIMIT:
      raise BudgetReached("required preserved images exceed root image limit")

  @property
  def used(self) -> int:
    return sum(
      blob.length
      for path, blob in self.claimed.items()
      if path not in self.protected
    )

  @property
  def available(self) -> int:
    return max(0, self.maximum - self.used - self.text_bytes)

  def room(self, additional: int) -> None:
    if self.used + self.text_bytes + additional > self.maximum:
      raise BudgetReached("remaining extracted/generated content budget reached")

  def resolve(self, relative: str) -> Blob:
    if relative in self.files:
      return self.files[relative]
    return self.existing[relative]

  def claim(self, relative: str, image: bool = False) -> Blob:
    public_path(relative)
    blob = self.resolve(relative)

    if relative not in self.claimed and relative not in self.protected:
      self.room(blob.length)

    if image:
      images = self.protected_images | self.image_paths | {relative}
      if len(images) > IMAGE_LIMIT:
        raise BudgetReached("root generated-image limit reached")

    self.claimed[relative] = blob

    if image:
      self.image_paths.add(relative)

    return blob

  def rollback(self, paths: set[str], images: set[str]) -> None:
    self.claimed = {
      path: blob for path, blob in self.claimed.items() if path in paths
    }
    self.image_paths = set(images)

    for relative, blob in list(self.files.items()):
      if relative in self.claimed or relative in self.protected:
        continue
      _unlink(blob.path)
      del self.files[relative]

  @contextmanager
  def open(self, relative: str, limit: int = FILE_LIMIT) -> Iterator[Any]:
    public_path(relative)
    require(relative not in self.protected, "cannot replace protected output")
    require(
      relative not in self.files and relative not in self.claimed,
      "duplicate staged output",
    )
    path = self.root / relative
    created = False
    complete = False

    try:
      with staging_output(path) as stream:
        created = True
        writer = LimitedWriter(stream, limit, self.room)
        yield writer

      blob = Blob(path, writer.size)
      self.files[relative] = blob
      self.claimed[relative] = blob
      complete = True

    finally:
      if created and not complete:
        _unlink(path)

  def write_text(self, relative: str, raw: bytes) -> None:
    self.text_bytes = 0

    with self.open(relative, TEXT_LIMIT - 8192) as stream:
      stream.write(raw)

  def _asset_directory(self, relative: str) -> Path:
    directory = self.root

    with storage_io():
      checked_directory(directory.lstat())

      for component in relative_parts(parent_path(relative)):
        directory /= component
        try:
          directory.mkdir(mode=0o700)
        except FileExistsError:
          pass
        checked_directory(directory.lstat())

    return directory

  def asset(self, folder: str, suffix: str, action: Any) -> str:
    require(
      suffix in {".png", ".svg", ".wmf", ".emf"},
      "unsupported generated asset suffix",
    )
    if folder:
      public_path(folder)

    temporary = self.root / (".asset-" + token())
    created = False

    try:
      with staging_output(temporary) as stream:
        created = True
        writer = LimitedWriter(stream, FILE_LIMIT)
        action(writer)

      with storage_io():
        key = file_digest(temporary)

      relative = join_path(folder, "images/" + key + suffix)

      if relative in self.files or relative in self.existing:
        self.claim(relative, image=True)
        return relative

      blob = Blob(temporary, writer.size)
      self.files[relative] = blob

      try:
        self.claim(relative, image=True)
        directory = self._asset_directory(relative)
        target = directory / (key + suffix)

        with storage_io():
          if os.path.lexists(target):
            raise StorageFailure("untracked generated asset destination")
          os.rename(temporary, target)

        blob.path = target

      except BaseException:
        self.files.pop(relative, None)
        self.claimed.pop(relative, None)
        self.image_paths.discard(relative)
        raise

      return relative

    finally:
      if created:
        _unlink(temporary)

  def cleanup(self, keep: set[Path]) -> None:
    with storage_io():
      for directory, directories, files in os.walk(
        self.root,
        topdown=False,
        followlinks=False,
        onerror=_walk_error,
      ):
        checked_directory(Path(directory).lstat())

        for name in files:
          path = Path(directory) / name
          checked_file(path.lstat())
          if path not in keep:
            path.unlink()

        for name in directories:
          path = Path(directory) / name
          checked_directory(path.lstat())
          if not any(path.iterdir()):
            path.rmdir()


class Markdown:
  def __init__(self, output: Output):
    self.output = output
    self.parts: list[str] = []
    self.appendix: list[str] = []
    self.size = 0

  def add(self, *parts: str, appendix: bool = False) -> None:
    target = self.appendix if appendix else self.parts
    other = self.parts if appendix else self.appendix
    additional = sum(len(part.encode()) + 1 for part in parts)

    if parts and not target and other:
      additional += 1

    if self.size + additional > TEXT_LIMIT - 8192:
      raise LimitReached("generated Markdown reaches text limit")

    self.output.room(additional)
    target.extend(parts)
    self.size += additional
    self.output.text_bytes = self.size

  @staticmethod
  def _render(parts: list[str]) -> bytes:
    text = "\n".join(parts).rstrip("\n")
    return (text + "\n").encode() if text else b""

  def content(self) -> bytes:
    return self._render(self.parts)

  def bytes(self) -> bytes:
    content = self.content()
    appendix = self._render(self.appendix)
    separator = b"\n" if content and appendix else b""
    return content + separator + appendix


class WalkBudget:
  def __init__(self):
    self.entries = 0
    self.expanded = 0
    self.stopped = False

  def entry(self) -> None:
    if self.entries >= ENTRY_LIMIT:
      self.stopped = True
      raise LimitReached("container entry limit reached")
    self.entries += 1

  def expansion(self, size: int) -> None:
    if self.expanded + size > ASSET_LIMIT:
      self.stopped = True
      raise LimitReached("archive expansion exceeds root byte limit")
    self.expanded += size


class ExpansionReader:
  def __init__(self, stream: Any, budget: WalkBudget):
    self.stream = stream
    self.budget = budget

  def read(self, size: int = -1) -> bytes:
    remaining = ASSET_LIMIT - self.budget.expanded
    amount = min(size if size >= 0 else 128 * 1024, remaining + 1)
    raw = self.stream.read(amount)
    self.budget.expansion(len(raw))
    return raw


class FrameReader:
  def __init__(self, source: Any, kind: str, budget: WalkBudget):
    require(kind in {"tar-zstd", "tar-lz4"}, "unsupported compressed frame")
    self.source = source
    self.kind = kind
    self.budget = budget
    self.decoder = None
    self.pending = b""
    self.buffer = b""
    self.position = 0
    self.finished = False

  def new_decoder(self) -> Any:
    if self.kind == "tar-zstd":
      import zstandard

      return zstandard.ZstdDecompressor(
        max_window_size=ASSET_LIMIT // 1024
      ).decompressobj()

    import lz4.frame

    return lz4.frame.LZ4FrameDecompressor()

  def read(self, size: int = -1) -> bytes:
    if size < 0:
      size = 128 * 1024
    if size == 0:
      return b""

    while self.position == len(self.buffer) and not self.finished:
      self.buffer = b""
      self.position = 0
      draining = bool(
        self.kind == "tar-lz4"
        and self.decoder is not None
        and not self.decoder.eof
        and not self.decoder.needs_input
      )
      raw = b"" if draining else self.pending or self.source.read(1024)
      self.pending = b""

      if not raw and not draining:
        if self.decoder is not None and not self.decoder.eof:
          raise _InvalidContent("truncated compressed TAR frame")
        self.finished = True
        break

      if self.decoder is None or self.decoder.eof:
        self.decoder = self.new_decoder()

      if self.kind == "tar-lz4":
        decoded = self.decoder.decompress(raw, max_length=128 * 1024)
      else:
        decoded = self.decoder.decompress(raw)

      self.budget.expansion(len(decoded))
      self.buffer = decoded

      if self.decoder.eof:
        self.pending = self.decoder.unused_data or b""

    end = min(len(self.buffer), self.position + size)
    raw = self.buffer[self.position:end]
    self.position = end
    return raw


@contextmanager
def tar_stream(
  path: Path, kind: str, budget: WalkBudget
) -> Iterator[Any]:
  with decoder_input(path) as source:
    if kind in {"tar-zstd", "tar-lz4"}:
      yield FrameReader(source, kind, budget)
      return

    prefix = source.read(6)
    source.seek(0)

    if prefix.startswith(b"\x1f\x8b"):
      decoded = gzip.GzipFile(fileobj=source)
    elif prefix.startswith(b"BZh"):
      import bz2

      decoded = bz2.BZ2File(source)
    elif prefix.startswith(b"\xfd7zXZ\x00"):
      import lzma

      decoded = lzma.LZMAFile(source)
    else:
      decoded = None

    if decoded is None:
      yield ExpansionReader(source, budget)
    else:
      with decoded:
        yield ExpansionReader(decoded, budget)


def _zip_kind(stream: Any, suffix: str) -> str:
  with zipfile.ZipFile(stream) as archive:
    try:
      archive.getinfo("[Content_Types].xml")
    except KeyError:
      return suffix if suffix in OFFICE_SUFFIXES else "zip"

    for marker, kind in (
      ("word/document.xml", ".docx"),
      ("ppt/presentation.xml", ".pptx"),
      ("xl/workbook.xml", ".xlsx"),
    ):
      try:
        archive.getinfo(marker)
      except KeyError:
        continue
      return kind

  return suffix if suffix in OFFICE_SUFFIXES else "zip"


def local_kind(path: Path, name: str | None = None) -> str:
  with decoder_input(path) as stream:
    prefix = stream.read(512)
    suffix = path.suffix.lower()
    lower = path.name.lower()

    for magic, kind in (
      (b"%PDF-", ".pdf"),
      (b"\x89PNG\r\n\x1a\n", ".png"),
      (b"\xff\xd8\xff", ".jpg"),
      (b"GIF8", ".gif"),
      (b"II*\x00", ".tif"),
      (b"MM\x00*", ".tif"),
      (b"BM", ".bmp"),
      (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "unsupported"),
    ):
      if prefix.startswith(magic):
        return kind

    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
      return ".webp"

    if prefix.startswith(b"PK"):
      stream.seek(0)
      return _zip_kind(stream, suffix)

  if prefix.startswith(b"\x28\xb5\x2f\xfd") or lower.endswith(
    (".tar.zst", ".tar.zstd", ".tzst")
  ):
    return "tar-zstd"

  if prefix.startswith(b"\x04\x22\x4d\x18") or lower.endswith(".tar.lz4"):
    return "tar-lz4"

  if (
    lower.endswith(TAR_SUFFIXES)
    or prefix.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00"))
    or prefix[257:262] == b"ustar"
  ):
    return "tar"

  kind = named_kind(path.name)
  return named_kind(name) if kind == "unsupported" and name else kind


class ContentWalker:
  def __init__(self, output: Output):
    self.output = output
    self.budget = WalkBudget()
    self.nodes: dict[str, dict[str, Any]] = {}
    self.allocated: set[str] = set()

  def problem(
    self,
    node: dict[str, Any],
    message: str,
    retry: bool = False,
  ) -> None:
    message = display(message)

    if message not in node["problems"] and len(node["problems"]) < ENTRY_LIMIT + 20:
      node["problems"].append(message)

    node["retry"] |= retry

  def allocate(self, parent: str, name: str) -> str:
    base = bundle_name(name, 48)
    number = 1

    while True:
      candidate = join_path(
        join_path(parent, "contents"),
        numbered(base, number),
      )
      if candidate.casefold() not in self.allocated:
        self.allocated.add(candidate.casefold())
        return candidate
      number += 1

  def _validate_text(self, node: dict[str, Any], path: Path) -> None:
    try:
      if node["bytes"] > TEXT_LIMIT:
        raise LimitReached("text original exceeds text limit")

      with storage_io():
        raw = read_private(path, TEXT_LIMIT)

      text = decode_text(raw)
      if len(text.encode()) > TEXT_LIMIT:
        raise LimitReached("decoded text exceeds text limit")

    except (StorageFailure, ProtectionFailure):
      raise
    except Failure as error:
      self.problem(node, "text inspection: " + error.message)

  def node(
    self,
    folder: str,
    name: str,
    original: str,
    size: int,
    sha256: str,
  ) -> dict[str, Any]:
    require(folder not in self.nodes, "duplicate content node")
    node = {
      "name": display(name),
      "original": original,
      "bytes": size,
      "sha256": sha256,
      "kind": "unsupported",
      "children": [],
      "problems": [],
      "retry": False,
      "visuals": None,
      "products": {},
      "attempts": {},
    }
    self.nodes[folder] = node
    path = self.output.root / join_path(folder, original)

    try:
      node["kind"] = local_kind(path, name)
    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except Exception as error:
      node["kind"] = named_kind(original)
      if node["kind"] == "unsupported":
        node["kind"] = named_kind(name)
      message, retry = _input_error(error)
      self.problem(node, "type inspection failed: " + message, retry)

    if node["kind"] in TEXT_KINDS:
      self._validate_text(node, path)

    return node

  def save_child(
    self,
    parent: str,
    name: str,
    size: int,
    opener: Any,
    depth: int,
    counted: bool = False,
  ) -> None:
    owner = self.nodes[parent]
    folder = self.allocate(parent, name)
    original = original_name(name)
    relative = join_path(folder, original)

    if size < 0 or size > FILE_LIMIT:
      self.problem(owner, f"{display(name, 512)}: exceeds member size limit")
      return

    try:
      self.output.room(size)
      hasher = hashlib.sha256()
      total = 0

      with opener() as source:
        require(source is not None, "member stream unavailable")

        with self.output.open(relative) as target:
          while True:
            chunk = source.read(min(128 * 1024, size + 1 - total))
            if not chunk:
              break

            total += len(chunk)
            if not counted:
              self.budget.expansion(len(chunk))

            if total > size:
              raise _InvalidContent("member size mismatch")
            target.write(chunk)
            hasher.update(chunk)

          if total != size:
            raise _InvalidContent("member size mismatch")

      self.node(folder, name, original, total, hasher.hexdigest())
      owner["children"].append(folder)
      self.walk(folder, depth + 1)

    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except LimitReached as error:
      self.problem(
        owner, f"{display(name, 512)}: {error.message}", retryable(error)
      )
      if isinstance(error, BudgetReached):
        self.budget.stopped = True
    except Exception as error:
      message, retry = _input_error(error)
      self.problem(owner, f"{display(name, 512)}: {message}", retry)

  def walk_zip(self, folder: str, depth: int, office: bool) -> None:
    node = self.nodes[folder]
    path = self.output.root / join_path(folder, node["original"])

    with decoder_input(path) as source, zipfile.ZipFile(source) as archive:
      if office and len(archive.infolist()) > 10000:
        raise LimitReached("Office container has too many entries")

      for member in archive.infolist():
        if office and "/embeddings/" not in member.filename:
          continue

        if self.budget.stopped:
          self.problem(node, "remaining contents omitted: traversal budget reached")
          break

        self.budget.entry()

        if member.is_dir():
          continue

        kind = stat.S_IFMT(member.external_attr >> 16)
        if kind not in {0, stat.S_IFREG}:
          self.problem(
            node, f"{display(member.filename, 512)}: special entry skipped"
          )
          continue

        if member.flag_bits & 1:
          self.problem(
            node, f"{display(member.filename, 512)}: encrypted member"
          )
          continue

        if depth >= DEPTH_LIMIT:
          self.problem(node, "nested contents omitted: depth limit reached")
          break

        self.save_child(
          folder,
          member.filename,
          member.file_size,
          lambda member=member: archive.open(member),
          depth,
        )

  def walk_tar(self, folder: str, depth: int) -> None:
    node = self.nodes[folder]
    path = self.output.root / join_path(folder, node["original"])
    complete = True

    with tar_stream(path, node["kind"], self.budget) as stream:
      with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
          if self.budget.stopped:
            self.problem(
              node, "remaining contents omitted: traversal budget reached"
            )
            complete = False
            break

          self.budget.entry()

          if member.isdir():
            continue

          if not member.isfile() or member.issparse():
            self.problem(
              node, f"{display(member.name, 512)}: special entry skipped"
            )
            continue

          if depth >= DEPTH_LIMIT:
            self.problem(node, "nested contents omitted: depth limit reached")
            complete = False
            break

          def open_member(member: Any = member) -> Any:
            extracted = archive.extractfile(member)
            require(extracted is not None, "member stream unavailable")
            return closing(extracted)

          self.save_child(
            folder, member.name, member.size, open_member, depth, counted=True
          )

          if self.budget.stopped:
            complete = False
            break

      if complete and not self.budget.stopped:
        while stream.read(128 * 1024):
          pass

  def walk_pdf(self, folder: str, depth: int) -> None:
    import pypdfium2 as pdfium

    node = self.nodes[folder]
    path = self.output.root / join_path(folder, node["original"])

    with (
      decoder_input(path) as source,
      pdfium.PdfDocument(source) as document,
    ):
      for index in range(document.count_attachments()):
        if self.budget.stopped:
          self.problem(
            node, "remaining attachments omitted: traversal budget reached"
          )
          break

        self.budget.entry()

        if depth >= DEPTH_LIMIT:
          self.problem(node, "nested attachments omitted: depth limit reached")
          break

        try:
          attachment = document.get_attachment(index)
          name = attachment.get_name() or f"attachment-{index + 1}"
          size = ctypes.c_ulong()
          require(
            pdfium.raw.FPDFAttachment_GetFile(attachment, None, 0, size),
            "cannot determine attachment size",
          )

          def open_attachment() -> Any:
            self.output.room(size.value)
            if size.value > FILE_LIMIT:
              raise LimitReached("PDF attachment exceeds member size limit")
            raw = bytes(attachment.get_data()) if size.value else b""
            return io.BytesIO(raw)

          self.save_child(
            folder, name, size.value, open_attachment, depth
          )

        except (StorageFailure, ProtectionFailure, Cancelled, LimitReached):
          raise
        except Exception as error:
          message, retry = _input_error(error)
          self.problem(node, f"attachment {index + 1}: {message}", retry)

  def walk(self, folder: str, depth: int = 0) -> None:
    node = self.nodes[folder]

    try:
      if node["kind"] == "zip":
        self.walk_zip(folder, depth, office=False)
      elif node["kind"] in OFFICE_SUFFIXES:
        self.walk_zip(folder, depth, office=True)
      elif node["kind"] in {"tar", "tar-zstd", "tar-lz4"}:
        self.walk_tar(folder, depth)
      elif node["kind"] == ".pdf":
        self.walk_pdf(folder, depth)

    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except LimitReached as error:
      self.problem(node, error.message, retryable(error))
      if isinstance(error, BudgetReached):
        self.budget.stopped = True
    except Exception as error:
      message, retry = _input_error(error)
      self.problem(node, "content extraction failed: " + message, retry)
