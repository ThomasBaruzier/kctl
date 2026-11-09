import bz2
import gzip
import io
import lzma
import os
import stat
import sys
import tarfile
import zipfile

from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

import kctl.content as content

from kctl.content import (
  Blob,
  ContentWalker,
  ExpansionReader,
  FrameReader,
  LimitedWriter,
  Markdown,
  Output,
  WalkBudget,
  local_kind,
  tar_stream,
)
from kctl.core import (
  BudgetReached,
  Cancelled,
  Failure,
  LimitReached,
  ProtectionFailure,
  StorageFailure,
  digest,
  original_name,
)
from kctl.storage import checked_directory, checked_file, write_new


def zip_bytes(entries):
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name, raw in entries:
      archive.writestr(name, raw)
  return stream.getvalue()


def damaged_zip(entries, damaged):
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
    for name, raw in entries:
      archive.writestr(name, raw)
    member = archive.getinfo(damaged)
    offset = (
      member.header_offset + 30
      + len(member.filename.encode()) + len(member.extra)
    )

  raw = bytearray(stream.getvalue())
  raw[offset] ^= 1
  return bytes(raw)


def tar_bytes(entries):
  stream = io.BytesIO()
  with tarfile.open(fileobj=stream, mode="w") as archive:
    for name, raw in entries:
      member = tarfile.TarInfo(name)
      member.size = len(raw)
      archive.addfile(member, io.BytesIO(raw))
  return stream.getvalue()


def walk(root, name, raw):
  root.mkdir(mode=0o700)
  original = original_name(name)
  write_new(root / original, raw)
  output = Output(root)
  walker = ContentWalker(output)
  walker.node("", name, original, len(raw), digest(raw))
  walker.walk("")
  return walker, output


def members(walker):
  return [
    walker.nodes[folder]
    for folder in walker.nodes[""]["children"]
  ]


def member_bytes(walker, output, name):
  for folder, node in walker.nodes.items():
    if folder and node["name"] == name:
      return output.resolve(folder + "/" + node["original"]).path.read_bytes()
  pytest.fail(f"member not found: {name}")


def read_all(stream, size=97):
  chunks = []
  while chunk := stream.read(size):
    chunks.append(chunk)
  return b"".join(chunks)


@pytest.fixture
def output(private_dir):
  return Output(private_dir)


def test_blob_lengths_exclude_generated_prefix(private_dir):
  blob = Blob(private_dir / "text.md", 100, 20)

  assert blob.length == 80


@pytest.mark.parametrize(
  ("size", "offset"),
  [(-1, 0), (1, -1), (1, 2), (True, 0), (1, True)],
)
def test_blob_boundaries_are_validated(private_dir, size, offset):
  with pytest.raises(Failure, match="boundaries"):
    Blob(private_dir / "text.md", size, offset)


def test_limited_writer_checks_size_before_writing():
  stream = io.BytesIO()
  writer = LimitedWriter(stream, 4)

  assert writer.write(b"ab") == 2
  assert writer.tell() == 2
  assert writer.write(b"cd") == 2

  with pytest.raises(LimitReached):
    writer.write(b"e")

  assert stream.getvalue() == b"abcd"
  assert writer.tell() == 4


def test_limited_writer_reports_cumulative_proposals_to_allowance():
  proposals = []
  stream = io.BytesIO()
  writer = LimitedWriter(stream, 20, proposals.append)

  writer.write(b"one")
  writer.write(b"two")
  writer.flush()

  assert proposals == [3, 6]


def test_short_local_write_is_a_storage_failure():
  class ShortWriter:
    def write(self, raw):
      return len(raw) - 1

  writer = LimitedWriter(ShortWriter(), 10)

  with pytest.raises(StorageFailure, match="incomplete"):
    writer.write(b"data")

  assert writer.size == 0


def test_os_write_errors_are_storage_failures():
  class BrokenWriter:
    def write(self, raw):
      raise OSError("private filesystem details")

  with pytest.raises(StorageFailure, match="I/O failed"):
    LimitedWriter(BrokenWriter(), 10).write(b"data")


def test_output_registers_only_completed_private_files(output):
  with output.open("contents/example/source.py") as stream:
    stream.write(b"print(1)\n")
    assert output.files == {}

  relative = "contents/example/source.py"
  blob = output.files[relative]
  assert blob.path.read_bytes() == b"print(1)\n"
  assert output.claimed[relative] is blob
  assert output.used == len(b"print(1)\n")
  checked_file(blob.path.lstat())
  checked_directory(blob.path.parent.lstat())


def test_failed_output_is_removed_and_not_claimed(output):
  with pytest.raises(Failure, match="decoder"):
    with output.open("value.txt") as stream:
      stream.write(b"partial")
      raise Failure("decoder failed")

  assert not (output.root / "value.txt").exists()
  assert output.files == {}
  assert output.claimed == {}


def test_failed_open_does_not_delete_an_existing_destination(output):
  write_new(output.root / "value.txt", b"keep")

  with pytest.raises(StorageFailure):
    with output.open("value.txt") as stream:
      stream.write(b"replace")

  assert (output.root / "value.txt").read_bytes() == b"keep"
  assert output.files == {}


def test_duplicate_staged_path_is_rejected(output):
  with output.open("value.txt") as stream:
    stream.write(b"first")

  with pytest.raises(Failure, match="duplicate"):
    with output.open("value.txt"):
      pass

  assert (output.root / "value.txt").read_bytes() == b"first"


