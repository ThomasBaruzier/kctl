import copy
import io
import os
import sys
import time
import zipfile

from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import kctl
import kctl.content as content_module
import kctl.conversion as conversion_module
import kctl.processing as processing

from kctl.archive import Archive
from kctl.content import Blob, ContentWalker
from kctl.conversion import Engines, FileProcessor
from kctl.core import (
  BudgetReached,
  Failure,
  SourceProblem,
  StorageFailure,
  digest,
  identity,
  json_bytes,
  parse_json,
)
from kctl.exports import export_courses
from kctl.processing import DocumentJob, Processor, _copy_blob, worker_main
from kctl.records import (
  EMPTY_SHA256,
  error_outcome,
  outcome,
  validate_result_data,
)
from kctl.storage import signature, write_new
from kctl.workers import Workers


COURSE = "course/one"
OWNER = COURSE + "/page"
FOLDER = "documents/Course"


@pytest.fixture
def archive(private_dir, monkeypatch):
  module = ModuleType("kctl.views")
  module.render = lambda *args: None
  monkeypatch.setitem(sys.modules, "kctl.views", module)
  monkeypatch.setattr(kctl, "views", module, raising=False)

  value = Archive(private_dir / "archive", create=True)
  value.prepare()
  value.item("index", {
    "title": "Archive",
    "path": "documents/index.md",
    "folder": "documents",
    "status": "archived",
    "children": [COURSE],
    "parent": None,
  }, None)
  value.item(COURSE, {
    "title": "Course",
    "path": FOLDER + "/index.md",
    "folder": FOLDER,
    "course_id": "one",
    "include_submissions": True,
    "status": "archived",
    "children": [OWNER],
    "parent": "index",
  }, None)
  value.item(OWNER, {
    "title": "Page",
    "path": FOLDER + "/page.md",
    "folder": FOLDER,
    "status": "archived",
    "parent": COURSE,
    "documents": [],
    "related": [],
    "problems": [],
  }, "Source page\n")

  try:
    yield value
  finally:
    value.close()


def add_document(
  archive,
  name="lesson.html",
  raw=b"<p>Source</p>",
  *,
  role="attachment",
  attached=True,
):
  number = len(archive.data["documents"]) + 1
  url = f"https://ctl.kmu.ac.kr/fileDownServlet?id={number}"
  key = identity(OWNER, url)
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/new/" + name, raw)
  doc = {
    "owner": OWNER,
    "url": url,
    "name": name,
    "folder": FOLDER + f"/Document-{number}",
    "original": name,
    "role": role,
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": signature(archive.path(work + "/new/" + name)),
    "cache": "downloaded",
    "result": None,
    "attempts": {},
  }
  archive.document(key, doc, work)

  if attached:
    references = archive.data["items"][OWNER]["documents"]
    archive.item(OWNER, {"documents": [*references, key]}, None)

  return key


def job_spec(archive, key, *, force=False, steps=None, retry_steps=None):
  doc = copy.deepcopy(archive.data["documents"][key])
  work = archive.new_work()
  archive.mkdir(work + "/new")
  spec = {
    "key": key,
    "root": str(archive.root),
    "document": doc,
    "signature": copy.deepcopy(doc["signature"]),
    "settings": copy.deepcopy(archive.settings),
    "steps": ["convert", "ocr"] if steps is None else steps,
    "retry_steps": [] if retry_steps is None else retry_steps,
    "force": force,
    "work": work,
  }
  archive.write(work + "/job.json", json_bytes(spec))
  return spec


def run_job(archive, key, **options):
  spec = job_spec(archive, key, **options)
  engines = Engines()

  try:
    result = DocumentJob(spec, engines).run()
  finally:
    engines.close()

  message = {**result, "notes": [], "seconds": 0.1}
  return spec, message


def process_document(archive, key, **options):
  spec, message = run_job(archive, key, **options)
  archive.publish(spec, message)
  return archive.data["documents"][key]["result"]


def source_body(archive, key, step="convert", folder=""):
  doc = archive.data["documents"][key]
  product = doc["result"]["nodes"][folder]["products"][step]
  if product["markdown"] is None:
    return b""
  return archive.read_content(
    doc["folder"] + "/" + product["markdown"],
    product,
    size=doc["result"]["files"][product["markdown"]],
  )


def zip_bytes(entries):
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, raw in entries:
      archive.writestr(name, raw)
  return stream.getvalue()


@pytest.fixture
def converter(monkeypatch):
  state = SimpleNamespace(
    calls=[],
    bodies={"convert": b"Converted source\n", "ocr": b"OCR source\n"},
    failures={},
    visuals=False,
  )

  class Converter:
    def __init__(
      self,
      source,
      output,
      folder,
      node,
      step,
      settings,
      engines,
      cached=None,
      force_visuals=False,
    ):
      self.output = output
      self.folder = folder
      self.step = step
      self.settings = settings
      self.cached = cached
      self.force_visuals = force_visuals

    def run(self):
      state.calls.append({
        "step": self.step,
        "folder": self.folder,
        "cached": copy.deepcopy(self.cached),
        "force_visuals": self.force_visuals,
        "protected": set(self.output.protected),
      })
      error = state.failures.get(self.step)
      if error is not None:
        if isinstance(error, StorageFailure):
          raise error
        return error_outcome(self.settings[self.step], error), None

      visuals = None
      images = []

      if state.visuals:
        if self.cached and not self.force_visuals:
          for image in self.cached["images"]:
            self.output.claim(image["path"], image=True)
            images.append(copy.deepcopy(image))
        else:
          path = self.output.asset(
            self.folder, ".png", lambda stream: stream.write(b"raster")
          )
          images = [{
            "path": path,
            "key": Path(path).stem,
            "blank": False,
            "label": "Source image",
            "page": None,
          }]

        visuals = outcome(
          self.settings["visuals"],
          outputs={image["path"] for image in images},
        ) | {"images": images}

      raw = state.bodies[self.step]
      markdown = None

      if raw:
        filename = "text.md" if self.step == "convert" else "ocr.md"
        markdown = self.folder + "/" + filename if self.folder else filename
        try:
          self.output.write_text(markdown, raw)
        except BudgetReached as error:
          return error_outcome(self.settings[self.step], error), None

      return outcome(
        self.settings[self.step],
        outputs=set(self.output.claimed),
        markdown=markdown,
        content_bytes=len(raw),
        content_sha256=digest(raw),
      ), visuals

  monkeypatch.setattr(processing, "FileProcessor", Converter)
  return state


