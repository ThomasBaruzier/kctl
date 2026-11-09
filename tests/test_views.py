import copy

import pytest

import kctl.views as views

from kctl.archive import Archive
from kctl.core import SourceProblem, digest, identity
from kctl.records import attempt_record, outcome
from kctl.storage import signature


COURSE = "course/one"
OWNER = COURSE + "/page"
FOLDER = "documents/Course"


@pytest.fixture
def archive(private_dir):
  value = Archive(private_dir / "archive", create=True)
  value.prepare()
  value.item("index", {
    "title": "Archive",
    "path": "documents/index.md",
    "folder": "documents",
    "status": "archived",
    "parent": None,
    "children": [COURSE],
  }, None)
  value.item(COURSE, {
    "title": "Course",
    "path": FOLDER + "/index.md",
    "folder": FOLDER,
    "status": "archived",
    "parent": "index",
    "course_id": "one",
    "include_submissions": True,
    "children": [OWNER],
  }, None)
  value.item(OWNER, {
    "title": "Source page",
    "path": FOLDER + "/page.md",
    "folder": FOLDER,
    "status": "archived",
    "parent": COURSE,
    "documents": [],
    "related": [],
    "problems": [],
    "source": "https://ctl.kmu.ac.kr/Course.do?cmd=viewStudyHome",
  }, "## Documents\nFetched: source statement.\n\nMeaningful body.\n")

  try:
    yield value
  finally:
    value.close()


def page_bytes(archive):
  return archive.read(FOLDER + "/page.md", 1024 * 1024)


def add_document(archive, name="source.txt", raw=b"Source attachment\n"):
  url = "https://ctl.kmu.ac.kr/fileDownServlet?id=1"
  key = identity(OWNER, url)
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/new/" + name, raw)
  record = {
    "owner": OWNER,
    "url": url,
    "name": name,
    "folder": FOLDER + "/Document",
    "original": name,
    "role": "attachment",
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": signature(archive.path(work + "/new/" + name)),
    "cache": "downloaded",
    "result": None,
    "attempts": {},
  }
  archive.document(key, record, work)
  archive.item(OWNER, {"documents": [key]}, None)
  return key


def add_result(archive, key, *, body=b"Converted source\n", retained=False):
  doc = copy.deepcopy(archive.data["documents"][key])
  settings = copy.deepcopy(archive.settings["convert"])
  header = b"# Generated\n\n"
  raw = header + body
  product = outcome(
    settings,
    outputs={"text.md"},
    markdown="text.md",
    content_bytes=len(body),
    content_sha256=digest(body),
  )
  product["header_bytes"] = len(header)
  node = {
    "name": doc["name"],
    "original": doc["original"],
    "kind": ".pdf",
    "sha256": doc["sha256"],
    "bytes": doc["bytes"],
    "children": [],
    "problems": [],
    "retry": False,
    "visuals": None,
    "products": {"convert": product},
    "attempts": {},
  }

  if retained:
    failed = outcome(
      settings,
      problems=["latest conversion failed"],
      retry=True,
      failed=True,
    )
    node["attempts"]["convert"] = attempt_record(failed, retained=True)

  files = {"text.md": len(raw)}
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(
    work + "/new/" + doc["original"],
    archive.read(doc["folder"] + "/" + doc["original"], 4096),
  )
  archive.write(work + "/new/text.md", raw)

  if retained:
    archive.write(work + "/new/index.md", b"# Details\n")
    files["index.md"] = len(b"# Details\n")

  doc["result"] = {
    "source": doc["sha256"],
    "settings": copy.deepcopy(archive.settings["walk"]),
    "nodes": {"": node},
    "files": files,
    "finished": "2026-11-04T00:00:00+00:00",
  }
  archive.install("documents", key, doc, work)


def test_view_rendering_preserves_source_prefix_and_content(archive):
  item = archive.data["items"][OWNER]
  before = page_bytes(archive)
  source = archive.read_content(item["path"], item)

  archive.flush_views()

  after = page_bytes(archive)
  assert after.startswith(before)
  assert archive.read_content(item["path"], item) == source
  assert b"Fetched: source statement." in after
  assert b"## Documents" in source
  assert b"[Back](index.md)" in after


