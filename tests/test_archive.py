import copy
import os
import sys

from pathlib import Path
from types import ModuleType

import pytest

import kctl
import kctl.archive as archive_module

from kctl.archive import (
  Archive,
  cached_result_valid,
  check_bundle,
  source_content,
)
from kctl.core import (
  MANIFEST_LIMIT,
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  WorkerResultFailure,
  digest,
  identity,
  json_bytes,
  parse_json,
)
from kctl.records import (
  attempt_record,
  new_manifest,
  outcome,
  validate_manifest,
)
from kctl.storage import Tree, signature, write_new


COURSE = "course/one"
FOLDER = "documents/First"
STAMP = "2026-11-04T00:00:00+00:00"


@pytest.fixture
def views(monkeypatch):
  module = ModuleType("kctl.views")
  calls = []

  def render(archive, key, incomplete):
    calls.append((key, set(incomplete)))

  module.render = render
  monkeypatch.setitem(sys.modules, "kctl.views", module)
  monkeypatch.setattr(kctl, "views", module, raising=False)
  return calls


@pytest.fixture
def archive(private_dir, views):
  value = Archive(private_dir / "archive", create=True)
  value.prepare()

  try:
    yield value
  finally:
    value.close()


def add_course(archive, key=COURSE, folder=FOLDER, include=True):
  if "index" not in archive.data["items"]:
    archive.item("index", {
      "title": "Archive",
      "path": "documents/index.md",
      "folder": "documents",
      "status": "archived",
      "children": [],
      "parent": None,
    }, None)

  archive.item(key, {
    "title": Path(folder).name,
    "path": folder + "/index.md",
    "folder": folder,
    "status": "archived",
    "parent": "index",
    "course_id": key.removeprefix("course/"),
    "include_submissions": include,
    "children": [],
  }, None)

  children = archive.data["items"]["index"]["children"]
  archive.item("index", {
    "children": list(dict.fromkeys([*children, key])),
  }, None)


def add_page(
  archive,
  key="page",
  folder=FOLDER,
  parent=COURSE,
  body="Source content.\n",
):
  if parent == COURSE and COURSE not in archive.data["items"]:
    add_course(archive)

  path = folder + "/" + key.rsplit("/", 1)[-1] + ".md"
  archive.item(key, {
    "title": key,
    "path": path,
    "folder": folder,
    "status": "archived",
    "parent": parent,
    "source": "https://ctl.kmu.ac.kr/Course.do?cmd=viewStudyHome",
    "documents": [],
    "related": [],
    "problems": [],
    "empty_listing": False,
  }, body)

  if parent is not None:
    item = archive.data["items"][parent]
    field = "children" if "children" in item else "related"
    archive.item(parent, {
      field: list(dict.fromkeys([*item.get(field, []), key])),
    }, None)

  return path


def stage_document(
  archive,
  owner="page",
  name="original.pdf",
  raw=b"%PDF-original",
  role="attachment",
  identifier="one",
):
  if owner not in archive.data["items"]:
    add_page(archive, owner)

  folder = archive.data["items"][owner]["folder"] + "/Document-" + identifier
  url = "https://ctl.kmu.ac.kr/fileDownServlet?id=" + identifier
  key = identity(owner, url)
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/new/" + name, raw)
  record = {
    "owner": owner,
    "url": url,
    "name": name,
    "folder": folder,
    "original": name,
    "role": role,
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": signature(archive.path(work + "/new/" + name)),
    "cache": "downloaded",
    "result": None,
    "attempts": {},
  }
  return key, record, work


def add_document(archive, *, attach=True, **kwargs):
  key, record, work = stage_document(archive, **kwargs)
  archive.document(key, record, work)

  if attach:
    owner = record["owner"]
    documents = archive.data["items"][owner].get("documents", [])
    archive.item(owner, {
      "documents": list(dict.fromkeys([*documents, key])),
    }, None)

  return key


def make_result(archive, doc, body=b"Extracted text.\n"):
  header = b"# Generated\n\n"
  appendix = b"\n## Extracted images\n"
  product = outcome(
    archive.settings["convert"],
    outputs={"text.md"},
    markdown="text.md",
    content_bytes=len(body),
    content_sha256=digest(body),
  )
  product["header_bytes"] = len(header)
  raw = header + body + appendix
  node = {
    "name": doc["name"],
    "kind": ".pdf",
    "original": doc["original"],
    "bytes": doc["bytes"],
    "sha256": doc["sha256"],
    "children": [],
    "problems": [],
    "retry": False,
    "visuals": None,
    "products": {"convert": product},
    "attempts": {"convert": attempt_record(product, False)},
  }
  result = {
    "source": doc["sha256"],
    "settings": copy.deepcopy(archive.settings["walk"]),
    "nodes": {"": node},
    "files": {"text.md": len(raw)},
    "finished": STAMP,
  }
  return result, {"text.md": raw}