@pytest.fixture
def scheduler(monkeypatch):
  pools = []

  class Pool:
    def __init__(self, count, timeout, *, target):
      self.count = count
      self.timeout = timeout
      self.target = target
      self.active = []
      self.finished = deque()
      pools.append(self)

    def capacity(self):
      return len(self.active) < self.count

    def running(self):
      return len(self.active)

    def submit(self, spec):
      self.active.append(spec)

    def collect(self):
      while self.finished:
        spec, error = self.finished.popleft()
        self.active.remove(spec)
        yield spec, error

    def close(self):
      abandoned = list(self.active)
      self.active.clear()
      return abandoned

    def complete(self, spec, message=None, error=None):
      if message is not None:
        write_new(
          Path(spec["root"]) / spec["work"] / "result.json",
          json_bytes(message),
        )
      self.finished.append((spec, error))

  monkeypatch.setattr(processing, "Workers", Pool)
  return pools


def test_job_stages_original_without_altering_archive(archive, converter):
  key = add_document(archive)
  doc = copy.deepcopy(archive.data["documents"][key])
  spec, message = run_job(archive, key)
  stage = archive.path(spec["work"] + "/new")

  assert (stage / doc["original"]).read_bytes() == b"<p>Source</p>"
  assert (stage / "text.md").exists()
  assert (stage / "ocr.md").exists()
  assert archive.data["documents"][key] == doc
  assert message["error"] is None
  assert message["fatal"] is None
  validate_result_data(message["result"], doc)


def test_products_have_verified_source_boundaries(archive, converter):
  key = add_document(archive)
  result = process_document(archive, key)

  assert source_body(archive, key) == b"Converted source\n"
  assert source_body(archive, key, "ocr") == b"OCR source\n"
  product = result["nodes"][""]["products"]["convert"]
  assert product["header_bytes"] > 0
  assert product["content_sha256"] == digest(b"Converted source\n")
  assert [call["step"] for call in converter.calls] == ["convert", "ocr"]


def test_passthrough_text_does_not_create_duplicate_products(archive, converter):
  raw = b"# Readme\n\nSource content.\n"
  key = add_document(archive, name="README.md", raw=raw)
  result = process_document(archive, key)

  assert converter.calls == []
  assert result["nodes"][""]["kind"] == ".md"
  assert result["nodes"][""]["products"] == {}
  assert result["files"] == {}
  doc = archive.data["documents"][key]
  assert archive.read(doc["folder"] + "/README.md", 1024) == raw


def test_nested_source_files_are_extracted_without_conversion(archive, converter):
  key = add_document(
    archive,
    name="project.zip",
    raw=zip_bytes([
      ("src/main.py", b"print('source')\n"),
      ("README.md", b"# Project\n"),
    ]),
  )
  result = process_document(archive, key)

  assert converter.calls == []
  root = result["nodes"][""]
  assert root["kind"] == "zip"
  assert len(root["children"]) == 2
  assert "index.md" in result["files"]
  assert {
    result["nodes"][folder]["name"] for folder in root["children"]
  } == {"src/main.py", "README.md"}
  assert all(
    not node["products"] for node in result["nodes"].values()
  )


def test_nested_documents_receive_bundle_relative_products(archive, converter):
  key = add_document(
    archive,
    name="project.zip",
    raw=zip_bytes([("lessons/lesson.html", b"<p>Lesson</p>")]),
  )
  result = process_document(archive, key)
  folder = result["nodes"][""]["children"][0]

  assert source_body(archive, key, folder=folder) == b"Converted source\n"
  assert source_body(archive, key, "ocr", folder) == b"OCR source\n"
  assert result["nodes"][folder]["name"] == "lessons/lesson.html"
  assert all(call["folder"] == folder for call in converter.calls)


def test_cached_job_does_not_rediscover_or_reconvert(
  archive, converter, monkeypatch
):
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()

  def fail(*args, **kwargs):
    raise AssertionError("discovery should be reused")

  monkeypatch.setattr(ContentWalker, "walk", fail)
  process_document(archive, key)

  assert converter.calls == []
  assert source_body(archive, key) == b"Converted source\n"
  doc = archive.data["documents"][key]
  text = archive.read(doc["folder"] + "/text.md", 4096).decode()
  assert text.count("[Original]") == 1


def test_forced_job_rediscovers_and_regenerates_requested_products(
  archive, converter, monkeypatch
):
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()
  converter.bodies["convert"] = b"Regenerated\n"
  original_walk = ContentWalker.walk
  walks = []

  def counted(self, folder, depth=0):
    walks.append((folder, depth))
    return original_walk(self, folder, depth)

  monkeypatch.setattr(ContentWalker, "walk", counted)
  process_document(archive, key, force=True)

  assert walks == [("", 0)]
  assert [call["step"] for call in converter.calls] == ["convert", "ocr"]
  assert source_body(archive, key) == b"Regenerated\n"


def test_processing_settings_change_only_regenerates_affected_product(
  archive, converter
):
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()
  archive.settings["convert"] = {
    **archive.settings["convert"], "version": 99,
  }
  converter.bodies["convert"] = b"New settings\n"

  process_document(archive, key)

  assert [call["step"] for call in converter.calls] == ["convert"]
  assert source_body(archive, key) == b"New settings\n"
  assert source_body(archive, key, "ocr") == b"OCR source\n"