def test_repeated_rendering_is_byte_stable(archive):
  archive.flush_views()
  before = page_bytes(archive)
  metadata = signature(archive.path(FOLDER + "/page.md"))

  archive.mark(OWNER)
  archive.flush_views()

  assert page_bytes(archive) == before
  assert signature(archive.path(FOLDER + "/page.md")) == metadata


def test_old_generated_footer_is_replaced_not_accumulated(archive):
  path = FOLDER + "/page.md"
  archive.write(path, page_bytes(archive) + b"\nOBSOLETE GENERATED FOOTER\n")
  archive.mark(OWNER)

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "OBSOLETE GENERATED FOOTER" not in text
  assert text.count("[Back]") == 1
  assert text.count("Meaningful body.") == 1


def test_human_document_links_do_not_change_source_body(archive):
  key = add_document(archive)
  before = archive.read_content(
    FOLDER + "/page.md", archive.data["items"][OWNER]
  )

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "source.txt" in text
  assert "[original](Document/source.txt)" in text
  assert archive.read_content(
    FOLDER + "/page.md", archive.data["items"][OWNER]
  ) == before
  assert archive.data["items"][OWNER]["documents"] == [key]


def test_processed_document_links_include_retention_and_details(archive):
  key = add_document(archive, "lesson.pdf", b"%PDF-original")
  add_result(archive, key, retained=True)

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "contents / details" in text
  assert "text (retained)" in text
  assert "latest conversion failed" in text


def test_conversion_gallery_without_native_text_is_labeled_images(archive):
  key = add_document(archive, "lesson.pdf", b"%PDF-original")
  add_result(archive, key, body=b"")

  archive.flush_views()

  assert "[images](Document/text.md)" in page_bytes(archive).decode()
  assert "[text](Document/text.md)" not in page_bytes(archive).decode()


def test_missing_document_original_is_shown_without_a_broken_link(archive):
  key = add_document(archive)
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/" + doc["original"])

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "original missing" in text
  assert "[original]" not in text


def test_missing_document_record_remains_visible_as_unavailable(archive):
  archive.item(OWNER, {"documents": ["missing"]}, None)

  archive.flush_views()

  assert "Document unavailable." in page_bytes(archive).decode()


def test_processing_root_failure_is_visible_without_replacing_source(archive):
  key = add_document(archive)
  archive.processing_failure(key, ["convert", "ocr"], "worker timed out")

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "latest root attempt failed" in text
  assert "worker timed out" in text
  assert "Meaningful body." in text


@pytest.mark.parametrize(
  ("status", "message"),
  [
    ("failed", "Refresh failed"),
    ("absent", "menu not offered"),
    ("partial", "retrieval incomplete"),
  ],
)
def test_retained_page_is_clearly_labeled(archive, status, message):
  archive.item(OWNER, {"status": status, "message": message}, None)

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "Showing previous content." in text
  assert message in text
  assert "Meaningful body." in text


def test_partial_current_content_is_not_mislabeled_as_previous(archive):
  archive.item(OWNER, {
    "status": "partial",
    "message": "",
    "problems": ["embedded content was not captured"],
  }, "New partial source\n")

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "New partial source" in text
  assert "Extraction incomplete" in text
  assert "embedded content was not captured" in text
  assert "Showing previous content" not in text


def test_missing_source_page_is_not_fabricated_during_rendering(archive):
  archive.remove(FOLDER + "/page.md")
  archive.scan_missing()
  archive.mark(OWNER)

  archive.flush_views()

  assert not archive.exists(FOLDER + "/page.md")
  assert OWNER in archive.missing
  index = archive.read("documents/index.md", 4096).decode()
  assert "stored page missing" in index


def test_corrupted_source_body_is_not_overwritten_by_rendering(archive):
  path = FOLDER + "/page.md"
  original = page_bytes(archive)
  corrupted = original.replace(b"Meaningful body.", b"Corrupted body!!")

  assert corrupted != original
  assert len(corrupted) == len(original)

  archive.write(path, corrupted)
  archive.mark(OWNER)

  with pytest.raises(SourceProblem, match="changed"):
    views.render(archive, OWNER, archive.incomplete())

  archive.flush_views()

  assert archive.read(path, 4096) == corrupted
  assert OWNER in archive.dirty_items
  assert OWNER in archive.damaged
  assert COURSE in archive.incomplete()
  index = archive.read("documents/index.md", 4096).decode()
  assert "stored source content changed" in index


