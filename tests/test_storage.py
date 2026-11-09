import errno
import os
import stat

from pathlib import Path

import pytest

import kctl.storage as storage

from kctl.core import (
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  digest,
)
from kctl.storage import (
  Tree,
  check_job_source,
  checked_directory,
  checked_file,
  checked_local,
  file_digest,
  read_private,
  remove_work,
  signature,
  source_signature,
  staging_output,
  storage_io,
  sync_tree,
  verify_source,
  write_new,
)


@pytest.fixture
def tree(private_dir):
  value = Tree(private_dir)
  try:
    yield value
  finally:
    value.close()


def make_document(tree, raw=b"original"):
  folder = "documents/Course/Document"
  tree.write(folder + "/original.txt", raw)
  path = tree.path(folder + "/original.txt")
  return {
    "folder": folder,
    "original": "original.txt",
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": signature(path),
  }


def test_tree_creates_private_parents(private_dir):
  root = private_dir / "one" / "two"
  tree = Tree(root)

  try:
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700
    assert tree.root == root
  finally:
    tree.close()

  assert tree.fd == -1


def test_tree_refuses_missing_root_when_creation_disabled(private_dir):
  with pytest.raises(Failure, match="missing"):
    Tree(private_dir / "missing", create=False)


def test_tree_refuses_filesystem_root():
  with pytest.raises(Failure, match="filesystem root"):
    Tree(Path("/"))


def test_tree_refuses_public_existing_root(private_dir):
  private_dir.chmod(0o755)

  with pytest.raises(ProtectionFailure, match="directory"):
    Tree(private_dir)


def test_tree_refuses_writable_ancestor(private_dir):
  parent = private_dir / "shared"
  parent.mkdir(mode=0o770)
  parent.chmod(0o770)

  with pytest.raises(ProtectionFailure, match="ancestor"):
    Tree(parent / "archive")


def test_tree_does_not_follow_root_symlinks(private_dir):
  real = private_dir / "real"
  real.mkdir(mode=0o700)
  link = private_dir / "link"
  link.symlink_to(real, target_is_directory=True)

  with pytest.raises(OSError):
    Tree(link)


def test_write_read_and_managed_permissions(tree):
  tree.write("a/b/value.txt", b"value")

  assert tree.read("a/b/value.txt", 5) == b"value"
  assert tree.exists("a/b")
  assert tree.exists("a/b/value.txt")
  assert not tree.exists("missing/value.txt")

  checked_directory(tree.path("a").lstat())
  checked_directory(tree.path("a/b").lstat())
  checked_file(tree.path("a/b/value.txt").lstat())


@pytest.mark.parametrize("limit", [-1, True])
def test_invalid_read_limits_fail(tree, limit):
  tree.write("value.txt", b"value")

  with pytest.raises(Failure, match="read limit"):
    tree.read("value.txt", limit)


def test_bounded_reads_do_not_silently_truncate(tree):
  tree.write("value.txt", b"value")

  with pytest.raises(Failure, match="too large"):
    tree.read("value.txt", 4)

  with pytest.raises(Failure, match="too large"):
    read_private(tree.path("value.txt"), 4)


def test_empty_files_can_be_read_with_zero_limit(tree):
  tree.write("empty", b"")
  assert tree.read("empty", 0) == b""


def test_streaming_atomic_write_replaces_the_complete_file(tree):
  tree.write("course.txt", b"old")

  with tree.atomic("course.txt") as stream:
    stream.write(b"first\n")
    stream.write(b"second\n")
    assert tree.read("course.txt", 100) == b"old"

  assert tree.read("course.txt", 100) == b"first\nsecond\n"
  assert not list(tree.root.glob(".write-*"))


def test_interrupted_write_preserves_previous_file(tree):
  tree.write("course.txt", b"old")

  with pytest.raises(KeyboardInterrupt):
    with tree.atomic("course.txt") as stream:
      stream.write(b"unfinished")
      raise KeyboardInterrupt()

  assert tree.read("course.txt", 100) == b"old"
  assert not list(tree.root.glob(".write-*"))


def test_failed_new_write_leaves_no_destination(tree):
  with pytest.raises(Failure, match="limit"):
    with tree.atomic("course.txt") as stream:
      stream.write(b"unfinished")
      raise Failure("limit")

  assert not tree.exists("course.txt")
  assert not list(tree.root.glob(".write-*"))