def test_visuals_from_conversion_are_reused_by_ocr(archive, converter):
  converter.visuals = True
  key = add_document(archive)
  result = process_document(archive, key)

  convert, ocr = converter.calls
  assert convert["cached"] is None
  assert ocr["cached"]["status"] == "complete"
  assert ocr["force_visuals"] is False
  assert result["nodes"][""]["visuals"]["status"] == "complete"
  assets = [path for path in result["files"] if path.startswith("images/")]
  assert len(assets) == 1


def test_forced_conversion_does_not_force_duplicate_visual_work_in_ocr(
  archive, converter
):
  converter.visuals = True
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()

  process_document(archive, key, force=True)

  assert converter.calls[0]["force_visuals"] is True
  assert converter.calls[1]["force_visuals"] is False
  assert converter.calls[1]["cached"]["status"] == "complete"


def test_failed_regeneration_retains_previous_source_and_boundaries(
  archive, converter
):
  key = add_document(archive)
  process_document(archive, key)
  converter.failures["convert"] = Failure("decoder failed")

  result = process_document(archive, key, force=True)
  node = result["nodes"][""]
  attempt = node["attempts"]["convert"]

  assert source_body(archive, key) == b"Converted source\n"
  assert attempt["retained"] is True
  assert attempt["retry"] is True
  assert node["products"]["convert"]["status"] == "complete"
  assert "index.md" in result["files"]
  doc = archive.data["documents"][key]
  raw = archive.read(doc["folder"] + "/text.md", 4096)
  assert b"Retained result" in raw
  assert b"Retained result" not in source_body(archive, key)


def test_fixed_limit_retains_previous_result_without_automatic_retry(
  archive, converter
):
  from kctl.core import LimitReached

  key = add_document(archive)
  process_document(archive, key)
  converter.failures["convert"] = LimitReached("fixed conversion limit")

  result = process_document(archive, key, force=True)
  attempt = result["nodes"][""]["attempts"]["convert"]

  assert attempt["retained"] is True
  assert attempt["retry"] is False
  assert archive.reusable(
    archive.data["documents"][key], ["convert", "ocr"]
  )

  converter.calls.clear()
  process_document(archive, key)
  assert converter.calls == []


def test_root_retry_marker_rediscovers_and_retries_requested_step(
  archive, converter, monkeypatch
):
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()
  original_walk = ContentWalker.walk
  walks = []

  def counted(self, folder, depth=0):
    walks.append(folder)
    return original_walk(self, folder, depth)

  monkeypatch.setattr(ContentWalker, "walk", counted)
  process_document(archive, key, retry_steps=["convert"])

  assert walks == [""]
  assert [call["step"] for call in converter.calls] == ["convert"]


def test_missing_requested_product_is_rebuilt(archive, converter):
  key = add_document(archive)
  process_document(archive, key)
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/text.md")
  converter.calls.clear()
  converter.bodies["convert"] = b"Repaired\n"

  process_document(archive, key)

  assert [call["step"] for call in converter.calls] == ["convert"]
  assert source_body(archive, key) == b"Repaired\n"


def test_same_size_product_corruption_is_rebuilt(archive, converter):
  key = add_document(archive)
  process_document(archive, key)
  doc = archive.data["documents"][key]
  relative = doc["folder"] + "/text.md"
  raw = archive.read(relative, 4096)
  archive.write(relative, raw.replace(b"Converted", b"Corrupted"))
  converter.calls.clear()

  process_document(archive, key)

  assert [call["step"] for call in converter.calls] == ["convert"]
  assert source_body(archive, key) == b"Converted source\n"


def test_unrequested_valid_product_is_preserved(archive, converter):
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()
  converter.bodies["convert"] = b"Updated conversion\n"

  process_document(archive, key, force=True, steps=["convert"])

  assert [call["step"] for call in converter.calls] == ["convert"]
  assert source_body(archive, key) == b"Updated conversion\n"
  assert source_body(archive, key, "ocr") == b"OCR source\n"


def test_damaged_unrequested_product_is_not_misrepresented_as_retained(
  archive, converter
):
  key = add_document(archive)
  process_document(archive, key)
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/ocr.md")

  result = process_document(archive, key, steps=["convert"])
  node = result["nodes"][""]

  assert "ocr" not in node["products"]
  assert node["attempts"]["ocr"]["retained"] is False
  assert node["attempts"]["ocr"]["retry"] is True
  assert "ocr.md" not in result["files"]
  assert "index.md" in result["files"]


def test_other_products_are_protected_during_regeneration(
  archive, converter, monkeypatch
):
  converter.bodies = {
    "convert": b"A" * 20,
    "ocr": b"B" * 20,
  }
  key = add_document(archive)
  process_document(archive, key)
  converter.calls.clear()
  converter.bodies["convert"] = b"C" * 90
  monkeypatch.setattr(processing, "CONTENT_LIMIT", 100)
  monkeypatch.setattr(content_module, "CONTENT_LIMIT", 100)

  result = process_document(archive, key, force=True, steps=["convert"])

  assert "ocr.md" in converter.calls[0]["protected"]
  assert "text.md" not in converter.calls[0]["protected"]
  assert source_body(archive, key) == b"A" * 20
  assert source_body(archive, key, "ocr") == b"B" * 20
  assert result["nodes"][""]["attempts"]["convert"]["retained"] is True
  assert result["nodes"][""]["attempts"]["convert"]["retry"] is True


def test_candidate_cleanup_does_not_delete_preserved_outputs(
  archive, converter
):
  key = add_document(archive)
  process_document(archive, key)
  converter.failures["convert"] = Failure("failed")
  spec, message = run_job(archive, key, force=True, steps=["convert"])
  staged = archive.path(spec["work"] + "/new")

  assert (staged / "text.md").is_file()
  assert (staged / "ocr.md").is_file()
  assert b"Converted source" in (staged / "text.md").read_bytes()
  validate_result_data(message["result"], spec["document"])