def test_output_budget_is_checked_during_streaming(private_dir, monkeypatch):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 5)
  output = Output(private_dir)

  with pytest.raises(BudgetReached):
    with output.open("value.txt") as stream:
      stream.write(b"123")
      stream.write(b"456")

  assert output.files == {}
  assert not (private_dir / "value.txt").exists()


def test_existing_file_claim_is_counted_once(private_dir):
  path = private_dir / "source.txt"
  write_new(path, b"source")
  output = Output(private_dir, existing={"source.txt": Blob(path, 6)})

  first = output.claim("source.txt")
  second = output.claim("source.txt")

  assert first is second
  assert output.used == 6
  assert output.files == {}


def test_protected_outputs_consume_budget_without_double_counting(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 10)
  path = private_dir / "source.txt"
  write_new(path, b"source")
  output = Output(private_dir, protected={"source.txt": Blob(path, 6)})

  assert output.available == 4
  output.claim("source.txt")
  assert output.used == 0
  assert output.available == 4

  with pytest.raises(Failure, match="protected"):
    with output.open("source.txt"):
      pass

  with output.open("new.txt") as stream:
    stream.write(b"1234")

  assert output.available == 0


def test_oversized_preserved_content_fails_before_processing(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 3)

  with pytest.raises(BudgetReached, match="preserved content"):
    Output(
      private_dir,
      protected={"large": Blob(private_dir / "large", 4)},
    )


def test_assets_are_named_by_content_and_deduplicated(output):
  raw = b"normalized raster bytes"
  expected = "images/" + digest(raw) + ".png"

  first = output.asset("", ".png", lambda stream: stream.write(raw))
  second = output.asset("", ".png", lambda stream: stream.write(raw))

  assert first == second == expected
  assert output.files[expected].path.read_bytes() == raw
  assert output.used == len(raw)
  assert output.image_paths == {expected}
  assert not list(output.root.glob(".asset-*"))


def test_nested_asset_directories_remain_private_with_permissive_umask(output):
  previous = os.umask(0o022)

  try:
    relative = output.asset(
      "contents/document", ".png", lambda stream: stream.write(b"image")
    )
  finally:
    os.umask(previous)

  path = output.root / relative
  checked_file(path.lstat())
  checked_directory(path.parent.lstat())
  checked_directory(path.parent.parent.lstat())
  checked_directory(path.parent.parent.parent.lstat())


def test_identical_protected_asset_is_reused_when_no_budget_remains(
  private_dir, monkeypatch
):
  raw = b"image"
  name = "images/" + digest(raw) + ".png"
  source = private_dir / "existing.png"
  write_new(source, raw)
  candidate = private_dir / "candidate"
  candidate.mkdir(mode=0o700)
  monkeypatch.setattr(content, "CONTENT_LIMIT", len(raw))
  output = Output(
    candidate,
    protected={name: Blob(source, len(raw))},
    protected_images={name},
  )

  assert output.available == 0
  assert output.asset("", ".png", lambda stream: stream.write(raw)) == name
  assert output.resolve(name).path == source
  assert output.files == {}
  assert output.used == 0
  assert not list(candidate.glob(".asset-*"))


def test_asset_encoding_failure_removes_temporary_file(output):
  def fail(stream):
    stream.write(b"partial")
    raise Failure("encoding failed")

  with pytest.raises(Failure, match="encoding"):
    output.asset("", ".png", fail)

  assert output.files == {}
  assert output.claimed == {}
  assert not list(output.root.glob(".asset-*"))


def test_asset_budget_failure_does_not_leave_registered_output(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 2)
  output = Output(private_dir)

  with pytest.raises(BudgetReached):
    output.asset("", ".png", lambda stream: stream.write(b"large"))

  assert output.files == {}
  assert output.claimed == {}
  assert output.image_paths == set()
  assert not list(private_dir.glob(".asset-*"))


def test_asset_image_limit_counts_unique_assets(private_dir, monkeypatch):
  monkeypatch.setattr(content, "IMAGE_LIMIT", 1)
  output = Output(private_dir)
  first = output.asset("", ".png", lambda stream: stream.write(b"one"))

  assert output.asset("", ".png", lambda stream: stream.write(b"one")) == first

  with pytest.raises(BudgetReached, match="image limit"):
    output.asset("", ".png", lambda stream: stream.write(b"two"))

  assert set(output.files) == {first}
  assert output.image_paths == {first}


def test_asset_does_not_overwrite_untracked_destination(output):
  raw = b"image"
  target = output.root / "images" / (digest(raw) + ".png")
  write_new(target, b"untracked")

  with pytest.raises(StorageFailure, match="untracked"):
    output.asset("", ".png", lambda stream: stream.write(raw))

  assert target.read_bytes() == b"untracked"
  assert output.files == {}
  assert output.claimed == {}


def test_rollback_removes_only_newly_claimed_outputs(output):
  first = output.asset("", ".png", lambda stream: stream.write(b"one"))
  paths = set(output.claimed)
  images = set(output.image_paths)
  second = output.asset("", ".png", lambda stream: stream.write(b"two"))

  with output.open("temporary.txt") as stream:
    stream.write(b"temporary")

  output.rollback(paths, images)

  assert set(output.files) == {first}
  assert set(output.claimed) == {first}
  assert output.image_paths == {first}
  assert not (output.root / second).exists()
  assert not (output.root / "temporary.txt").exists()