def test_replace_failure_preserves_previous_file(tree, monkeypatch):
  tree.write("course.txt", b"old")

  def fail(*args, **kwargs):
    raise OSError(errno.ENOSPC, "full")

  monkeypatch.setattr(storage.os, "replace", fail)

  with pytest.raises(StorageFailure, match="I/O failed"):
    tree.write("course.txt", b"new")

  assert tree.read("course.txt", 100) == b"old"
  assert not list(tree.root.glob(".write-*"))


def test_directory_sync_failure_reports_that_replacement_happened(
  tree, monkeypatch
):
  tree.write("course.txt", b"old")
  real_fsync = storage.os.fsync

  def fail_directory(descriptor):
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
      raise OSError(errno.EIO, "sync failed")
    real_fsync(descriptor)

  monkeypatch.setattr(storage.os, "fsync", fail_directory)

  with pytest.raises(StorageFailure, match="file replaced"):
    tree.write("course.txt", b"new")

  assert tree.read("course.txt", 100) == b"new"
  assert not list(tree.root.glob(".write-*"))


@pytest.mark.parametrize("operation", ["read", "write", "remove", "exists"])
def test_symlink_file_is_not_followed(tree, operation):
  tree.write("original", b"safe")
  (tree.root / "link").symlink_to(tree.root / "original")

  with pytest.raises((OSError, ProtectionFailure)):
    if operation == "read":
      tree.read("link", 100)
    elif operation == "write":
      tree.write("link", b"changed")
    elif operation == "remove":
      tree.remove("link")
    else:
      tree.exists("link")

  assert tree.read("original", 100) == b"safe"


def test_symlink_directory_is_not_followed(tree):
  tree.mkdir("real")
  (tree.root / "link").symlink_to(tree.root / "real", target_is_directory=True)

  with pytest.raises(OSError):
    tree.write("link/value", b"value")

  assert not tree.exists("real/value")


def test_hard_linked_files_are_rejected(tree):
  tree.write("original", b"safe")
  os.link(tree.root / "original", tree.root / "copy")

  with pytest.raises(ProtectionFailure, match="one link"):
    tree.read("original", 100)

  with pytest.raises(ProtectionFailure):
    tree.write("copy", b"changed")


def test_world_readable_managed_file_is_rejected(tree):
  tree.write("value", b"safe")
  (tree.root / "value").chmod(0o644)

  with pytest.raises(ProtectionFailure, match="0600"):
    tree.read("value", 100)


def test_fifo_read_does_not_wait_for_a_writer(tree):
  os.mkfifo(tree.root / "pipe", 0o600)

  with pytest.raises(ProtectionFailure):
    tree.read("pipe", 100)


def test_locks_exclude_independent_openers(tree):
  tree.lock()
  other = Tree(tree.root, create=False)

  try:
    with pytest.raises(StorageFailure, match="locked"):
      other.lock()

    tree.close()
    other.lock()
    assert other.lock_fd is not None
  finally:
    other.close()


def test_lock_cannot_be_acquired_twice_on_same_tree(tree):
  tree.lock()

  with pytest.raises(Failure, match="already locked"):
    tree.lock()


def test_atomic_cleanup_only_removes_recognized_temporaries(tree):
  recognized = ".write-" + "a" * 32
  tree.write(recognized, b"abandoned")
  tree.write(".write-not-a-token", b"retain")
  tree.write("source.txt", b"retain")

  tree.clean_atomic()

  assert not tree.exists(recognized)
  assert tree.exists(".write-not-a-token")
  assert tree.exists("source.txt")


def test_move_refuses_existing_destination(tree):
  tree.write("source", b"source")
  tree.write("destination", b"destination")

  with pytest.raises(StorageFailure, match="destination exists"):
    tree.move("source", "destination")

  assert tree.read("source", 100) == b"source"
  assert tree.read("destination", 100) == b"destination"


def test_move_supports_bundles(tree):
  tree.write("old/original.txt", b"source")
  tree.move("old", "documents/Course/Document")

  assert not tree.exists("old")
  assert tree.read("documents/Course/Document/original.txt", 100) == b"source"


def test_remove_is_idempotent(tree):
  tree.write("value", b"value")
  tree.remove("value")
  tree.remove("value")
  tree.remove("missing/value")

  assert not tree.exists("value")