def test_rediscovery_does_not_silently_drop_previous_extracted_members(
  archive, converter, monkeypatch
):
  key = add_document(
    archive,
    name="project.zip",
    raw=zip_bytes([("source.py", b"print(1)\n")]),
  )
  process_document(archive, key)
  previous = copy.deepcopy(archive.data["documents"][key]["result"])
  monkeypatch.setattr(ContentWalker, "walk", lambda *args, **kwargs: None)

  with pytest.raises(Failure, match="rediscovery omitted"):
    run_job(archive, key, force=True)

  assert archive.data["documents"][key]["result"] == previous


def test_damaged_extracted_original_is_rediscovered_from_downloaded_source(
  archive, converter
):
  key = add_document(
    archive,
    name="project.zip",
    raw=zip_bytes([("source.py", b"print(1)\n")]),
  )
  result = process_document(archive, key)
  child = result["nodes"][""]["children"][0]
  doc = archive.data["documents"][key]
  relative = doc["folder"] + "/" + child + "/source.py"
  archive.write(relative, b"print(2)\n")

  process_document(archive, key)

  assert archive.read(relative, 100) == b"print(1)\n"


def test_source_change_before_job_is_a_source_problem(archive):
  key = add_document(archive, name="source.txt", raw=b"original")
  spec = job_spec(archive, key)
  doc = archive.data["documents"][key]
  archive.write(doc["folder"] + "/source.txt", b"modified")

  with pytest.raises(SourceProblem, match="during processing"):
    DocumentJob(spec, Engines()).run()


def test_source_change_during_processing_prevents_result_completion(
  archive, converter, monkeypatch
):
  key = add_document(archive)
  spec = job_spec(archive, key)
  original_materialize = DocumentJob.materialize

  def change(self):
    files = original_materialize(self)
    archive.write(
      self.doc["folder"] + "/" + self.doc["original"],
      b"<p>Changed</p>",
    )
    return files

  monkeypatch.setattr(DocumentJob, "materialize", change)

  with pytest.raises(SourceProblem, match="during processing"):
    DocumentJob(spec, Engines()).run()

  assert archive.data["documents"][key]["result"] is None


def test_storage_failure_does_not_become_a_partial_product(archive, converter):
  key = add_document(archive)
  converter.failures["convert"] = StorageFailure("disk failed")

  with pytest.raises(StorageFailure, match="disk failed"):
    run_job(archive, key)

  assert archive.data["documents"][key]["result"] is None


def test_original_and_source_relations_are_unchanged_by_processing(
  archive, converter
):
  key = add_document(archive, attached=False)
  before = copy.deepcopy(archive.data["items"][OWNER])

  process_document(archive, key, force=True)

  assert archive.data["items"][OWNER] == before
  assert archive.document_keys([COURSE]) == []
  assert archive.course_for_document(key) == COURSE


def test_excluded_submission_stays_excluded_after_forced_processing(
  archive, converter
):
  key = add_document(archive, role="submission")
  archive.item(COURSE, {"include_submissions": False}, None)

  process_document(archive, key, force=True)

  assert archive.data["documents"][key]["role"] == "submission"
  assert archive.data["items"][OWNER]["documents"] == [key]
  assert archive.document_keys([COURSE]) == []


def test_materialization_does_not_duplicate_old_product_headers(
  archive, converter
):
  key = add_document(archive)
  process_document(archive, key)
  process_document(archive, key)
  process_document(archive, key)

  doc = archive.data["documents"][key]
  text = archive.read(doc["folder"] + "/text.md", 4096).decode()
  assert text.count("[Original]") == 1
  assert text.count("Converted source") == 1
  assert source_body(archive, key) == b"Converted source\n"


def test_empty_ocr_result_has_no_manufactured_markdown(archive, converter):
  converter.bodies["ocr"] = b""
  key = add_document(archive)
  result = process_document(archive, key)
  product = result["nodes"][""]["products"]["ocr"]

  assert product["status"] == "complete"
  assert product["markdown"] is None
  assert product["content_bytes"] == 0
  assert product["content_sha256"] == EMPTY_SHA256
  assert "ocr.md" not in result["files"]


def test_copy_blob_omits_old_prefix_and_preserves_the_rest(private_dir):
  source = private_dir / "old.md"
  target = private_dir / "new.md"
  write_new(source, b"OLD\nsource\nappendix\n")

  _copy_blob(Blob(source, source.stat().st_size, 4), target, b"NEW\n")

  assert target.read_bytes() == b"NEW\nsource\nappendix\n"


def test_copy_blob_rejects_wrong_expected_identity(private_dir):
  source = private_dir / "original"
  write_new(source, b"content")

  with pytest.raises(Failure, match="identity mismatch"):
    _copy_blob(
      Blob(source, 7),
      private_dir / "copy",
      expected=digest(b"different"),
    )


def test_copy_blob_rejects_a_changed_dependency_size(private_dir):
  source = private_dir / "original"
  write_new(source, b"content")

  with pytest.raises(Failure, match="size changed"):
    _copy_blob(Blob(source, 6), private_dir / "copy")


@pytest.mark.parametrize(
  ("field", "value"),
  [
    ("root", "relative"),
    ("work", "../outside"),
    ("steps", []),
    ("steps", ["convert", "convert"]),
    ("steps", ["unknown"]),
    ("force", 1),
    ("settings", {}),
  ],
)
def test_job_specification_is_checked_before_staging(archive, field, value):
  key = add_document(archive, name="source.txt", raw=b"source")
  spec = job_spec(archive, key)
  spec[field] = value

  with pytest.raises(Failure):
    DocumentJob(spec, Engines())


def test_processor_enqueues_each_document_once(archive, scheduler):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=2, timeout=30)

  processor.enqueue(key)
  processor.enqueue(key)

  assert list(processor.ready) == [key]
  assert processor.busy()
  assert scheduler[0].target is worker_main


def test_processor_rejects_unknown_documents(archive, scheduler):
  processor = Processor(archive, force=False, jobs=1, timeout=30)

  with pytest.raises(Failure, match="unknown processing document"):
    processor.enqueue("missing")