def test_cleanup_keeps_selected_files_and_removes_empty_directories(output):
  with output.open("keep/value.txt") as stream:
    stream.write(b"keep")
  with output.open("discard/nested/value.txt") as stream:
    stream.write(b"discard")

  keep = output.root / "keep/value.txt"
  output.cleanup({keep})

  assert keep.read_bytes() == b"keep"
  assert not (output.root / "discard").exists()
  assert output.root.is_dir()


def test_cleanup_rejects_symlinks_instead_of_following_them(output):
  write_new(output.root / "source.txt", b"keep")
  (output.root / "link").symlink_to(output.root / "source.txt")

  with pytest.raises(ProtectionFailure):
    output.cleanup({output.root / "source.txt"})

  assert (output.root / "source.txt").read_bytes() == b"keep"


def test_markdown_separates_source_content_from_generated_appendix(output):
  markdown = Markdown(output)
  markdown.add("## Documents", "", "Fetched: source text.  ", "")
  markdown.add("## Extracted images", "", "generated link", appendix=True)

  assert markdown.content() == (
    b"## Documents\n\nFetched: source text.  \n"
  )
  assert markdown.bytes() == (
    b"## Documents\n\nFetched: source text.  \n\n"
    b"## Extracted images\n\ngenerated link\n"
  )
  assert len(markdown.bytes()) <= markdown.size
  assert output.text_bytes == markdown.size


def test_appendix_only_markdown_has_no_exportable_body(output):
  markdown = Markdown(output)
  markdown.add("## Images", "generated link", appendix=True)

  assert markdown.content() == b""
  assert markdown.bytes() == b"## Images\ngenerated link\n"


def test_markdown_can_add_source_after_an_appendix_without_mixing_them(output):
  markdown = Markdown(output)
  markdown.add("Gallery", appendix=True)
  markdown.add("Source")

  assert markdown.content() == b"Source\n"
  assert markdown.bytes() == b"Source\n\nGallery\n"
  assert len(markdown.bytes()) <= markdown.size


def test_markdown_and_assets_share_the_remaining_budget(private_dir, monkeypatch):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 8)
  output = Output(private_dir)
  markdown = Markdown(output)
  markdown.add("text")

  with pytest.raises(BudgetReached):
    output.asset("", ".png", lambda stream: stream.write(b"1234"))

  assert markdown.content() == b"text\n"
  assert output.files == {}


def test_failed_markdown_addition_is_atomic(private_dir, monkeypatch):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 5)
  output = Output(private_dir)
  markdown = Markdown(output)
  markdown.add("one")
  before = markdown.bytes()

  with pytest.raises(BudgetReached):
    markdown.add("too large")

  assert markdown.bytes() == before
  assert output.text_bytes == 4


def test_markdown_reservation_is_replaced_by_stored_text(output):
  markdown = Markdown(output)
  markdown.add("Source", "")
  markdown.add("Gallery", appendix=True)
  raw = markdown.bytes()

  output.write_text("text.md", raw)

  assert output.text_bytes == 0
  assert output.used == len(raw)
  assert output.files["text.md"].path.read_bytes() == raw


def test_markdown_text_limit_is_enforced_before_mutating_parts(
  output, monkeypatch
):
  monkeypatch.setattr(content, "TEXT_LIMIT", 8192 + 4)
  markdown = Markdown(output)

  with pytest.raises(LimitReached, match="Markdown"):
    markdown.add("1234")

  assert markdown.parts == []
  assert markdown.size == 0


@pytest.mark.parametrize(
  ("name", "raw", "expected"),
  [
    ("document.bin", b"%PDF-1.7\n", ".pdf"),
    ("image.bin", b"\x89PNG\r\n\x1a\n", ".png"),
    ("image.bin", b"\xff\xd8\xff", ".jpg"),
    ("image.bin", b"GIF89a", ".gif"),
    ("image.bin", b"RIFF1234WEBP", ".webp"),
    ("image.bin", b"II*\x00", ".tif"),
    ("image.bin", b"MM\x00*", ".tif"),
    ("image.bin", b"BMexample", ".bmp"),
    ("old.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "unsupported"),
    ("lesson.HTM", b"<p>Lesson</p>", ".html"),
    ("source.py", b"print('hello')\n", ".py"),
    ("README.md", b"# Readme\n", ".md"),
    ("data.bin", b"binary", "unsupported"),
  ],
)
def test_local_format_detection(private_dir, name, raw, expected):
  path = private_dir / name
  write_new(path, raw)

  assert local_kind(path) == expected


def test_original_name_recovers_extensionless_source_classification(private_dir):
  path = private_dir / original_name(".gitignore")
  write_new(path, b"cache/\n")

  assert local_kind(path, "project/.gitignore") == "text"


@pytest.mark.parametrize(
  ("marker", "kind"),
  [
    ("word/document.xml", ".docx"),
    ("ppt/presentation.xml", ".pptx"),
    ("xl/workbook.xml", ".xlsx"),
  ],
)
def test_office_detection_uses_package_markers(private_dir, marker, kind):
  path = private_dir / "document.bin"
  write_new(path, zip_bytes([
    ("[Content_Types].xml", b"<Types/>"),
    (marker, b"<document/>"),
  ]))

  assert local_kind(path) == kind


def test_regular_zip_is_not_misclassified_as_office(private_dir):
  path = private_dir / "bundle.zip"
  write_new(path, zip_bytes([("word/document.xml", b"ordinary member")]))

  assert local_kind(path) == "zip"


def test_zip_members_preserve_source_names_but_use_safe_local_paths(private_dir):
  entries = [
    ("project/src/main.py", b"print(1)\n"),
    ("../../outside.txt", b"relative traversal"),
    ("/absolute.txt", b"absolute"),
    (r"C:\project\source.py", b"print(2)\n"),
    (".gitignore", b"cache/\n"),
    ("text.md", b"# Source, not a generated product\n"),
  ]
  walker, output = walk(
    private_dir / "bundle", "bundle.zip", zip_bytes(entries)
  )

  assert [node["name"] for node in members(walker)] == [
    name for name, _ in entries
  ]
  assert walker.nodes[""]["problems"] == []

  for name, raw in entries:
    assert member_bytes(walker, output, name) == raw

  for folder, node in walker.nodes.items():
    if not folder:
      continue
    assert folder.startswith("contents/")
    assert ".." not in folder.split("/")
    assert "/" not in node["original"]
    assert "\\" not in node["original"]
    checked_file(output.resolve(folder + "/" + node["original"]).path.lstat())

  assert not (private_dir / "outside.txt").exists()
  assert next(node for node in members(walker) if node["name"] == "text.md")[
    "original"
  ] != "text.md"
  assert next(node for node in members(walker) if node["name"] == ".gitignore")[
    "kind"
  ] == "text"


def test_duplicate_zip_names_are_extracted_as_distinct_occurrences(private_dir):
  with pytest.warns(UserWarning, match="Duplicate name"):
    raw = zip_bytes([
      ("same.txt", b"first"),
      ("same.txt", b"second"),
      ("SAME.txt", b"third"),
    ])

  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)
  folders = walker.nodes[""]["children"]
  values = [
    output.resolve(folder + "/" + walker.nodes[folder]["original"]).path.read_bytes()
    for folder in folders
  ]

  assert values == [b"first", b"second", b"third"]
  assert len({folder.casefold() for folder in folders}) == 3