def processing_job(archive, key, body=b"Extracted text.\n"):
  doc = copy.deepcopy(archive.data["documents"][key])
  result, files = make_result(archive, doc, body)
  work = archive.new_work()
  archive.mkdir(work + "/new")
  original = archive.read(doc["folder"] + "/" + doc["original"], 1024)
  archive.write(work + "/new/" + doc["original"], original)

  for relative, raw in files.items():
    archive.write(work + "/new/" + relative, raw)

  spec = {
    "key": key,
    "root": str(archive.root),
    "document": doc,
    "signature": copy.deepcopy(doc["signature"]),
    "settings": copy.deepcopy(archive.settings),
    "steps": ["convert"],
    "retry_steps": [],
    "force": False,
    "work": work,
  }
  message = {
    "result": result,
    "error": None,
    "fatal": None,
    "notes": [],
    "seconds": 0.25,
  }
  return spec, message


def stage_page(archive, key, body):
  old = archive.data["items"][key]
  record = copy.deepcopy(old)
  header = b"# Replacement\n\n"
  content = body.encode()
  record.update({
    "header_bytes": len(header),
    "content_bytes": len(content),
    "content_sha256": digest(content),
  })
  work = archive.new_work()
  archive.write(work + "/new", header + content)
  return record, work


def leave_receipt(archive, section, key, record, work):
  target = record["folder"] if section == "documents" else record["path"]
  receipt = {
    "section": section,
    "key": key,
    "record": copy.deepcopy(record),
    "old": copy.deepcopy(archive.data[section].get(key)),
    "work": work,
    "target": target,
    "had_old": archive.exists(target),
  }
  archive.checkpoint()
  archive.write(".ctl/pending.json", json_bytes(receipt))
  return receipt


def test_opening_fresh_archive_only_creates_internal_state(private_dir):
  root = private_dir / "archive"
  archive = Archive(root, create=True)

  try:
    assert set(path.name for path in root.iterdir()) == {".ctl"}
    assert archive.data == new_manifest()
    assert not archive.exists(".ctl/work")
    assert not archive.exists("documents")
    assert not archive.exists("extracted")
    validate_manifest(archive.data)
  finally:
    archive.close()


def test_nonempty_unmanaged_directory_is_not_claimed(private_dir):
  write_new(private_dir / "keep.txt", b"keep")

  with pytest.raises(Failure, match="empty archive"):
    Archive(private_dir, create=True)

  assert (private_dir / "keep.txt").read_bytes() == b"keep"


def test_old_archive_format_is_rejected_without_migration(private_dir):
  tree = Tree(private_dir)
  try:
    tree.mkdir(".ctl")
    data = new_manifest()
    data["application"] = "ctl"
    tree.write(".ctl/manifest.json", json_bytes(data))
  finally:
    tree.close()

  with pytest.raises(Failure, match="fresh archive"):
    Archive(private_dir, create=False)

  assert parse_json((private_dir / ".ctl/manifest.json").read_bytes()) == data


def test_archive_lock_is_exclusive(archive):
  with pytest.raises(StorageFailure, match="locked"):
    Archive(archive.root, create=False)


def test_existing_archive_is_opened_without_rendering(archive, views):
  add_page(archive)
  archive.flush(force=True)
  root = archive.root
  archive.close()
  views.clear()
  reopened = Archive(root, create=False)

  try:
    assert views == []
    assert reopened.data["items"]["page"]["content_bytes"] > 0
    assert not reopened.active_run
  finally:
    reopened.close()


def test_account_binding_is_stable(archive):
  archive.account("student-1")
  archive.account("student-1")

  with pytest.raises(Failure, match="another account"):
    archive.account("student-2")

  assert archive.data["account"] == "student-1"
  saved = parse_json(archive.read(".ctl/manifest.json", MANIFEST_LIMIT))
  assert saved["account"] == "student-1"


def test_export_preparation_does_not_render_or_replace_run(archive, views):
  add_page(archive)
  archive.begin("sync")
  archive.finish("partial", upstream={"status": "failed"})
  previous = copy.deepcopy(archive.data["run"])
  work = archive.new_work()
  archive.write(work + "/abandoned", b"temporary")
  views.clear()

  archive.prepare_export()

  assert archive.exists("extracted")
  assert not archive.exists(work)
  assert archive.data["run"] == previous
  assert views == []


def test_prepare_regenerates_views_explicitly(archive, views):
  add_page(archive)
  views.clear()

  archive.prepare()

  assert {key for key, _ in views} == {
    "index", COURSE, "page",
  }