def test_planning_writes_a_private_bounded_job_specification(archive, scheduler):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=True, jobs=1, timeout=30)
  archive.processing_failure(key, ["ocr"], "previous timeout")

  spec = processor.plan(key)
  raw = archive.read(spec["work"] + "/job.json", 1024 * 1024)

  assert parse_json(raw) == spec
  assert spec["steps"] == ["convert", "ocr"]
  assert spec["retry_steps"] == ["ocr"]
  assert spec["force"] is True
  assert archive.path(spec["work"] + "/new").is_dir()
  assert "password" not in raw.decode().lower()


def test_planning_refreshes_verified_signature_without_changing_content(
  archive, scheduler
):
  key = add_document(archive, name="source.txt", raw=b"source")
  doc = archive.data["documents"][key]
  path = archive.path(doc["folder"] + "/" + doc["original"])
  os.utime(path, ns=(1_000_000_000, 1_000_000_000))
  processor = Processor(archive, force=False, jobs=1, timeout=30)

  spec = processor.plan(key)

  assert spec["signature"] == signature(path)
  assert doc["signature"] == spec["signature"]
  assert archive.dirty


def test_missing_original_records_processing_failure_without_submitting(
  archive, scheduler
):
  key = add_document(archive, name="source.txt", raw=b"source")
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/" + doc["original"])
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)

  processor.tick()

  assert not processor.busy()
  assert processor.failed == 1
  assert scheduler[0].active == []
  assert set(doc["attempts"]) == {"convert", "ocr"}


def test_unchanged_completed_root_is_reused_without_a_worker(
  archive, scheduler, converter
):
  key = add_document(archive)
  process_document(archive, key)
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)

  processor.tick()

  assert processor.reused == 1
  assert not processor.busy()
  assert scheduler[0].active == []


def test_scheduler_publishes_valid_completion(archive, scheduler, converter):
  key = add_document(archive)
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  pool = scheduler[0]
  spec = pool.active[0]
  message = DocumentJob(spec, Engines()).run()
  message.update(notes=[], seconds=1.25)
  pool.complete(spec, message)

  processor.tick()

  assert not processor.busy()
  assert processor.completed == 1
  assert processor.failed == 0
  assert processor.worker_seconds == 1.25
  assert source_body(archive, key) == b"Converted source\n"
  assert not archive.exists(spec["work"])


def test_failed_worker_is_recorded_and_workspace_discarded(archive, scheduler):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  pool = scheduler[0]
  spec = pool.active[0]
  pool.complete(spec, error="processing timed out after 30s")

  processor.tick()

  assert processor.failed == 1
  assert processor.completed == 0
  assert not archive.exists(spec["work"])
  assert "timed out" in archive.data["documents"][key]["attempts"]["ocr"]["error"]


def test_missing_worker_completion_file_is_a_root_failure(archive, scheduler):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]
  scheduler[0].complete(spec)

  processor.tick()

  assert processor.failed == 1
  assert not archive.exists(spec["work"])
  assert "completion record missing" in (
    archive.data["documents"][key]["attempts"]["convert"]["error"]
  )


def test_malformed_worker_json_is_not_published(archive, scheduler):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]
  archive.write(spec["work"] + "/result.json", b"not JSON")
  scheduler[0].complete(spec)

  processor.tick()

  assert processor.failed == 1
  assert archive.data["documents"][key]["result"] is None
  assert not archive.exists(spec["work"])


def test_worker_storage_failure_stops_instead_of_discarding_as_decode_error(
  archive, scheduler
):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]
  scheduler[0].complete(spec, {
    "result": None,
    "error": None,
    "fatal": "disk failed",
    "notes": [],
    "seconds": 0,
  })

  with pytest.raises(StorageFailure, match="disk failed"):
    processor.tick()

  assert archive.exists(spec["work"])
  assert archive.data["documents"][key]["result"] is None


def test_bad_worker_product_does_not_replace_previous_result(
  archive, scheduler, converter
):
  key = add_document(archive)
  previous = process_document(archive, key)
  processor = Processor(archive, force=True, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]
  message = DocumentJob(spec, Engines()).run()
  message.update(notes=[], seconds=0)
  relative = spec["work"] + "/new/text.md"
  raw = archive.read(relative, 4096)
  archive.write(relative, raw.replace(b"Converted", b"Corrupted"))
  scheduler[0].complete(spec, message)

  processor.tick()

  assert processor.failed == 1
  assert archive.data["documents"][key]["result"] == previous
  assert source_body(archive, key) == b"Converted source\n"


def test_root_result_limitations_affect_summary(archive, scheduler, converter):
  from kctl.core import LimitReached

  key = add_document(archive)
  converter.failures["convert"] = LimitReached("fixed limit")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]
  message = DocumentJob(spec, Engines()).run()
  message.update(notes=[], seconds=0.5)
  scheduler[0].complete(spec, message)

  processor.tick()

  assert processor.completed == 1
  assert processor.failed == 0
  assert processor.limited == 1


def test_retryable_product_failure_is_counted_as_failed_root(
  archive, scheduler, converter
):
  key = add_document(archive)
  converter.failures["convert"] = Failure("decoder failed")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]
  message = DocumentJob(spec, Engines()).run()
  message.update(notes=[], seconds=0)
  scheduler[0].complete(spec, message)

  processor.tick()

  assert processor.completed == 1
  assert processor.failed == 1
  assert processor.limited == 0


def test_close_records_abandoned_jobs_without_removing_previous_products(
  archive, scheduler, converter
):
  key = add_document(archive)
  process_document(archive, key)
  processor = Processor(archive, force=True, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  spec = scheduler[0].active[0]

  processor.close("interrupted before publication")

  assert "interrupted" in (
    archive.data["documents"][key]["attempts"]["convert"]["error"]
  )
  assert source_body(archive, key) == b"Converted source\n"
  assert archive.exists(spec["work"])
  assert scheduler[0].active == []


def test_close_does_not_mutate_records_when_publication_is_pending(
  archive, scheduler
):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)
  processor.tick()
  archive.write(".ctl/pending.json", b"pending")

  processor.close()

  assert archive.data["documents"][key]["attempts"] == {}
  assert scheduler[0].active == []