def test_empty_zip_is_a_valid_empty_container(private_dir):
  walker, output = walk(
    private_dir / "bundle", "empty.zip", zip_bytes([])
  )

  assert walker.nodes[""]["kind"] == "zip"
  assert walker.nodes[""]["children"] == []
  assert walker.nodes[""]["problems"] == []
  assert output.files == {}


def test_zip_directories_and_special_entries_are_not_materialized(private_dir):
  directory = zipfile.ZipInfo("empty/")
  symlink = zipfile.ZipInfo("link.txt")
  symlink.create_system = 3
  symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
  fifo = zipfile.ZipInfo("pipe")
  fifo.create_system = 3
  fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
  raw = zip_bytes([
    (directory, b""),
    (symlink, b"/outside"),
    (fifo, b""),
    ("normal.txt", b"normal"),
  ])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  assert [node["name"] for node in members(walker)] == ["normal.txt"]
  assert len(walker.nodes[""]["problems"]) == 2
  assert walker.nodes[""]["retry"] is False
  assert walker.budget.entries == 4
  assert member_bytes(walker, output, "normal.txt") == b"normal"


def test_encrypted_zip_member_is_skipped_without_password_attempts(private_dir):
  raw = bytearray(zip_bytes([("secret.txt", b"secret")]))
  central = raw.index(b"PK\x01\x02")
  raw[central + 8:central + 10] = (1).to_bytes(2, "little")
  walker, output = walk(
    private_dir / "bundle", "encrypted.zip", bytes(raw)
  )

  assert walker.nodes[""]["children"] == []
  assert any("encrypted member" in text for text in walker.nodes[""]["problems"])
  assert walker.nodes[""]["retry"] is False
  assert output.files == {}