def test_course_and_source_page_records_are_persisted(archive):
  path = add_page(archive, body="자료\nFetched: source text.\n")
  item = archive.data["items"]["page"]
  raw = archive.read(path, 4096)

  assert item["header_bytes"] > 0
  assert item["content_bytes"] == len("자료\nFetched: source text.\n".encode())
  assert source_content(raw, item) == "자료\nFetched: source text.\n".encode()
  assert archive.read_content(path, item) == "자료\nFetched: source text.\n".encode()
  validate_manifest(archive.data)


def test_generated_footer_is_not_source_content(archive):
  path = add_page(archive, body="## Documents\nActual source.\n")
  item = archive.data["items"]["page"]
  raw = archive.read(path, 4096)
  archive.write(path, raw + b"\n[Back](index.md)\nGenerated: yesterday\n")

  assert archive.read_content(path, item) == b"## Documents\nActual source.\n"


def test_image_only_page_can_have_an_empty_textual_body(archive):
  path = add_page(archive, body="")
  item = archive.data["items"]["page"]

  assert item["content_bytes"] == 1
  assert archive.read_content(path, item) == b"\n"
  validate_manifest(archive.data)


def test_failed_page_refresh_preserves_original_content_and_references(archive):
  path = add_page(archive, body="Previous content\n")
  key = add_document(archive)
  before = archive.read(path, 4096)
  span = {
    name: archive.data["items"]["page"][name]
    for name in ("header_bytes", "content_bytes", "content_sha256")
  }

  archive.item("page", {
    "status": "partial", "message": "retrieval incomplete",
  }, None)
  archive.item("page", {
    "status": "failed", "message": "layout changed",
  }, None)

  item = archive.data["items"]["page"]
  assert archive.read(path, 4096) == before
  assert item["documents"] == [key]
  assert all(item[name] == value for name, value in span.items())


def test_completed_page_refresh_replaces_its_attachment_references(archive):
  path = add_page(archive)
  key = add_document(archive)

  archive.item("page", {
    "documents": [],
    "status": "archived",
    "message": "",
  }, "Updated source\n")

  assert archive.data["items"]["page"]["documents"] == []
  assert key in archive.data["documents"]
  assert archive.read_content(path, archive.data["items"]["page"]) == (
    b"Updated source\n"
  )


def test_download_publication_does_not_attach_document_to_page(archive):
  key = add_document(archive, attach=False)

  assert archive.data["items"]["page"]["documents"] == []
  assert key in archive.data["documents"]
  assert archive.document_keys([COURSE]) == []


def test_completed_download_records_its_installed_signature(archive):
  key = add_document(archive)
  doc = archive.data["documents"][key]
  path = archive.path(doc["folder"] + "/" + doc["original"])

  assert doc["signature"] == signature(path)
  assert not archive.exists(".ctl/pending.json")
  assert list(archive.path(".ctl/work").iterdir()) == []