def test_progress_and_summary_are_small_and_explicit(
  archive, scheduler, capsys
):
  key = add_document(archive, name="source.txt", raw=b"source")
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  processor.enqueue(key)

  processor.progress()

  assert "0 running, 1 queued" in capsys.readouterr().err
  assert processor.summary() == {
    "processed_roots": 0,
    "reused_roots": 0,
    "failed_roots": 0,
    "limited_roots": 0,
    "worker_seconds": 0.0,
  }


def test_completed_findings_are_counted_without_repeating_paths(
  archive, scheduler, capsys
):
  processor = Processor(archive, force=False, jobs=1, timeout=30)
  node = {
    "name": "source.pdf", "children": [], "retry": False,
    "problems": [f"Coverage finding {index}" for index in range(10)],
    "products": {}, "attempts": {},
  }
  spec = {
    "document": {"folder": "documents/Course/source"},
    "steps": ["convert"],
  }
  message = {
    "result": {"nodes": {"": node}}, "seconds": 1.0,
    "notes": ["decoder\n\x1b warning" + "x" * 400],
  }

  processor._completed(spec, message)

  report = capsys.readouterr().err
  assert report.count("documents/Course/source") == 1
  assert "10 findings" in report
  assert "after successful export" in report
  assert "Coverage finding" not in report
  assert "\x1b" not in report
  assert len(report.splitlines()) == 3
  assert processor.summary()["limited_roots"] == 1
  assert processor.summary()["failed_roots"] == 0


def wait_for_completion(pool):
  deadline = time.monotonic() + 30

  while time.monotonic() < deadline:
    completed = list(pool.collect())
    if completed:
      return completed
    time.sleep(0.02)

  pytest.fail("worker did not complete")


def test_real_spawned_worker_processes_text_container_without_ocr_model(archive):
  key = add_document(
    archive,
    name="project.zip",
    raw=zip_bytes([
      ("src/main.py", b"print('hello')\n"),
      ("README.md", b"# Source\n"),
    ]),
  )
  spec = job_spec(archive, key)
  pool = Workers(1, 30, target=worker_main)

  try:
    pool.submit(spec)
    assert wait_for_completion(pool) == [(spec, None)]
    message = parse_json(
      archive.read(spec["work"] + "/result.json", 16 * 1024 * 1024)
    )
    assert message["error"] is None
    assert message["fatal"] is None
    assert message["seconds"] >= 0
    assert message["notes"] == []
    assert len(message["result"]["nodes"]) == 3
    archive.publish(spec, message)
  finally:
    pool.close()

  assert archive.data["documents"][key]["result"] is not None


def test_real_spawned_worker_reports_invalid_job_without_echoing_raw_input(
  archive
):
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/job.json", b"private invalid JSON")
  spec = {"root": str(archive.root), "work": work}
  pool = Workers(1, 30, target=worker_main)

  try:
    pool.submit(spec)
    assert wait_for_completion(pool) == [(spec, None)]
    message = parse_json(archive.read(work + "/result.json", 4096))
  finally:
    pool.close()

  assert message["result"] is None
  assert message["error"] == "malformed JSON"
  assert message["fatal"] is None
  assert "private" not in json_bytes(message).decode()


def test_real_scheduler_completes_and_reuses_passthrough_document(archive):
  key = add_document(archive, name="source.py", raw=b"print('source')\n")
  processor = Processor(archive, force=False, jobs=1, timeout=30)

  try:
    processor.enqueue(key)
    deadline = time.monotonic() + 30

    while processor.busy():
      processor.tick()
      if time.monotonic() >= deadline:
        pytest.fail("processing did not complete")
      time.sleep(0.02)
  finally:
    processor.close()

  assert processor.completed == 1
  assert processor.failed == 0
  assert archive.data["documents"][key]["result"]["files"] == {}

  second = Processor(archive, force=False, jobs=1, timeout=30)
  try:
    second.enqueue(key)
    second.tick()
    assert second.reused == 1
    assert second.workers.workers == []
  finally:
    second.close()


def recoverable_presentation():
  from PIL import Image
  from pptx import Presentation
  from pptx.util import Inches

  presentation = Presentation()
  for number, title in enumerate(("First lesson", "Second lesson", "Third lesson")):
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    textbox = slide.shapes.add_textbox(
      Inches(1), Inches(1), Inches(5), Inches(1)
    )
    textbox.text_frame.text = title

    if number:
      image_data = io.BytesIO()
      with Image.new("RGB", (12, 10), "red" if number == 1 else "blue") as image:
        image.putpixel((0, 0), (0, 0, 0))
        image.save(image_data, format="PNG")
      slide.shapes.add_picture(image_data, Inches(1), Inches(3))

    if number == 1:
      table = slide.shapes.add_table(
        1, 2, Inches(1), Inches(2), Inches(5), Inches(1)
      ).table
      table.cell(0, 0).text = "Deadline"
      table.cell(0, 1).text = "Friday"

  original = io.BytesIO()
  presentation.save(original)
  packed = io.BytesIO()

  with zipfile.ZipFile(original) as source:
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_STORED) as target:
      for member in source.infolist():
        target.writestr(member.filename, source.read(member))
      damaged = target.getinfo("ppt/media/image1.png")

  raw = bytearray(packed.getvalue())
  start = damaged.header_offset
  name_size = int.from_bytes(raw[start + 26:start + 28], "little")
  extra_size = int.from_bytes(raw[start + 28:start + 30], "little")
  raw[start + 30 + name_size + extra_size] ^= 1
  return bytes(raw)