def test_nested_zip_is_walked_recursively(private_dir):
  nested = zip_bytes([("src/main.py", b"print(1)\n")])
  raw = zip_bytes([("nested.zip", nested), ("README.txt", b"Readme")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  child = walker.nodes[""]["children"][0]
  assert walker.nodes[child]["kind"] == "zip"
  assert len(walker.nodes[child]["children"]) == 1
  assert member_bytes(walker, output, "src/main.py") == b"print(1)\n"
  assert walker.budget.expanded == len(nested) + len(b"print(1)\n") + len(b"Readme")


def test_depth_limit_does_not_hide_other_top_level_members(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "DEPTH_LIMIT", 1)
  nested = zip_bytes([("deep.txt", b"too deep")])
  raw = zip_bytes([("nested.zip", nested), ("tail.txt", b"keep")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  nested_folder = walker.nodes[""]["children"][0]
  nested_node = walker.nodes[nested_folder]
  assert nested_node["children"] == []
  assert any("depth limit" in text for text in nested_node["problems"])
  assert nested_node["retry"] is False
  assert member_bytes(walker, output, "tail.txt") == b"keep"
  assert walker.budget.stopped is False


def test_entry_limit_is_shared_by_nested_containers(private_dir, monkeypatch):
  monkeypatch.setattr(content, "ENTRY_LIMIT", 2)
  nested = zip_bytes([("one.txt", b"one"), ("two.txt", b"two")])
  raw = zip_bytes([("nested.zip", nested), ("tail.txt", b"tail")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  assert walker.budget.entries == 2
  assert walker.budget.stopped
  assert member_bytes(walker, output, "one.txt") == b"one"
  assert not any(node["name"] == "two.txt" for node in walker.nodes.values())
  assert not any(node["name"] == "tail.txt" for node in walker.nodes.values())


def test_fixed_member_size_limit_skips_member_and_continues(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "FILE_LIMIT", 4)
  raw = zip_bytes([("large.txt", b"12345"), ("small.txt", b"123")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  assert [node["name"] for node in members(walker)] == ["small.txt"]
  assert member_bytes(walker, output, "small.txt") == b"123"
  assert walker.nodes[""]["retry"] is False
  assert any("member size limit" in text for text in walker.nodes[""]["problems"])


def test_remaining_output_budget_marks_discovery_retryable(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "CONTENT_LIMIT", 4)
  raw = zip_bytes([("large.txt", b"12345"), ("tail.txt", b"x")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  assert walker.nodes[""]["retry"] is True
  assert walker.budget.stopped
  assert walker.nodes[""]["children"] == []
  assert output.files == {}


def test_expansion_limit_is_nonretryable_for_unchanged_settings(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "ASSET_LIMIT", 4)
  raw = zip_bytes([("large.txt", b"12345")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)

  assert walker.nodes[""]["retry"] is False
  assert walker.budget.stopped
  assert walker.nodes[""]["children"] == []
  assert output.files == {}
  assert any("expansion" in text for text in walker.nodes[""]["problems"])


@pytest.mark.parametrize("raw", [b"PK\x03\x04broken", b"not a ZIP"])
def test_invalid_zip_preserves_root_and_records_nonretryable_failure(
  private_dir, raw
):
  walker, output = walk(private_dir / "bundle", "broken.zip", raw)

  assert walker.nodes[""]["kind"] == "zip"
  assert walker.nodes[""]["retry"] is False
  assert any(
    "ZIP integrity check failed" in problem
    for problem in walker.nodes[""]["problems"]
  )
  assert (output.root / "broken.zip").read_bytes() == raw
  assert output.files == {}


def test_crc_failure_does_not_erase_healthy_siblings_or_original(private_dir):
  raw = damaged_zip([
    ("first.txt", b"First source\n"),
    ("bad.txt", b"Damaged source\n"),
    ("last.py", b"print('last source')\n"),
  ], "bad.txt")
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)
  root = walker.nodes[""]

  assert [node["name"] for node in members(walker)] == [
    "first.txt", "last.py",
  ]
  assert root["retry"] is False
  assert not walker.budget.stopped
  assert any(
    "bad.txt" in problem and "Bad CRC-32" in problem
    for problem in root["problems"]
  )
  assert member_bytes(walker, output, "first.txt") == b"First source\n"
  assert member_bytes(walker, output, "last.py") == b"print('last source')\n"
  assert not list(output.root.glob("contents/*/bad.txt"))
  assert (output.root / "bundle.zip").read_bytes() == raw


def test_nested_corrupt_container_does_not_stop_outer_members(private_dir):
  nested = b"PK\x03\x04broken"
  raw = zip_bytes([("broken.zip", nested), ("tail.txt", b"Keep tail\n")])
  walker, output = walk(private_dir / "bundle", "bundle.zip", raw)
  child = walker.nodes[""]["children"][0]

  assert walker.nodes[child]["problems"]
  assert walker.nodes[child]["retry"] is False
  assert walker.nodes[child]["children"] == []
  assert walker.nodes[""]["problems"] == []
  assert member_bytes(walker, output, "broken.zip") == nested
  assert member_bytes(walker, output, "tail.txt") == b"Keep tail\n"


def test_office_walk_extracts_embeddings_not_xml_or_media(private_dir):
  raw = zip_bytes([
    ("[Content_Types].xml", b"<Types/>"),
    ("word/document.xml", b"<document/>"),
    ("word/media/image.png", b"\x89PNG\r\n\x1a\nimage"),
    ("word/embeddings/package.zip", zip_bytes([("code.py", b"print(1)\n")])),
    ("word/embeddings/legacy.bin", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
  ])
  walker, output = walk(private_dir / "bundle", "document.docx", raw)

  assert walker.nodes[""]["kind"] == ".docx"
  assert [node["name"] for node in members(walker)] == [
    "word/embeddings/package.zip",
    "word/embeddings/legacy.bin",
  ]
  assert member_bytes(walker, output, "code.py") == b"print(1)\n"
  assert not any(node["name"].endswith("image.png") for node in walker.nodes.values())


def test_office_embedding_crc_failure_preserves_other_embedded_files(private_dir):
  raw = damaged_zip([
    ("[Content_Types].xml", b"<Types/>"),
    ("word/document.xml", b"<document/>"),
    ("word/embeddings/bad.txt", b"Corrupted embedded source\n"),
    ("word/embeddings/good.py", b"print('embedded source')\n"),
  ], "word/embeddings/bad.txt")
  walker, output = walk(private_dir / "bundle", "document.docx", raw)

  assert walker.nodes[""]["kind"] == ".docx"
  assert walker.nodes[""]["retry"] is False
  assert [node["name"] for node in members(walker)] == [
    "word/embeddings/good.py",
  ]
  assert member_bytes(walker, output, "word/embeddings/good.py") == (
    b"print('embedded source')\n"
  )
  assert any(
    "word/embeddings/bad.txt" in problem and "Bad CRC-32" in problem
    for problem in walker.nodes[""]["problems"]
  )


@pytest.mark.parametrize("mode", ["tar", "gzip", "bz2", "xz"])
def test_tar_variants_extract_regular_files(private_dir, mode):
  raw = tar_bytes([("project/source.py", b"print(1)\n"), ("empty.txt", b"")])
  encode = {
    "tar": lambda value: value,
    "gzip": gzip.compress,
    "bz2": bz2.compress,
    "xz": lzma.compress,
  }[mode]
  names = {
    "tar": "bundle.tar",
    "gzip": "bundle.tar.gz",
    "bz2": "bundle.tar.bz2",
    "xz": "bundle.tar.xz",
  }
  walker, output = walk(
    private_dir / "bundle", names[mode], encode(raw)
  )

  assert walker.nodes[""]["problems"] == []
  assert member_bytes(walker, output, "project/source.py") == b"print(1)\n"
  assert member_bytes(walker, output, "empty.txt") == b""
  assert walker.budget.expanded == len(raw)


def test_tar_links_and_special_entries_are_skipped(private_dir):
  stream = io.BytesIO()

  with tarfile.open(fileobj=stream, mode="w") as archive:
    for name, kind in (
      ("directory", tarfile.DIRTYPE),
      ("symbolic", tarfile.SYMTYPE),
      ("hard", tarfile.LNKTYPE),
      ("pipe", tarfile.FIFOTYPE),
    ):
      member = tarfile.TarInfo(name)
      member.type = kind
      member.linkname = "/outside"
      archive.addfile(member)

    member = tarfile.TarInfo("../source.txt")
    member.size = 4
    archive.addfile(member, io.BytesIO(b"safe"))

  walker, output = walk(
    private_dir / "bundle", "bundle.tar", stream.getvalue()
  )

  assert [node["name"] for node in members(walker)] == ["../source.txt"]
  assert len(walker.nodes[""]["problems"]) == 3
  assert member_bytes(walker, output, "../source.txt") == b"safe"
  assert not (private_dir / "source.txt").exists()


def test_tar_headers_and_padding_count_toward_expansion_budget(
  private_dir, monkeypatch
):
  monkeypatch.setattr(content, "ASSET_LIMIT", 1024)
  walker, output = walk(
    private_dir / "bundle",
    "bundle.tar",
    tar_bytes([("small.txt", b"x")]),
  )

  assert walker.budget.stopped
  assert walker.nodes[""]["problems"]
  assert walker.nodes[""]["retry"] is False


def test_invalid_tar_structure_is_nonretryable(private_dir):
  raw = b"invalid tar header"
  walker, output = walk(private_dir / "bundle", "broken.tar", raw)

  assert walker.nodes[""]["retry"] is False
  assert any(
    "TAR structure" in problem for problem in walker.nodes[""]["problems"]
  )
  assert (output.root / "broken.tar").read_bytes() == raw
  assert output.files == {}


def test_truncated_gzip_trailer_is_a_known_input_failure(private_dir):
  raw = gzip.compress(tar_bytes([("source.txt", b"content")]))[:-4]
  walker, output = walk(private_dir / "bundle", "bundle.tar.gz", raw)

  assert walker.nodes[""]["problems"]
  assert walker.nodes[""]["retry"] is False
  assert all(
    "interrupted" not in problem for problem in walker.nodes[""]["problems"]
  )
  assert (output.root / "bundle.tar.gz").read_bytes() == raw


def test_bad_gzip_crc_is_nonretryable_without_changing_original(private_dir):
  raw = bytearray(gzip.compress(tar_bytes([("source.txt", b"content")])))
  raw[-8] ^= 1
  raw = bytes(raw)
  walker, output = walk(private_dir / "bundle", "bundle.tar.gz", raw)

  assert walker.nodes[""]["retry"] is False
  assert any(
    "gzip integrity check failed" in problem
    for problem in walker.nodes[""]["problems"]
  )
  assert (output.root / "bundle.tar.gz").read_bytes() == raw


def test_expansion_reader_honors_zero_length_and_global_budget(monkeypatch):
  monkeypatch.setattr(content, "ASSET_LIMIT", 4)
  budget = WalkBudget()
  reader = ExpansionReader(io.BytesIO(b"12345"), budget)

  assert reader.read(0) == b""
  assert reader.read(2) == b"12"
  assert budget.expanded == 2
  assert reader.read(2) == b"34"

  with pytest.raises(LimitReached, match="expansion"):
    reader.read(1)

  assert budget.stopped


@pytest.mark.parametrize("kind", ["tar-zstd", "tar-lz4"])
def test_frame_reader_reassembles_concatenated_frames(kind):
  if kind == "tar-zstd":
    module = pytest.importorskip("zstandard")
    compress = module.ZstdCompressor().compress
  else:
    module = pytest.importorskip("lz4.frame")
    compress = module.compress

  raw = b"first frame\n" * 100 + b"second frame\n" * 100
  split = len(raw) // 2
  source = io.BytesIO(compress(raw[:split]) + compress(raw[split:]))
  budget = WalkBudget()
  reader = FrameReader(source, kind, budget)

  assert reader.read(0) == b""
  assert read_all(reader, 31) == raw
  assert reader.read(10) == b""
  assert budget.expanded == len(raw)


@pytest.mark.parametrize(
  ("kind", "filename"),
  [("tar-zstd", "bundle.tar.zst"), ("tar-lz4", "bundle.tar.lz4")],
)
def test_compressed_tar_frames_are_extracted_and_drained(
  private_dir, kind, filename
):
  if kind == "tar-zstd":
    module = pytest.importorskip("zstandard")
    compress = module.ZstdCompressor().compress
  else:
    module = pytest.importorskip("lz4.frame")
    compress = module.compress

  raw = tar_bytes([("source.txt", b"content")])
  split = len(raw) // 2
  packed = compress(raw[:split]) + compress(raw[split:])
  walker, output = walk(private_dir / "bundle", filename, packed)

  assert walker.nodes[""]["kind"] == kind
  assert walker.nodes[""]["problems"] == []
  assert member_bytes(walker, output, "source.txt") == b"content"
  assert walker.budget.expanded == len(raw)


@pytest.mark.parametrize("kind", ["tar-zstd", "tar-lz4"])
def test_truncated_frames_have_a_specific_input_failure(kind):
  if kind == "tar-zstd":
    module = pytest.importorskip("zstandard")
    compress = module.ZstdCompressor().compress
  else:
    module = pytest.importorskip("lz4.frame")
    compress = module.compress

  reader = FrameReader(
    io.BytesIO(compress(b"content" * 100)[:-1]), kind, WalkBudget()
  )

  with pytest.raises(Failure, match="truncated compressed TAR frame"):
    read_all(reader)


def test_uncompressed_tar_stream_is_bounded_and_closed(private_dir):
  path = private_dir / "source.tar"
  raw = tar_bytes([("source.txt", b"content")])
  write_new(path, raw)
  budget = WalkBudget()

  with tar_stream(path, "tar", budget) as stream:
    assert read_all(stream) == raw

  assert budget.expanded == len(raw)


@pytest.mark.parametrize("actual", [b"short", b"too much data"])
def test_member_size_mismatch_removes_partial_output_without_retry(
  private_dir, actual
):
  root = private_dir / "bundle"
  root.mkdir(mode=0o700)
  write_new(root / "root.txt", b"root")
  output = Output(root)
  walker = ContentWalker(output)
  walker.node("", "root.txt", "root.txt", 4, digest(b"root"))

  walker.save_child(
    "", "member.txt", 8, lambda: io.BytesIO(actual), depth=0
  )

  assert walker.nodes[""]["children"] == []
  assert walker.nodes[""]["retry"] is False
  assert walker.nodes[""]["problems"] == ["member.txt: member size mismatch"]
  assert output.files == {}
  assert not list(root.glob("contents/*/member.txt"))


def test_oversized_member_is_rejected_before_opening_stream(
  private_dir, monkeypatch
):
  root = private_dir / "bundle"
  root.mkdir(mode=0o700)
  write_new(root / "root.txt", b"root")
  output = Output(root)
  walker = ContentWalker(output)
  walker.node("", "root.txt", "root.txt", 4, digest(b"root"))
  monkeypatch.setattr(content, "FILE_LIMIT", 4)

  def unexpected():
    raise AssertionError("oversized member must not be opened")

  walker.save_child("", "large.txt", 5, unexpected, depth=0)

  assert walker.nodes[""]["retry"] is False
  assert output.files == {}


def test_storage_failure_is_not_downgraded_to_a_member_problem(
  private_dir, monkeypatch
):
  root = private_dir / "bundle"
  root.mkdir(mode=0o700)
  write_new(root / "root.txt", b"root")
  output = Output(root)
  walker = ContentWalker(output)
  walker.node("", "root.txt", "root.txt", 4, digest(b"root"))

  @contextmanager
  def fail(*args, **kwargs):
    raise StorageFailure("disk full")
    yield

  monkeypatch.setattr(output, "open", fail)

  with pytest.raises(StorageFailure, match="disk full"):
    walker.save_child(
      "", "source.txt", 4, lambda: io.BytesIO(b"data"), depth=0
    )


def test_wrapped_file_failure_during_type_inspection_remains_fatal(
  private_dir, monkeypatch
):
  path = private_dir / "source.zip"
  write_new(path, zip_bytes([("source.txt", b"source")]))
  failure = StorageFailure("local device failed")

  def fail(stream, suffix):
    stream.raw.failure = failure
    raise zipfile.BadZipFile("decoder replaced the read failure")

  monkeypatch.setattr(content, "_zip_kind", fail)

  with pytest.raises(StorageFailure) as captured:
    local_kind(path)

  assert captured.value is failure


@pytest.mark.parametrize(
  ("error", "retry"),
  [
    (EOFError("decoder reached EOF"), False),
    (zipfile.BadZipFile("Bad CRC-32"), False),
    (Failure("decoder initialization failed"), True),
    (OSError("untyped decompression failure"), True),
    (RuntimeError("unexpected decoder failure"), True),
  ],
)
def test_member_failures_keep_narrow_retry_classification(output, error, retry):
  write_new(output.root / "root.txt", b"root")
  walker = ContentWalker(output)
  walker.node("", "root.txt", "root.txt", 4, digest(b"root"))

  class BrokenInput(io.BytesIO):
    def read(self, size=-1):
      raise error

  walker.save_child(
    "", "broken.txt", 4, lambda: BrokenInput(b"data"), depth=0
  )
  walker.save_child(
    "", "healthy.txt", 4, lambda: io.BytesIO(b"keep"), depth=0
  )

  assert walker.nodes[""]["retry"] is retry
  assert [node["name"] for node in members(walker)] == ["healthy.txt"]
  assert member_bytes(walker, output, "healthy.txt") == b"keep"
  assert all(
    "interrupted" not in problem for problem in walker.nodes[""]["problems"]
  )
  assert not list(output.root.glob("contents/*/broken.txt"))


@pytest.mark.parametrize("boundary", ["inspection", "member", "container"])
def test_explicit_cancellation_is_not_recorded_as_damaged_input(
  output, monkeypatch, boundary
):
  write_new(output.root / "root.zip", zip_bytes([]))
  walker = ContentWalker(output)
  raw = (output.root / "root.zip").read_bytes()

  def cancel(*args, **kwargs):
    raise Cancelled()

  if boundary == "inspection":
    monkeypatch.setattr(content, "local_kind", cancel)
    with pytest.raises(Cancelled):
      walker.node("", "root.zip", "root.zip", len(raw), digest(raw))
    return

  walker.node("", "root.zip", "root.zip", len(raw), digest(raw))

  if boundary == "container":
    monkeypatch.setattr(walker, "walk_zip", cancel)
    with pytest.raises(Cancelled):
      walker.walk("")
  else:
    with pytest.raises(Cancelled):
      walker.save_child("", "source.txt", 4, cancel, depth=0)

  assert walker.nodes[""]["problems"] == []


@pytest.fixture
def pdfium(monkeypatch):
  state = SimpleNamespace(attachments=[], reads=[], closed=0)
  module = ModuleType("pypdfium2")

  class Attachment:
    def __init__(self, name, raw, size=None, error=None):
      self.name = name
      self.raw = raw
      self.size = len(raw) if size is None else size
      self.error = error

    def get_name(self):
      return self.name

    def get_data(self):
      state.reads.append(self.name)
      if self.error is not None:
        raise self.error
      return self.raw

  class Document:
    def __init__(self, path):
      self.path = path

    def __enter__(self):
      return self

    def __exit__(self, *args):
      state.closed += 1

    def count_attachments(self):
      return len(state.attachments)

    def get_attachment(self, index):
      return state.attachments[index]

  def get_file(attachment, buffer, length, size):
    assert buffer is None
    assert length == 0
    size.value = attachment.size
    return True

  module.PdfDocument = Document
  module.raw = SimpleNamespace(FPDFAttachment_GetFile=get_file)
  monkeypatch.setitem(sys.modules, "pypdfium2", module)
  state.Attachment = Attachment
  return state


def test_pdf_embedded_files_are_extracted_without_text_conversion(
  private_dir, pdfium
):
  pdfium.attachments = [
    pdfium.Attachment("src/main.py", b"print(1)\n"),
    pdfium.Attachment("empty.txt", b""),
  ]
  walker, output = walk(
    private_dir / "bundle", "document.pdf", b"%PDF-synthetic"
  )

  assert walker.nodes[""]["kind"] == ".pdf"
  assert walker.nodes[""]["products"] == {}
  assert member_bytes(walker, output, "src/main.py") == b"print(1)\n"
  assert member_bytes(walker, output, "empty.txt") == b""
  assert pdfium.reads == ["src/main.py"]
  assert pdfium.closed == 1


def test_pdf_attachment_size_is_checked_before_allocating_its_data(
  private_dir, pdfium, monkeypatch
):
  monkeypatch.setattr(content, "FILE_LIMIT", 4)
  pdfium.attachments = [
    pdfium.Attachment("large.bin", b"", size=5),
    pdfium.Attachment("small.txt", b"keep"),
  ]
  walker, output = walk(
    private_dir / "bundle", "document.pdf", b"%PDF-synthetic"
  )

  assert pdfium.reads == ["small.txt"]
  assert member_bytes(walker, output, "small.txt") == b"keep"
  assert walker.nodes[""]["retry"] is False
  assert any("member size limit" in text for text in walker.nodes[""]["problems"])


def test_pdf_embedded_archives_use_the_same_recursive_budget(private_dir, pdfium):
  nested = zip_bytes([("source.py", b"print(1)\n")])
  pdfium.attachments = [pdfium.Attachment("project.zip", nested)]
  walker, output = walk(
    private_dir / "bundle", "document.pdf", b"%PDF-synthetic"
  )

  assert member_bytes(walker, output, "source.py") == b"print(1)\n"
  assert walker.budget.entries == 2
  assert walker.budget.expanded == len(nested) + len(b"print(1)\n")


def test_failed_pdf_attachment_does_not_discard_other_attachments(
  private_dir, pdfium
):
  pdfium.attachments = [
    pdfium.Attachment("bad.txt", b"x", error=Failure("decoder failed")),
    pdfium.Attachment("good.txt", b"keep"),
  ]
  walker, output = walk(
    private_dir / "bundle", "document.pdf", b"%PDF-synthetic"
  )

  assert walker.nodes[""]["retry"] is True
  assert member_bytes(walker, output, "good.txt") == b"keep"
  assert not any(node["name"] == "bad.txt" for node in members(walker))
  assert pdfium.closed == 1


def test_pdf_attachment_size_mismatch_does_not_retry_unchanged_input(
  private_dir, pdfium
):
  pdfium.attachments = [
    pdfium.Attachment("bad.txt", b"short", size=8),
    pdfium.Attachment("good.txt", b"keep"),
  ]
  walker, output = walk(
    private_dir / "bundle", "document.pdf", b"%PDF-synthetic"
  )

  assert walker.nodes[""]["retry"] is False
  assert member_bytes(walker, output, "good.txt") == b"keep"
  assert walker.nodes[""]["problems"] == ["bad.txt: member size mismatch"]
  assert not list(output.root.glob("contents/*/bad.txt"))


def test_pdf_attachment_cancellation_propagates_and_closes_document(
  private_dir, pdfium
):
  pdfium.attachments = [
    pdfium.Attachment("source.txt", b"data", error=Cancelled()),
  ]

  with pytest.raises(Cancelled):
    walk(private_dir / "bundle", "document.pdf", b"%PDF-synthetic")

  assert pdfium.closed == 1
  assert not list((private_dir / "bundle").glob("contents/*/source.txt"))