def test_staging_creates_private_intermediate_directories(private_dir):
  path = private_dir / "a" / "b" / "value"
  previous = os.umask(0o022)

  try:
    write_new(path, b"value")
  finally:
    os.umask(previous)

  checked_directory((private_dir / "a").lstat())
  checked_directory((private_dir / "a" / "b").lstat())
  checked_file(path.lstat())
  assert read_private(path, 100) == b"value"


def test_staging_refuses_to_replace_an_existing_file(private_file):
  path = private_file("value", b"old")

  with pytest.raises(StorageFailure):
    write_new(path, b"new")

  assert read_private(path, 100) == b"old"


def test_staging_leaves_failed_partial_output_for_workspace_cleanup(private_dir):
  path = private_dir / "partial"

  with pytest.raises(Failure, match="decoder"):
    with staging_output(path) as stream:
      stream.write(b"partial")
      raise Failure("decoder")

  checked_file(path.lstat())
  assert path.read_bytes() == b"partial"


def test_sync_tree_checks_all_entries(tree):
  tree.write("bundle/a/b.txt", b"content")
  sync_tree(tree.path("bundle"))

  (tree.root / "bundle" / "a" / "link").symlink_to(
    tree.root / "bundle" / "a" / "b.txt"
  )

  with pytest.raises((OSError, ProtectionFailure)):
    sync_tree(tree.path("bundle"))


def test_workspace_cleanup_is_scoped_and_idempotent(tree):
  work = ".ctl/work/" + "a" * 32
  tree.write(work + "/new/source.txt", b"source")
  tree.write("documents/keep.txt", b"keep")

  remove_work(tree.root, work)
  remove_work(tree.root, work)

  assert not tree.exists(work)
  assert tree.read("documents/keep.txt", 100) == b"keep"


@pytest.mark.parametrize(
  "relative",
  ["documents", ".ctl/work", ".ctl/work/not-a-token", ".ctl/work/../documents"],
)
def test_workspace_cleanup_refuses_other_paths(tree, relative):
  with pytest.raises(Failure, match="work directory"):
    remove_work(tree.root, relative)


def test_workspace_cleanup_refuses_symlink_entries(tree):
  work = ".ctl/work/" + "b" * 32
  tree.mkdir(work)
  tree.write("keep", b"safe")
  (tree.root / work / "link").symlink_to(tree.root / "keep")

  with pytest.raises(ProtectionFailure):
    remove_work(tree.root, work)

  assert tree.read("keep", 100) == b"safe"


def test_source_verification_accepts_unchanged_content_after_metadata_change(tree):
  doc = make_document(tree)
  original = tree.path(doc["folder"] + "/" + doc["original"])
  os.utime(original, ns=(1_000_000_000, 1_000_000_000))

  verified = verify_source(tree.root, doc)

  assert verified != doc["signature"]
  assert verified == signature(original)
  assert file_digest(original) == doc["sha256"]


@pytest.mark.parametrize("replacement", [b"different length", b"modified"])
def test_changed_originals_are_not_reused(tree, replacement):
  doc = make_document(tree)
  tree.write(doc["folder"] + "/" + doc["original"], replacement)

  with pytest.raises(SourceProblem, match="changed"):
    verify_source(tree.root, doc)


def test_missing_original_is_a_source_problem(tree):
  doc = make_document(tree)
  tree.remove(doc["folder"] + "/" + doc["original"])

  with pytest.raises(SourceProblem, match="original missing"):
    source_signature(tree.root, doc)


def test_job_source_detects_changes_even_when_size_is_unchanged(tree):
  doc = make_document(tree)
  spec = {
    "root": str(tree.root),
    "document": doc,
    "signature": doc["signature"],
  }
  check_job_source(spec)
  tree.write(doc["folder"] + "/" + doc["original"], b"modified")

  with pytest.raises(SourceProblem, match="during processing"):
    check_job_source(spec)


def test_checked_local_verifies_intermediate_directories(tree):
  tree.write("a/b.txt", b"value")
  (tree.root / "a").chmod(0o755)

  with pytest.raises(ProtectionFailure):
    checked_local(tree.root, "a/b.txt")


def test_storage_io_sanitizes_os_error_details():
  with pytest.raises(StorageFailure) as captured:
    with storage_io():
      raise OSError(
        errno.EIO, "secret diagnostic", "/private/location/value.txt"
      )

  assert "value.txt" in captured.value.message
  assert "/private/location" not in captured.value.message
  assert "secret diagnostic" not in captured.value.message