def test_truncated_source_body_is_not_overwritten_by_rendering(archive):
  path = FOLDER + "/page.md"
  item = archive.data["items"][OWNER]
  end = item["header_bytes"] + item["content_bytes"]
  truncated = page_bytes(archive)[:end - 1]
  archive.write(path, truncated)
  archive.mark(OWNER)

  with pytest.raises(SourceProblem, match="boundaries"):
    views.render(archive, OWNER, archive.incomplete())

  archive.flush_views()

  assert archive.read(path, 4096) == truncated
  assert OWNER in archive.dirty_items
  assert OWNER in archive.damaged


def test_index_navigation_reports_incomplete_descendants(archive):
  archive.item(OWNER, {"status": "failed", "message": "layout changed"}, None)

  archive.flush_views()

  course_index = archive.read(FOLDER + "/index.md", 4096).decode()
  root_index = archive.read("documents/index.md", 4096).decode()
  assert "[Source page](page.md) - incomplete" in course_index
  assert "[Course](Course/index.md) - incomplete" in root_index
  assert "layout changed" in root_index


def test_missing_child_reference_has_no_fabricated_link(archive):
  archive.item(COURSE, {"children": ["missing-child"]}, None)

  archive.flush_views()

  text = archive.read(FOLDER + "/index.md", 4096).decode()
  assert "missing-child - unavailable" in text
  assert "[missing-child]" not in text


def test_related_pages_are_rendered_separately_from_source(archive):
  related = COURSE + "/receipt"
  archive.item(related, {
    "title": "Submission",
    "path": FOLDER + "/receipt.md",
    "folder": FOLDER,
    "status": "empty",
    "parent": OWNER,
    "documents": [],
    "related": [],
    "problems": [],
    "empty_listing": False,
  }, "No submission recorded\n")
  archive.item(OWNER, {"related": [related]}, None)

  archive.flush_views()

  text = page_bytes(archive).decode()
  assert "## Related pages" in text
  assert "[Submission](receipt.md) - empty" in text
  assert "No submission recorded" not in archive.read_content(
    FOLDER + "/page.md", archive.data["items"][OWNER]
  ).decode()
  receipt = archive.read(FOLDER + "/receipt.md", 4096).decode()
  assert "No submission recorded" in receipt
  assert "[Back](page.md)" in receipt


def test_known_empty_index_and_unfinished_index_have_distinct_messages(archive):
  archive.item(COURSE, {"children": [], "status": "empty"}, None)
  archive.flush_views()
  assert "No entries." in archive.read(FOLDER + "/index.md", 4096).decode()

  archive.item(COURSE, {
    "status": "partial", "message": "incomplete listing",
  }, None)
  archive.flush_views()
  text = archive.read(FOLDER + "/index.md", 4096).decode()
  assert "No entries recorded." in text
  assert "incomplete listing" in text


def test_metadata_only_page_has_a_human_placeholder_not_a_source_body(archive):
  key = COURSE + "/pending"
  archive.item(key, {
    "title": "Pending",
    "path": FOLDER + "/pending.md",
    "folder": FOLDER,
    "status": "partial",
    "parent": COURSE,
  }, None)

  archive.flush_views()

  text = archive.read(FOLDER + "/pending.md", 4096).decode()
  assert "Page content not archived yet." in text
  assert "content_bytes" not in archive.data["items"][key]


def test_missing_or_pathless_items_do_not_produce_files(archive):
  archive.item(COURSE + "/absent", {
    "title": "Absent menu",
    "status": "absent",
    "parent": COURSE,
  }, None)

  views.render(archive, "missing", set())
  views.render(archive, COURSE + "/absent", set())

  assert not archive.exists(FOLDER + "/absent.md")


def test_view_rendering_does_not_modify_run_or_relationships(archive):
  key = add_document(archive)
  archive.begin("sync")
  archive.finish("complete")
  before = copy.deepcopy(archive.data)

  archive.mark(OWNER)
  archive.flush_views()

  assert archive.data == before
  assert archive.data["items"][OWNER]["documents"] == [key]