def test_real_pptx_recovery_replaces_failed_text_and_survives_export(
  archive, converter, monkeypatch
):
  raw = recoverable_presentation()
  key = add_document(archive, name="lecture.pptx", raw=raw)
  converter.failures = {
    "convert": Failure("previous native conversion failed"),
    "ocr": Failure("previous image processing failed"),
  }
  previous = process_document(archive, key)
  assert previous["nodes"][""]["products"]["convert"]["markdown"] is None

  recognized = []

  def recognize(self, image):
    recognized.append(image.size)
    return "Healthy image transcript"

  monkeypatch.setattr(processing, "FileProcessor", FileProcessor)
  monkeypatch.setattr(Engines, "recognize", recognize)
  result = process_document(archive, key, force=True)
  doc = archive.data["documents"][key]
  node = result["nodes"][""]
  text = source_body(archive, key).decode()

  assert text.index("First lesson") < text.index("Second lesson")
  assert text.index("Second lesson") < text.index("Third lesson")
  assert "Deadline | Friday" in text
  assert node["products"]["convert"]["status"] == "partial"
  assert node["products"]["convert"]["retry"] is False
  assert node["attempts"]["convert"]["retained"] is False
  assert node["products"]["ocr"]["retry"] is False
  assert source_body(archive, key, "ocr") == (
    b"## Embedded image: ppt/media/image2.png\n\nHealthy image transcript\n"
  )
  assert recognized == [(12, 10)]
  assert archive.read(doc["folder"] + "/lecture.pptx", len(raw)) == raw
  assert archive.data["items"][OWNER]["documents"] == [key]
  assert archive.reusable(doc, ["convert", "ocr"])
  archive.validate()

  assert export_courses(archive, [COURSE]) == 3
  exported = archive.read("extracted/Course.txt", 65536).decode()
  footer = exported.rsplit("## Extraction notes\n", 1)[1]
  assert "First lesson" in exported
  assert "Deadline | Friday" in exported
  assert "Healthy image transcript" in exported
  assert footer.startswith("\nGenerated by kctl\nSources with notes: 1\n")
  assert "PPTX fallback" in footer
  assert "ppt/media/image1.png" in footer
  assert "Bad CRC-32" in footer

  processor = Processor(archive, force=False, jobs=1, timeout=30)
  try:
    processor.enqueue(key)
    processor.tick()
    assert not processor.busy()
    assert processor.reused == 1
    assert processor.limited == 1
    assert processor.failed == 0
    assert processor.workers.workers == []
  finally:
    processor.close()

  assert recognized == [(12, 10)]
  process_document(archive, key, force=True)
  assert recognized == [(12, 10), (12, 10)]
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  assert archive.data["items"][OWNER]["documents"] == [key]


def quiz_pdf(marked):
  text = b"Quiz source: explain the safety properties of the example system."
  stream = b"BT /F1 10 Tf 20 150 Td (" + text + b") Tj ET"
  annotations = b" /Annots [6 0 R]" if marked else b""
  objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    (
      b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200]"
      b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R"
      + annotations + b" >>"
    ),
    (
      f"<< /Length {len(stream)} >>\nstream\n".encode()
      + stream + b"\nendstream"
    ),
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ]
  if marked:
    objects.append(
      b"<< /Type /Annot /Subtype /Highlight /Rect [20 140 300 165]"
      b" /QuadPoints [20 165 300 165 20 140 300 140] /C [1 1 0] /F 4 >>"
    )

  raw = bytearray(b"%PDF-1.7\n")
  offsets = [0]
  for number, value in enumerate(objects, 1):
    offsets.append(len(raw))
    raw.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")

  xref = len(raw)
  raw.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
  for offset in offsets[1:]:
    raw.extend(f"{offset:010d} 00000 n \n".encode())
  raw.extend(
    f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
    f"startxref\n{xref}\n%%EOF\n".encode()
  )
  return bytes(raw)


def test_real_pdf_annotation_findings_survive_retention_and_body_deduplication(
  archive, monkeypatch
):
  blank_raw = quiz_pdf(False)
  marked_raw = quiz_pdf(True)
  blank = add_document(archive, name="blank.pdf", raw=blank_raw)
  marked = add_document(
    archive, name="submission.pdf", raw=marked_raw, role="submission"
  )
  process_document(archive, blank)
  current = copy.deepcopy(archive.settings["convert"])
  archive.settings["convert"].pop("pdf_coverage")

  with monkeypatch.context() as patch:
    patch.setattr(conversion_module, "_annotations", lambda page: set())
    process_document(archive, marked)

  previous_body = source_body(archive, marked)
  assert previous_body == source_body(archive, blank)
  archive.settings["convert"] = current

  result = process_document(archive, marked)
  node = result["nodes"][""]
  attempt = node["attempts"]["convert"]

  assert source_body(archive, marked) == previous_body
  assert node["products"]["convert"]["status"] == "complete"
  assert attempt["status"] == "partial"
  assert attempt["retained"] is True
  assert attempt["retry"] is False
  assert any("Highlight" in note for note in attempt["summary"]["coverage"])
  assert archive.reusable(archive.data["documents"][marked], ["convert", "ocr"])
  archive.validate()

  assert export_courses(archive, [COURSE]) == 3
  exported = archive.read("extracted/Course.txt", 16384).decode()
  footer = exported.rsplit("## Extraction notes\n", 1)[1]
  assert exported.count("Quiz source:") == 1
  assert "## Sources" in exported
  assert footer.startswith("\nGenerated by kctl\nSources with notes: 1\n")
  assert "submission.pdf" in footer
  assert "blank.pdf" not in footer
  assert "Highlight" in footer
  assert "Previous result retained" in footer
  assert "selected answer" not in footer.lower()

  for key, raw in ((blank, blank_raw), (marked, marked_raw)):
    doc = archive.data["documents"][key]
    assert archive.read(
      doc["folder"] + "/" + doc["original"], len(raw)
    ) == raw

  assert archive.data["items"][OWNER]["documents"] == [blank, marked]
  assert not archive.exists(".ctl/pending.json")
  assert list(archive.path(".ctl/work").iterdir()) == []

  archive.item(COURSE, {"include_submissions": False}, None)
  assert export_courses(archive, [COURSE]) == 0
  excluded = archive.read("extracted/Course.txt", 16384).decode()
  assert "submission.pdf" not in excluded
  assert "Highlight" not in excluded
  assert excluded.endswith(
    "## Extraction notes\n\nGenerated by kctl\nSources with notes: 0\n"
  )