def test_reused_document_preserves_locally_generated_result(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  archive.publish(spec, message)
  previous = copy.deepcopy(archive.data["documents"][key]["result"])
  incoming = copy.deepcopy(archive.data["documents"][key])
  incoming.update(result=None, attempts={}, cache="revalidated")

  archive.document(key, incoming, work=None)

  assert archive.data["documents"][key]["result"] == previous


def test_reuse_requires_an_existing_identical_document(archive):
  key, record, work = stage_document(archive)

  with pytest.raises(Failure, match="reused document identity"):
    archive.document(key, record, work=None)

  assert key not in archive.data["documents"]
  assert archive.exists(work)


def test_changed_signature_after_retrieval_prevents_reuse(archive):
  key = add_document(archive)
  record = copy.deepcopy(archive.data["documents"][key])
  archive.write(
    record["folder"] + "/" + record["original"], b"%PDF-original"
  )

  with pytest.raises(Failure, match="after retrieval verification"):
    archive.document(key, record, work=None)


def test_new_download_clears_previous_processing_results(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  archive.publish(spec, message)

  incoming_key, incoming, work = stage_document(
    archive, raw=b"%PDF-replacement"
  )
  assert incoming_key == key
  archive.document(key, incoming, work)

  record = archive.data["documents"][key]
  assert record["result"] is None
  assert record["attempts"] == {}
  assert not archive.exists(record["folder"] + "/text.md")


def test_untracked_bundle_content_prevents_replacement(archive):
  key = add_document(archive)
  doc = archive.data["documents"][key]
  archive.write(doc["folder"] + "/user-notes.txt", b"keep")

  _, incoming, work = stage_document(archive, raw=b"%PDF-new")
  with pytest.raises(Failure, match="untracked file"):
    archive.document(key, incoming, work)

  assert archive.read(doc["folder"] + "/user-notes.txt", 100) == b"keep"
  assert archive.data["documents"][key]["sha256"] == doc["sha256"]
  assert not archive.exists(".ctl/pending.json")


def test_missing_tracked_original_can_be_replaced_by_refresh(archive):
  key = add_document(archive)
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/" + doc["original"])
  _, incoming, work = stage_document(archive, raw=b"%PDF-restored")

  archive.document(key, incoming, work)

  assert archive.data["documents"][key]["sha256"] == digest(b"%PDF-restored")


def test_untracked_page_destination_is_not_overwritten(archive):
  add_course(archive)
  archive.write(FOLDER + "/page.md", b"user file")

  with pytest.raises(Failure, match="untracked page"):
    add_page(archive)

  assert archive.read(FOLDER + "/page.md", 100) == b"user file"


def test_assigned_page_path_cannot_change(archive):
  add_page(archive)

  with pytest.raises(Failure, match="assigned publication path"):
    archive.item("page", {
      "path": FOLDER + "/renamed.md",
    }, None)


def test_duplicate_course_identity_is_rejected_before_checkpoint(archive):
  add_course(archive)

  with pytest.raises(Failure, match="duplicate course identity"):
    archive.item("other-key", {
      "title": "Other",
      "path": "documents/Other/index.md",
      "folder": "documents/Other",
      "parent": "index",
      "status": "empty",
      "course_id": "one",
      "include_submissions": True,
      "children": [],
    }, None)


def test_corrupted_staged_original_never_creates_a_receipt(archive):
  key, record, work = stage_document(archive)
  archive.write(work + "/new/" + record["original"], b"%PDF-modified")
  record["signature"] = signature(
    archive.path(work + "/new/" + record["original"])
  )
  record["bytes"] = record["signature"]["size"]

  with pytest.raises(Failure, match="identity mismatch"):
    archive.document(key, record, work)

  assert not archive.exists(".ctl/pending.json")
  assert key not in archive.data["documents"]


@pytest.mark.parametrize("point", ["before_moves", "after_backup", "after_install"])
def test_pending_page_publication_recovers_each_move_state(
  archive, views, point
):
  path = add_page(archive, body="Old body\n")
  record, work = stage_page(archive, "page", "New body\n")
  leave_receipt(archive, "items", "page", record, work)

  if point in {"after_backup", "after_install"}:
    archive.move(path, work + "/old")
  if point == "after_install":
    archive.move(work + "/new", path)

  root = archive.root
  archive.close()
  reopened = Archive(root, create=False)

  try:
    assert reopened.exists(".ctl/pending.json")
    assert reopened.data["items"]["page"]["content_sha256"] != digest(
      b"New body\n"
    )
    views.clear()
    reopened.prepare_export()

    assert reopened.read_content(
      path, reopened.data["items"]["page"]
    ) == b"New body\n"
    assert not reopened.exists(".ctl/pending.json")
    assert not reopened.exists(work)
    assert views == []
  finally:
    reopened.close()


def test_opening_does_not_recover_before_account_check(archive):
  path = add_page(archive, body="Old body\n")
  archive.account("student-1")
  record, work = stage_page(archive, "page", "New body\n")
  leave_receipt(archive, "items", "page", record, work)
  root = archive.root
  archive.close()
  reopened = Archive(root, create=False)

  try:
    with pytest.raises(Failure, match="another account"):
      reopened.account("student-2")

    assert reopened.exists(".ctl/pending.json")
    assert reopened.read_content(
      path, reopened.data["items"]["page"]
    ) == b"Old body\n"
  finally:
    reopened.close()


def test_first_document_publication_recovers_without_attaching_it(archive):
  key, record, work = stage_document(archive)
  leave_receipt(archive, "documents", key, record, work)

  archive.recover()

  assert key in archive.data["documents"]
  assert archive.data["items"]["page"]["documents"] == []
  assert archive.document_keys([COURSE]) == []


def test_recovery_after_manifest_checkpoint_accepts_installed_signature(archive):
  key = add_document(archive)
  _, incoming, work = stage_document(archive, raw=b"%PDF-new")
  receipt = leave_receipt(archive, "documents", key, incoming, work)
  archive.move(receipt["target"], work + "/old")
  archive.move(work + "/new", receipt["target"])
  installed = copy.deepcopy(incoming)
  installed["signature"] = signature(
    archive.path(installed["folder"] + "/" + installed["original"])
  )
  archive.data["documents"][key] = installed
  archive.checkpoint()

  archive.recover()

  assert archive.data["documents"][key]["signature"] == installed["signature"]
  assert not archive.exists(work)
  assert not archive.exists(".ctl/pending.json")


def test_failure_removing_receipt_is_recoverable(archive, monkeypatch):
  path = add_page(archive, body="Old\n")
  record, work = stage_page(archive, "page", "New\n")
  real_remove = archive.remove

  def fail(relative):
    if relative == ".ctl/pending.json":
      raise StorageFailure("receipt removal failed")
    real_remove(relative)

  monkeypatch.setattr(archive, "remove", fail)

  with pytest.raises(StorageFailure, match="receipt removal"):
    archive.install("items", "page", record, work)

  assert archive.exists(".ctl/pending.json")
  assert archive.read_content(path, archive.data["items"]["page"]) == b"New\n"

  monkeypatch.setattr(archive, "remove", real_remove)
  archive.recover()

  assert not archive.exists(".ctl/pending.json")
  assert not archive.exists(work)


def test_move_failure_preserves_receipt_and_backup(archive, monkeypatch):
  path = add_page(archive, body="Old\n")
  record, work = stage_page(archive, "page", "New\n")
  real_move = archive.move

  def fail(source, target):
    if source == work + "/new":
      raise StorageFailure("installation interrupted")
    real_move(source, target)

  monkeypatch.setattr(archive, "move", fail)

  with pytest.raises(StorageFailure, match="interrupted"):
    archive.install("items", "page", record, work)

  assert archive.exists(".ctl/pending.json")
  assert archive.exists(work + "/old")
  assert archive.exists(work + "/new")
  assert not archive.exists(path)

  monkeypatch.setattr(archive, "move", real_move)
  archive.recover()

  assert archive.read_content(path, archive.data["items"]["page"]) == b"New\n"


def test_recovery_refuses_conflicting_targets(archive):
  path = add_page(archive, body="Old\n")
  record, work = stage_page(archive, "page", "New\n")
  leave_receipt(archive, "items", "page", record, work)
  archive.write(work + "/old", archive.read(path, 4096))

  with pytest.raises(Failure, match="conflicting publication"):
    archive.recover()

  assert archive.exists(".ctl/pending.json")
  assert archive.read_content(path, archive.data["items"]["page"]) == b"Old\n"


def test_recovery_refuses_manifest_disagreement(archive):
  add_page(archive)
  record, work = stage_page(archive, "page", "New\n")
  leave_receipt(archive, "items", "page", record, work)
  archive.data["items"]["page"]["title"] = "Unrelated change"

  with pytest.raises(Failure, match="disagrees"):
    archive.recover()

  assert archive.exists(".ctl/pending.json")


def test_recovery_refuses_damaged_staged_source(archive):
  add_page(archive)
  record, work = stage_page(archive, "page", "New body\n")
  leave_receipt(archive, "items", "page", record, work)
  raw = archive.read(work + "/new", 4096)
  archive.write(work + "/new", raw.replace(b"New body", b"Bad body"))

  with pytest.raises(SourceProblem, match="changed"):
    archive.recover()

  assert archive.exists(".ctl/pending.json")
  assert not archive.exists(work + "/old")


def test_cleanup_cannot_discard_pending_publication(archive):
  add_page(archive)
  record, work = stage_page(archive, "page", "New\n")
  leave_receipt(archive, "items", "page", record, work)

  with pytest.raises(Failure, match="recovery incomplete"):
    archive.cleanup_work()

  assert archive.exists(work)


def test_cleanup_rejects_unknown_workspace_entries(archive):
  archive.mkdir(".ctl/work/not-a-token")

  with pytest.raises(Failure, match="unrecognized work entry"):
    archive.cleanup_work()


def test_processing_publication_installs_verified_products(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key, body="자료\n".encode())

  archive.publish(spec, message)

  doc = archive.data["documents"][key]
  product = doc["result"]["nodes"][""]["products"]["convert"]
  assert archive.read_content(
    doc["folder"] + "/text.md",
    product,
    size=doc["result"]["files"]["text.md"],
  ) == "자료\n".encode()
  assert archive.reusable(doc, ["convert"])
  assert not archive.exists(spec["work"])
  assert archive.data["items"]["page"]["documents"] == [key]
  validate_manifest(archive.data)


def test_local_reprocessing_never_resurrects_removed_references(archive):
  key = add_document(archive)
  archive.item("page", {"documents": []}, "Current page\n")
  spec, message = processing_job(archive, key)

  archive.publish(spec, message)

  assert archive.data["items"]["page"]["documents"] == []
  assert archive.document_keys([COURSE]) == []
  assert archive.course_for_document(key) == COURSE
  assert archive.data["documents"][key]["result"] is not None


def test_submission_exclusion_survives_explicit_reprocessing(archive):
  key = add_document(archive, role="submission")
  archive.item(COURSE, {"include_submissions": False}, None)
  spec, message = processing_job(archive, key)

  archive.publish(spec, message)

  assert archive.data["items"]["page"]["documents"] == [key]
  assert archive.data["documents"][key]["role"] == "submission"
  assert archive.document_keys([COURSE]) == []
  assert archive.course_for_document(key) == COURSE


def test_submission_exclusion_also_applies_to_retained_failed_receipts(archive):
  key = add_document(archive, role="submission")
  archive.item(COURSE, {"include_submissions": False}, None)
  archive.item("page", {
    "status": "failed",
    "message": "receipt refresh failed",
  }, None)

  assert archive.data["items"]["page"]["documents"] == [key]
  assert archive.document_keys([COURSE]) == []


def test_processing_success_clears_only_requested_root_failure(archive):
  key = add_document(archive)
  archive.processing_failure(key, ["convert", "ocr"], "earlier failure")
  spec, message = processing_job(archive, key)

  archive.publish(spec, message)

  attempts = archive.data["documents"][key]["attempts"]
  assert "convert" not in attempts
  assert attempts["ocr"]["error"] == "earlier failure"


@pytest.mark.parametrize(
  ("field", "value"),
  [
    ("seconds", True),
    ("seconds", -1),
    ("seconds", float("nan")),
    ("notes", ["x\nunsafe"]),
    ("fatal", []),
    ("error", ""),
  ],
)
def test_malformed_worker_completion_does_not_change_bundle(
  archive, field, value
):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  message[field] = value

  with pytest.raises(WorkerResultFailure):
    archive.publish(spec, message)

  assert archive.data["documents"][key]["result"] is None
  assert not archive.exists(".ctl/pending.json")


def test_worker_fatal_storage_error_is_not_softened(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  message.update(result=None, fatal="disk failure")

  with pytest.raises(StorageFailure, match="disk failure"):
    archive.publish(spec, message)


def test_worker_error_is_a_retryable_root_failure_for_the_scheduler(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  message.update(result=None, error="decoder failure")

  with pytest.raises(WorkerResultFailure, match="decoder failure"):
    archive.publish(spec, message)


def test_worker_must_report_each_requested_operation(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  spec["steps"].append("ocr")

  with pytest.raises(WorkerResultFailure, match="omitted"):
    archive.publish(spec, message)


def test_worker_operation_must_use_current_settings(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  node = message["result"]["nodes"][""]
  node["attempts"]["convert"]["settings"] = {"version": -1}

  with pytest.raises(WorkerResultFailure, match="omitted"):
    archive.publish(spec, message)


def test_worker_source_change_is_detected_before_publication(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  doc = archive.data["documents"][key]
  archive.write(doc["folder"] + "/" + doc["original"], b"%PDF-changed")

  with pytest.raises(SourceProblem, match="during processing"):
    archive.publish(spec, message)

  assert archive.data["documents"][key]["result"] is None
  assert not archive.exists(".ctl/pending.json")


def test_worker_source_is_rechecked_after_staging_validation(
  archive, monkeypatch
):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  original_prepare = archive._prepare_publication

  def changed(*args, **kwargs):
    receipt = original_prepare(*args, **kwargs)
    doc = archive.data["documents"][key]
    archive.write(doc["folder"] + "/" + doc["original"], b"%PDF-changed")
    return receipt

  monkeypatch.setattr(archive, "_prepare_publication", changed)

  with pytest.raises(SourceProblem, match="during processing"):
    archive.publish(spec, message)

  assert not archive.exists(".ctl/pending.json")


def test_worker_same_size_text_corruption_is_rejected(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key, body=b"Good text\n")
  path = spec["work"] + "/new/text.md"
  archive.write(path, archive.read(path, 4096).replace(b"Good", b"Evil"))

  with pytest.raises(WorkerResultFailure, match="damaged"):
    archive.publish(spec, message)

  assert archive.data["documents"][key]["result"] is None
  assert not archive.exists(".ctl/pending.json")


def test_same_size_cached_text_corruption_disables_reuse(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key, body=b"Good text\n")
  archive.publish(spec, message)
  doc = archive.data["documents"][key]
  path = doc["folder"] + "/text.md"
  archive.write(path, archive.read(path, 4096).replace(b"Good", b"Evil"))

  assert not archive.reusable(doc, ["convert"])

  with pytest.raises(SourceProblem, match="changed"):
    archive.read_content(
      path, doc["result"]["nodes"][""]["products"]["convert"]
    )


def test_missing_cached_output_disables_reuse(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  archive.publish(spec, message)
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/text.md")

  assert not archive.reusable(doc, ["convert"])


def test_settings_change_disables_reuse(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  archive.publish(spec, message)
  doc = archive.data["documents"][key]

  archive.settings["convert"] = {
    **archive.settings["convert"], "version": 999,
  }
  assert not archive.reusable(doc, ["convert"])


def test_recorded_root_failure_requires_retry(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  archive.publish(spec, message)
  archive.processing_failure(key, ["convert"], "worker timed out")

  assert not archive.reusable(archive.data["documents"][key], ["convert"])


def test_cached_assets_are_checked_against_content_address(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  raw = b"raster"
  asset = "images/" + digest(raw) + ".png"
  product = message["result"]["nodes"][""]["products"]["convert"]
  product["outputs"].append(asset)
  message["result"]["files"][asset] = len(raw)
  archive.write(spec["work"] + "/new/" + asset, raw)
  archive.publish(spec, message)
  doc = archive.data["documents"][key]

  assert cached_result_valid(archive.path(doc["folder"]), doc["result"])
  archive.write(doc["folder"] + "/" + asset, b"broken")
  assert not cached_result_valid(archive.path(doc["folder"]), doc["result"])


def test_cached_nested_originals_are_hashed(archive):
  key = add_document(archive)
  spec, message = processing_job(archive, key)
  result = message["result"]
  child = "contents/source"
  raw = b"print(1)\n"
  result["nodes"][""]["children"] = [child]
  result["nodes"][child] = {
    "name": "project/src/main.py",
    "kind": ".py",
    "original": "main.py",
    "bytes": len(raw),
    "sha256": digest(raw),
    "children": [],
    "problems": [],
    "retry": False,
    "visuals": None,
    "products": {},
    "attempts": {},
  }
  result["files"][child + "/main.py"] = len(raw)
  result["files"]["index.md"] = 6
  archive.write(spec["work"] + "/new/" + child + "/main.py", raw)
  archive.write(spec["work"] + "/new/index.md", b"index\n")
  archive.publish(spec, message)
  doc = archive.data["documents"][key]
  archive.write(doc["folder"] + "/" + child + "/main.py", b"print(2)\n")

  assert not archive.reusable(doc, ["convert"])


def test_missing_source_page_is_reported_without_recreating_its_body(archive):
  path = add_page(archive)
  archive.remove(path)
  archive.scan_missing()

  assert "page" in archive.missing
  assert {"page", COURSE, "index"} <= archive.incomplete()

  with pytest.raises(SourceProblem, match="missing"):
    archive.read_content(path, archive.data["items"]["page"])


def test_same_size_page_corruption_is_not_treated_as_empty(archive):
  path = add_page(archive, body="Good text\n")
  item = archive.data["items"]["page"]
  archive.write(path, archive.read(path, 4096).replace(b"Good", b"Evil"))

  with pytest.raises(SourceProblem, match="changed"):
    archive.read_content(path, item)


def test_read_content_checks_expected_product_size(archive):
  path = add_page(archive)
  item = archive.data["items"]["page"]
  size = len(archive.read(path, 4096))

  with pytest.raises(SourceProblem, match="size changed"):
    archive.read_content(path, item, size=size + 1)


def test_missing_references_propagate_incompleteness(archive):
  add_page(archive)
  archive.item("page", {
    "documents": ["missing-document"],
    "related": ["missing-receipt"],
  }, None)

  assert {"page", COURSE, "index"} <= archive.incomplete()
  assert archive.document_keys([COURSE]) == []
  assert ("missing-receipt", None) in list(archive.walk_course(COURSE))


def test_walk_course_preserves_reference_order_and_deduplicates_edges(archive):
  add_page(archive, key="first")
  add_page(archive, key="second")
  archive.item(COURSE, {
    "children": ["second", "first"],
    "related": ["first"],
  }, None)

  assert [key for key, _ in archive.walk_course(COURSE)] == [
    COURSE, "second", "first",
  ]


def test_walk_course_detects_cycles_without_recursing(archive):
  add_page(archive)
  archive.item("page", {"related": [COURSE]}, None)

  with pytest.raises(Failure, match="cycle"):
    list(archive.walk_course(COURSE))


def test_walk_course_rejects_conflicting_parent(archive):
  add_page(archive)
  archive.data["items"]["page"]["parent"] = "other"

  with pytest.raises(Failure, match="conflicting parent"):
    list(archive.walk_course(COURSE))


def test_walk_course_cannot_enter_another_course(archive):
  add_course(archive)
  add_course(archive, key="course/two", folder="documents/Second")
  archive.data["items"][COURSE]["children"] = ["course/two"]
  archive.data["items"]["course/two"]["parent"] = COURSE

  with pytest.raises(Failure, match="another course"):
    list(archive.walk_course(COURSE))


def test_walk_course_checks_assigned_path_scope(archive):
  add_page(archive)
  archive.data["items"]["page"]["path"] = "documents/Other/page.md"

  with pytest.raises(Failure, match="assigned folder"):
    list(archive.walk_course(COURSE))


def test_current_document_selection_is_course_specific(archive):
  first = add_document(archive)
  add_course(archive, key="course/two", folder="documents/Second")
  add_page(
    archive,
    key="second",
    folder="documents/Second",
    parent="course/two",
  )
  second = add_document(
    archive, owner="second", identifier="two", role="submission"
  )
  archive.item(COURSE, {"include_submissions": False}, None)

  assert archive.document_keys([COURSE]) == [first]
  assert archive.document_keys(["course/two"]) == [second]
  assert archive.document_keys([COURSE, COURSE]) == [first]


def test_document_reference_cannot_claim_another_pages_attachment(archive):
  key = add_document(archive)
  add_page(archive, key="other")
  archive.item("other", {"documents": [key]}, None)

  with pytest.raises(Failure, match="owner reference"):
    archive.document_keys([COURSE])


def test_unreferenced_historical_document_still_has_a_course(archive):
  key = add_document(archive, attach=False)

  assert archive.document_keys([COURSE]) == []
  assert archive.course_for_document(key) == COURSE


def test_archive_level_document_has_no_course(archive):
  add_course(archive)
  add_page(
    archive,
    key="dashboard",
    folder="documents",
    parent="index",
  )
  key = add_document(archive, owner="dashboard")

  assert archive.course_for_document(key) is None
  assert archive.document_keys([COURSE]) == []


def test_retained_absent_page_remains_traversable_for_export_diagnostics(archive):
  path = add_page(archive, body="Historical instructions\n")
  archive.item("page", {
    "status": "absent",
    "message": "menu not offered",
  }, None)

  items = dict(archive.walk_course(COURSE))
  assert items["page"]["status"] == "absent"
  assert archive.read_content(path, items["page"]) == b"Historical instructions\n"


def test_marking_a_page_marks_its_ancestor_views(archive):
  add_page(archive)
  archive.dirty_items.clear()

  archive.mark("page")

  assert archive.dirty_items == {"page", COURSE, "index"}


def test_view_failure_keeps_unrendered_items_dirty(archive, monkeypatch):
  add_page(archive)
  archive.dirty_items = {"page"}

  def fail(*args):
    raise StorageFailure("view write failed")

  monkeypatch.setattr(sys.modules["kctl.views"], "render", fail)

  with pytest.raises(StorageFailure, match="view write"):
    archive.flush_views()

  assert "page" in archive.dirty_items


def test_run_lifecycle_and_manifest_checkpoint(archive):
  archive.begin("process")
  assert archive.active_run
  assert archive.data["run"]["status"] == "running"

  with pytest.raises(Failure, match="already active"):
    archive.begin("sync")

  archive.finish("partial", local={"processed_roots": 1})
  assert not archive.active_run
  assert archive.data["run"]["status"] == "partial"
  assert archive.data["run"]["local"] == {"processed_roots": 1}
  validate_manifest(archive.data)

  saved = parse_json(archive.read(".ctl/manifest.json", MANIFEST_LIMIT))
  assert saved["run"] == archive.data["run"]


def test_export_is_not_a_persisted_run_command(archive):
  with pytest.raises(Failure, match="invalid archive command"):
    archive.begin("export")


def test_finish_requires_an_active_run(archive):
  with pytest.raises(Failure, match="no active"):
    archive.finish("complete")


def test_finish_refuses_pending_publication(archive):
  add_page(archive)
  archive.begin("sync")
  record, work = stage_page(archive, "page", "New\n")
  leave_receipt(archive, "items", "page", record, work)

  with pytest.raises(Failure, match="recovery required"):
    archive.finish("complete")

  assert archive.active_run
  assert archive.data["run"]["status"] == "running"


def test_bundle_validation_rejects_untracked_directories(archive):
  key = add_document(archive)
  doc = archive.data["documents"][key]
  archive.mkdir(doc["folder"] + "/untracked")

  with pytest.raises(Failure, match="untracked directory"):
    check_bundle(
      archive.path(doc["folder"]), {doc["original"]}, exact=True
    )


def test_bundle_validation_rejects_symlink_assets(archive):
  key = add_document(archive)
  doc = archive.data["documents"][key]
  path = archive.path(doc["folder"])
  (path / "link").symlink_to(path / doc["original"])

  with pytest.raises(ProtectionFailure):
    check_bundle(path, {doc["original"], "link"}, exact=True)


def test_atomic_manifest_write_failure_leaves_prior_manifest(archive, monkeypatch):
  before = archive.read(".ctl/manifest.json", MANIFEST_LIMIT)
  archive.data["account"] = "student-1"

  def fail(*args, **kwargs):
    raise OSError("replace failed")

  monkeypatch.setattr(os, "replace", fail)

  with pytest.raises(StorageFailure):
    archive.checkpoint()

  assert archive.read(".ctl/manifest.json", MANIFEST_LIMIT) == before