def corrupt_docx():
  from docx import Document
  from docx.shared import Inches
  from PIL import Image

  document = Document()
  document.add_paragraph("Readable text before the images")

  for color in ("red", "blue"):
    data = io.BytesIO()
    with Image.new("RGB", (12, 10), color) as image:
      image.putpixel((0, 0), (0, 0, 0))
      image.save(data, format="PNG")
    document.add_picture(data, width=Inches(1))

  document.add_paragraph("Readable text after the images")
  original = io.BytesIO()
  document.save(original)
  packed = io.BytesIO()

  with zipfile.ZipFile(original) as source:
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_STORED) as target:
      for member in source.infolist():
        target.writestr(member.filename, source.read(member))
      member = target.getinfo("word/media/image1.png")

  raw = bytearray(packed.getvalue())
  start = member.header_offset
  name_size = int.from_bytes(raw[start + 26:start + 28], "little")
  extra_size = int.from_bytes(raw[start + 28:start + 30], "little")
  raw[start + 30 + name_size + extra_size] ^= 1
  return bytes(raw)


def test_real_docx_integrity_correction_is_published_and_reused(
  archive, monkeypatch
):
  raw = corrupt_docx()
  key = add_document(archive, name="reading.docx", raw=raw)
  current = copy.deepcopy(archive.settings["convert"])
  archive.settings["convert"].pop("office_integrity")
  calls = []
  original_markdown = Engines.markdown

  def markdown(self, path, kind, raw=None):
    calls.append(kind)
    return original_markdown(self, path, kind, raw)

  monkeypatch.setattr(Engines, "markdown", markdown)
  monkeypatch.setattr(
    Engines, "recognize", lambda self, image: "Healthy image transcript"
  )

  with monkeypatch.context() as patch:
    patch.setattr(FileProcessor, "office_failure", lambda self, error: self.issue(error))
    previous = process_document(archive, key)

  previous_node = previous["nodes"][""]
  assert previous_node["products"]["convert"]["retry"] is True
  assert previous_node["products"]["convert"]["content_bytes"] == 0
  previous_ocr = source_body(archive, key, "ocr")
  assert b"Healthy image transcript" in previous_ocr
  assert calls == [".docx"]

  archive.settings["convert"] = current
  result = process_document(archive, key)
  node = result["nodes"][""]
  attempt = node["attempts"]["convert"]
  doc = archive.data["documents"][key]

  assert calls == [".docx", ".docx"]
  assert attempt["settings"] == current
  assert attempt["status"] == "partial"
  assert attempt["retry"] is False
  assert attempt["retained"] is True
  assert any(
    "Native Office text unavailable after a verified ZIP integrity failure"
    in problem
    for problem in attempt["problems"]
  )
  assert source_body(archive, key) == b""
  assert source_body(archive, key, "ocr") == previous_ocr
  assert archive.reusable(doc, ["convert", "ocr"])
  assert archive.read(doc["folder"] + "/reading.docx", len(raw)) == raw
  assert archive.data["items"][OWNER]["documents"] == [key]
  archive.validate()

  assert export_courses(archive, [COURSE]) == 3
  exported = archive.read("extracted/Course.txt", 32768).decode()
  footer = exported.rsplit("## Extraction notes\n", 1)[1]
  assert "Healthy image transcript" in exported
  assert footer.startswith("\nGenerated by kctl\nSources with notes: 1\n")
  assert "word/media/image1.png" in footer
  assert "verified ZIP integrity failure" in footer

  processor = Processor(archive, force=False, jobs=1, timeout=30)
  try:
    processor.enqueue(key)
    processor.tick()
    assert not processor.busy()
    assert processor.reused == 1
    assert processor.limited == 1
    assert processor.failed == 0
    assert processor.workers.workers == []
  finally:
    processor.close()

  assert calls == [".docx", ".docx"]
  assert export_courses(archive, [COURSE]) == 3
  assert archive.read("extracted/Course.txt", 32768).decode() == exported

  forced = process_document(archive, key, force=True)
  assert calls == [".docx", ".docx", ".docx"]
  assert forced["nodes"][""]["attempts"]["convert"]["retry"] is False
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  assert archive.data["items"][OWNER]["documents"] == [key]


def test_unrelated_native_failure_remains_retryable_after_publication(
  archive, monkeypatch
):
  raw = corrupt_docx()
  key = add_document(archive, name="reading.docx", raw=raw)
  calls = []

  def fail(self, path, kind, raw=None):
    calls.append(kind)
    raise ValueError("independent native conversion failure")

  monkeypatch.setattr(Engines, "markdown", fail)
  monkeypatch.setattr(
    Engines, "recognize", lambda self, image: "Healthy image transcript"
  )

  for _ in range(2):
    result = process_document(archive, key)
    node = result["nodes"][""]
    assert node["attempts"]["convert"]["retry"] is True
    assert "unexpected ValueError" in node["attempts"]["convert"]["problems"]
    assert node["products"]["ocr"]["retry"] is False
    assert not archive.reusable(
      archive.data["documents"][key], ["convert", "ocr"]
    )

  assert calls == [".docx", ".docx"]
  assert node["attempts"]["convert"]["retained"] is True
  assert export_courses(archive, [COURSE]) == 3
  text = archive.read("extracted/Course.txt", 32768).decode()
  assert "Healthy image transcript" in text
  assert "unexpected ValueError" in text
  assert "Bad CRC-32" in text
  doc = archive.data["documents"][key]
  assert archive.read(doc["folder"] + "/reading.docx", len(raw)) == raw
  assert archive.data["items"][OWNER]["documents"] == [key]
  archive.validate()
