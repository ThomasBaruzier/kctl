import copy
import errno
import io
import os
import queue
import threading
import time
import zipfile

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import kctl.processing as processing
import kctl.storage as storage
import kctl.views as views
import kctl.workflows as workflows

from kctl.archive import Archive
from kctl.auth_html import soup_html
from kctl.content import ContentWalker, Output
from kctl.conversion import Engines, FileProcessor
from kctl.core import (
  CTL,
  TEXT_LIMIT,
  Cancelled,
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  digest,
  identity,
  join_path,
)
from kctl.courses import MENUS, CourseTraversal
from kctl.exports import export_courses
from kctl.processing import DocumentJob, Processor
from kctl.reader import Home
from kctl.records import (
  COVERAGE_OVERFLOW,
  error_outcome,
  new_manifest,
  outcome,
  validate_result_data,
)
from kctl.retrieval import Producer
from kctl.storage import (
  Tree,
  private_input,
  signature,
  staging_output,
  write_new,
)


COURSE = "course/one"
OWNER = COURSE + "/page"
FOLDER = "documents/Course"
PAGE = FOLDER + "/page.md"


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
    "title": "Source",
    "path": PAGE,
    "folder": FOLDER,
    "status": "archived",
    "parent": COURSE,
    "documents": [],
    "related": [],
    "problems": [],
  }, "Good source\n")
  value.flush_views()

  try:
    yield value
  finally:
    value.close()


@contextmanager
def broken_descriptor(stream):
  descriptor = stream.fileno()
  saved = os.dup(descriptor)

  try:
    os.close(descriptor)
    yield
  finally:
    try:
      os.dup2(saved, descriptor, inheritable=False)
    finally:
      os.close(saved)


def corrupt_page(archive):
  raw = archive.read(PAGE, 4096).replace(b"Good source", b"Evil source")
  archive.write(PAGE, raw)
  return raw


def enlarge_page(archive):
  with archive.path(PAGE).open("r+b") as stream:
    stream.truncate(TEXT_LIMIT + 1)
  archive.mark(OWNER)


def damaged_zip():
  stream = io.BytesIO()

  with zipfile.ZipFile(stream, "w", zipfile.ZIP_BZIP2) as container:
    container.writestr("bad.txt", b"Corrupt this member\n")
    container.writestr("good.txt", b"Keep this member\n")

  raw = bytearray(stream.getvalue())
  name_size = int.from_bytes(raw[26:28], "little")
  extra_size = int.from_bytes(raw[28:30], "little")
  start = 30 + name_size + extra_size
  assert raw[start:start + 3] == b"BZh"
  raw[start] = 0
  return bytes(raw)


def add_document(archive, raw, name="source.zip"):
  url = CTL + "/fileDownServlet?id=document"
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
    "result": None,
    "attempts": {},
  }
  archive.document(key, record, work)
  archive.item(OWNER, {"documents": [key]}, None)
  return key


def run_document(archive, key, *, force=False):
  doc = copy.deepcopy(archive.data["documents"][key])
  work = archive.new_work()
  archive.mkdir(work + "/new")
  spec = {
    "key": key,
    "root": str(archive.root),
    "document": doc,
    "signature": copy.deepcopy(doc["signature"]),
    "settings": copy.deepcopy(archive.settings),
    "steps": ["convert", "ocr"],
    "retry_steps": [],
    "force": force,
    "work": work,
  }
  engines = Engines()

  try:
    message = DocumentJob(spec, engines).run()
  finally:
    engines.close()

  message.update(notes=[], seconds=0)
  archive.publish(spec, message)
  return archive.data["documents"][key]["result"]


def converted_body(archive, key):
  doc = archive.data["documents"][key]
  product = doc["result"]["nodes"][""]["products"]["convert"]
  if product["markdown"] is None:
    return b""

  return archive.read_content(
    doc["folder"] + "/" + product["markdown"],
    product,
    size=doc["result"]["files"][product["markdown"]],
  )


def generated_text(archive, key, filename):
  doc = archive.data["documents"][key]
  return archive.read(doc["folder"] + "/" + filename, TEXT_LIMIT).decode()


def conversion_processor(archive, kind, raw, step="convert"):
  work = archive.new_work()
  name = "source" + kind
  archive.write(work + "/" + name, raw)
  output = Output(archive.mkdir(work + "/candidate"))
  node = {
    "name": name,
    "original": name,
    "kind": kind,
    "bytes": len(raw),
    "sha256": digest(raw),
  }
  return FileProcessor(
    archive.path(work + "/" + name),
    output,
    "",
    node,
    step,
    archive.settings,
    Engines(),
  )


def office_bytes():
  stream = io.BytesIO()

  with zipfile.ZipFile(stream, "w") as container:
    container.writestr("[Content_Types].xml", "<Types/>")
    container.writestr("word/document.xml", "<document/>")

  return stream.getvalue()


@pytest.fixture
def products(monkeypatch):
  state = SimpleNamespace(
    body=b"Previous source text\n",
    gallery=False,
    problems=[],
    coverage=[],
    error=None,
    calls=[],
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
      self.settings = settings[step]

    def run(self):
      state.calls.append(self.step)
      if self.step == "ocr":
        return outcome(self.settings), None
      if state.error is not None:
        return error_outcome(self.settings, state.error), None

      raw = state.body
      if state.gallery:
        raw += b"\n## Extracted images\n"

      markdown = None
      if raw:
        markdown = join_path(self.folder, "text.md")
        self.output.write_text(markdown, raw)

      summary = {"coverage": list(state.coverage)} if state.coverage else {}
      return outcome(
        self.settings,
        outputs=set(self.output.claimed),
        markdown=markdown,
        problems=list(state.problems),
        summary=summary,
        content_bytes=len(state.body),
        content_sha256=digest(state.body),
      ), None

  monkeypatch.setattr(processing, "FileProcessor", Converter)
  return state


def test_staging_does_not_translate_errors_from_its_caller(private_dir):
  path = private_dir / "partial"
  error = OSError("invalid compressed data")

  with pytest.raises(OSError) as captured:
    with staging_output(path) as stream:
      stream.write(b"partial")
      raise error

  assert captured.value is error
  assert path.read_bytes() == b"partial"


def test_private_input_does_not_translate_errors_from_its_caller(private_file):
  path = private_file("source", b"data")
  error = OSError("decoder failed")

  with pytest.raises(OSError) as captured:
    with private_input(path) as stream:
      assert stream.read() == b"data"
      raise error

  assert captured.value is error


def test_actual_staged_write_error_is_a_storage_failure(private_dir):
  with staging_output(private_dir / "output") as stream:
    with broken_descriptor(stream):
      with pytest.raises(StorageFailure, match="I/O failed"):
        stream.write(b"x" * 65536)

    stream.write(b"restored")


def test_actual_private_read_error_is_a_storage_failure(private_file):
  path = private_file("source", b"data")

  with private_input(path) as stream:
    with broken_descriptor(stream):
      with pytest.raises(StorageFailure, match="I/O failed"):
        stream.read(1)

    assert stream.read() == b"data"


def test_private_input_close_error_is_a_storage_failure(private_file):
  path = private_file("source", b"data")

  with pytest.raises(StorageFailure, match="I/O failed"):
    with private_input(path) as stream:
      os.close(stream.fileno())


def test_staged_sync_error_is_still_fatal(private_dir, monkeypatch):
  def fail(descriptor):
    raise OSError(errno.EIO, "sync failed")

  monkeypatch.setattr(storage.os, "fsync", fail)

  with pytest.raises(StorageFailure, match="I/O failed"):
    with staging_output(private_dir / "output") as stream:
      stream.write(b"data")


def test_corrupt_bzip_member_does_not_hide_a_valid_sibling(private_dir):
  raw = damaged_zip()
  path = private_dir / "source.zip"
  write_new(path, raw)
  output = Output(private_dir)
  walker = ContentWalker(output)
  walker.node("", "source.zip", "source.zip", len(raw), digest(raw))

  walker.walk("")

  root = walker.nodes[""]
  assert root["retry"] is True
  assert any("bad.txt" in problem for problem in root["problems"])
  assert len(root["children"]) == 1
  folder = root["children"][0]
  assert walker.nodes[folder]["name"] == "good.txt"
  assert output.resolve(folder + "/good.txt").path.read_bytes() == (
    b"Keep this member\n"
  )


def test_corrupt_member_remains_a_document_limitation_through_publication(archive):
  key = add_document(archive, damaged_zip())

  result = run_document(archive, key)

  validate_result_data(result, archive.data["documents"][key])
  assert result["nodes"][""]["problems"]
  assert any(
    node["name"] == "good.txt"
    for node in result["nodes"].values()
  )


@pytest.mark.parametrize("kind", [".html", ".png", ".docx", ".pdf"])
def test_conversion_source_open_failure_is_fatal(archive, monkeypatch, kind):
  processor = conversion_processor(archive, kind, b"source")
  original_open = storage.os.open

  def fail(path, *args, **kwargs):
    if path == processor.source or path == str(processor.source):
      raise OSError(errno.EIO, "source device failure")
    return original_open(path, *args, **kwargs)

  monkeypatch.setattr(storage.os, "open", fail)

  with pytest.raises(StorageFailure, match="I/O failed"):
    processor.run()


def test_conversion_source_metadata_failure_is_fatal(archive, monkeypatch):
  processor = conversion_processor(archive, ".html", b"<p>source</p>")
  original_lstat = Path.lstat

  def fail(path, *args, **kwargs):
    if path == processor.source:
      raise OSError(errno.EIO, "source metadata failure")
    return original_lstat(path, *args, **kwargs)

  monkeypatch.setattr(Path, "lstat", fail)

  with pytest.raises(StorageFailure, match="I/O failed"):
    processor.run()


@pytest.mark.parametrize("wrapped", [False, True])
def test_converter_cannot_hide_a_local_read_failure(archive, wrapped):
  processor = conversion_processor(archive, ".docx", office_bytes())

  def convert(stream, **kwargs):
    with broken_descriptor(stream):
      with pytest.raises(StorageFailure):
        stream.read(1)

    if wrapped:
      raise ValueError("converter replaced the input failure")

    return SimpleNamespace(text_content="Incorrect apparent success")

  processor.engines.converter = SimpleNamespace(convert_stream=convert)

  with pytest.raises(StorageFailure, match="I/O failed"):
    processor.run()


@pytest.mark.parametrize("error", [KeyboardInterrupt(), Cancelled()])
def test_decoder_input_does_not_replace_cancellation(archive, error):
  processor = conversion_processor(archive, ".docx", office_bytes())

  def convert(stream, **kwargs):
    with broken_descriptor(stream):
      with pytest.raises(StorageFailure):
        stream.read(1)

    raise error

  processor.engines.converter = SimpleNamespace(convert_stream=convert)

  with pytest.raises(type(error)) as captured:
    processor.engines.markdown(processor.source, ".docx")

  assert captured.value is error


def test_decoder_os_error_is_not_a_filesystem_failure(archive):
  processor = conversion_processor(archive, ".docx", office_bytes())

  def convert(stream, **kwargs):
    assert stream.read(2) == b"PK"
    raise OSError(errno.EINVAL, "malformed document data")

  processor.engines.converter = SimpleNamespace(convert_stream=convert)
  product, _ = processor.run()

  assert product["status"] == "failed"
  assert product["retry"] is True
  assert product["markdown"] is None


@pytest.mark.parametrize("kind", [".png", ".docx"])
def test_real_malformed_input_remains_a_processing_failure(archive, kind):
  processor = conversion_processor(archive, kind, b"broken")

  product, _ = processor.run()

  assert product["status"] == "failed"
  assert product["retry"] is True
  assert product["markdown"] is None


def test_ocr_image_open_failure_is_fatal(archive, monkeypatch):
  from PIL import Image

  data = io.BytesIO()
  with Image.new("RGB", (8, 8), "white") as image:
    image.putpixel((0, 0), (0, 0, 0))
    image.save(data, format="PNG")

  processor = conversion_processor(archive, ".png", data.getvalue(), "ocr")
  original_open = storage.os.open

  def fail(path, *args, **kwargs):
    if Path(path).parent.name == "images":
      raise OSError(errno.EIO, "cached image failure")
    return original_open(path, *args, **kwargs)

  monkeypatch.setattr(storage.os, "open", fail)

  with pytest.raises(StorageFailure, match="I/O failed"):
    processor.run()


def test_archive_preparation_allows_a_later_source_refresh(archive):
  damaged = corrupt_page(archive)
  root = archive.root
  archive.close()
  reopened = Archive(root, create=False)

  try:
    reopened.prepare()
    assert reopened.read(PAGE, 4096) == damaged
    assert OWNER in reopened.damaged

    reopened.begin("sync")
    reopened.item(OWNER, {
      "status": "archived",
      "message": "",
    }, "Repaired source\n")
    reopened.finish("complete")

    assert OWNER not in reopened.damaged
    assert OWNER not in reopened.incomplete()
    assert reopened.read_content(
      PAGE, reopened.data["items"][OWNER]
    ) == b"Repaired source\n"
    assert export_courses(reopened, [COURSE]) == 0
  finally:
    reopened.close()


def test_damaged_page_does_not_become_exportable_after_view_refresh(archive):
  assert export_courses(archive, [COURSE]) == 0
  previous = archive.read("extracted/Course.txt", 4096)
  corrupt_page(archive)

  archive.prepare()

  assert export_courses(archive, [COURSE]) == 2
  assert archive.read("extracted/Course.txt", 4096) == previous

  with pytest.raises(SourceProblem):
    archive.read_content(PAGE, archive.data["items"][OWNER])


def test_oversized_page_is_not_read_during_view_refresh(archive, monkeypatch):
  enlarge_page(archive)
  original_read = archive.read

  def read(relative, limit):
    assert relative != PAGE, "oversized source must not be read"
    return original_read(relative, limit)

  monkeypatch.setattr(archive, "read", read)

  archive.prepare()

  assert archive.damaged[OWNER] == "stored source page exceeds text limit"
  assert archive.path(PAGE).stat().st_size == TEXT_LIMIT + 1
  assert COURSE in archive.incomplete()


def test_oversized_page_can_be_replaced_without_weakening_export(archive):
  assert export_courses(archive, [COURSE]) == 0
  previous = archive.read("extracted/Course.txt", 4096)
  enlarge_page(archive)

  archive.prepare()

  assert export_courses(archive, [COURSE]) == 2
  assert archive.read("extracted/Course.txt", 4096) == previous

  archive.begin("sync")
  archive.item(OWNER, {"status": "archived"}, "Repaired source\n")
  archive.finish("complete")

  assert OWNER not in archive.damaged
  assert archive.read_content(
    PAGE, archive.data["items"][OWNER]
  ) == b"Repaired source\n"
  assert export_courses(archive, [COURSE]) == 0


def test_oversized_page_backup_remains_recoverable(archive, monkeypatch):
  enlarge_page(archive)
  archive.prepare()
  original_move = archive.move

  def fail(source, target):
    if source.endswith("/new"):
      raise StorageFailure("interrupted installation")
    original_move(source, target)

  monkeypatch.setattr(archive, "move", fail)

  with pytest.raises(StorageFailure, match="interrupted installation"):
    archive.item(OWNER, {"status": "archived"}, "Recovered source\n")

  assert archive.exists(".ctl/pending.json")
  assert not archive.exists(PAGE)
  monkeypatch.setattr(archive, "move", original_move)

  archive.recover()
  archive.flush_views()

  assert not archive.exists(".ctl/pending.json")
  assert OWNER not in archive.damaged
  assert archive.read_content(
    PAGE, archive.data["items"][OWNER]
  ) == b"Recovered source\n"


def test_oversized_new_page_is_still_rejected(archive):
  work = archive.new_work()
  archive.write(work + "/new", b"source")

  with archive.path(work + "/new").open("r+b") as stream:
    stream.truncate(TEXT_LIMIT + 1)

  with pytest.raises(Failure, match="page exceeds size limit"):
    archive.install(
      "items", OWNER, copy.deepcopy(archive.data["items"][OWNER]), work
    )

  assert not archive.exists(".ctl/pending.json")
  assert archive.read_content(
    PAGE, archive.data["items"][OWNER]
  ) == b"Good source\n"


def test_repeated_view_refresh_does_not_repeat_the_same_damage_warning(
  archive, capsys
):
  corrupt_page(archive)
  archive.mark(OWNER)
  capsys.readouterr()

  archive.flush_views()
  first = capsys.readouterr().err
  archive.flush_views()
  second = capsys.readouterr().err

  assert "stored page left unchanged" in first
  assert second == ""
  assert OWNER in archive.dirty_items


def test_unrelated_damaged_page_does_not_block_healthy_course_export(archive):
  archive.item("historical", {
    "title": "Historical",
    "path": "documents/historical.md",
    "folder": "documents",
    "status": "archived",
    "parent": "index",
  }, "Old source\n")
  path = "documents/historical.md"
  archive.write(
    path, archive.read(path, 4096).replace(b"Old source", b"Bad source")
  )

  archive.prepare()

  assert "historical" in archive.damaged
  assert COURSE not in archive.incomplete()
  assert export_courses(archive, [COURSE]) == 0


@pytest.mark.parametrize(
  "error",
  [StorageFailure("disk failed"), ProtectionFailure("unsafe file")],
)
def test_view_batch_does_not_soften_filesystem_failures(
  archive, monkeypatch, error
):
  archive.mark(OWNER)

  def fail(*args):
    raise error

  monkeypatch.setattr(views, "render", fail)

  with pytest.raises(type(error), match=error.message):
    archive.flush_views()

  assert OWNER in archive.dirty_items


def test_initial_manifest_recovers_only_recognized_atomic_remnants(private_dir):
  root = private_dir / "archive"
  tree = Tree(root)

  try:
    tree.mkdir(".ctl")
    tree.write(".ctl/lock", b"")
    tree.write(".ctl/.write-" + "a" * 32, b"unfinished manifest")
  finally:
    tree.close()

  archive = Archive(root, create=True)

  try:
    assert archive.data == new_manifest()
    assert set(path.name for path in archive.path(".ctl").iterdir()) == {
      "lock", "manifest.json",
    }
  finally:
    archive.close()


@pytest.mark.parametrize("extra", [".ctl/notes", "documents/source.txt"])
def test_missing_manifest_with_other_content_is_not_reinitialized(
  private_dir, extra
):
  root = private_dir / "archive"
  temporary = ".ctl/.write-" + "a" * 32
  tree = Tree(root)

  try:
    tree.write(temporary, b"unfinished")
    tree.write(extra, b"keep")
  finally:
    tree.close()

  with pytest.raises(Failure, match="manifest missing"):
    Archive(root, create=True)

  assert (root / extra).read_bytes() == b"keep"
  assert (root / temporary).read_bytes() == b"unfinished"


@pytest.mark.parametrize("sync", [False, True])
def test_dashboard_failure_affects_sync_but_not_local_completion(
  archive, sync, capsys
):
  archive.item("dashboard", {
    "title": "Dashboard",
    "path": "documents/dashboard.md",
    "folder": "documents",
    "parent": "index",
    "status": "failed",
    "message": "dashboard layout changed",
  }, None)
  archive.item("index", {"children": ["dashboard", COURSE]}, None)
  archive.begin("sync" if sync else "process")
  summary = {
    "processed_roots": 0,
    "reused_roots": 0,
    "failed_roots": 0,
    "limited_roots": 0,
    "worker_seconds": 0,
  }
  processor = SimpleNamespace(
    busy=lambda: False,
    close=lambda: None,
    summary=lambda: summary,
  )
  upstream = {"status": "complete", "code": 0} if sync else None

  code = workflows._complete(
    archive,
    processor,
    [COURSE],
    time.monotonic(),
    upstream=upstream,
  )

  assert code == (3 if sync else 0)
  assert archive.data["run"]["incomplete_courses"] == 0
  output = capsys.readouterr().out
  assert ("dashboard: incomplete" in output) is sync
  assert archive.data["run"].get("incomplete_dashboard", False) is sync


def listing_producer(private_dir, body):
  page = soup_html('<div id="bodyContent">' + body + "</div>")
  reader = SimpleNamespace(
    course="one",
    page=lambda *args: page,
    menu=lambda *args: (page, CTL + "/Etest.do?cmd=viewEtestInfoList"),
  )
  producer = Producer(
    reader=reader,
    root=private_dir,
    snapshot={
      "items": {
        "listing": {
          "title": "Listing",
          "folder": FOLDER + "/Listing",
          "path": FOLDER + "/Listing/index.md",
          "status": "archived",
          "children": ["listing/old"],
          "parent": COURSE,
        },
      },
      "documents": {},
    },
    options=SimpleNamespace(
      course=None,
      refresh=False,
      include_submissions=True,
    ),
    events=queue.Queue(),
    stop=threading.Event(),
    home=Home(page, "student-1", {"one": "Course"}),
  )
  return producer, page


@pytest.mark.parametrize(
  "body",
  [
    '<div id="listBox"><p>No data</p><p>Important post</p></div>',
    (
      '<div id="listBox"><p>No data</p>'
      '<a onclick="newPostHandler()">Post</a></div>'
    ),
    '<div id="listBox"><p>No data</p><img src="/lmsdata/source.png"></div>',
    (
      '<div id="listBox"><p>No data</p></div>'
      '<div id="listBox"><p>Additional instructions</p></div>'
    ),
    (
      '<div id="listBox"><p>No data</p></div>'
      '<dl class="element"><dt>Unrecognized post</dt></dl>'
    ),
    (
      '<div id="listBox"><p>No data</p></div>'
      '<div class="new-layout">'
      '<a onclick="newPostHandler()">Important post</a></div>'
    ),
    (
      '<div id="listBox"><p>No data</p></div>'
      '<p>Important instructions outside the listing</p>'
    ),
    (
      '<div id="listBox"><p>No data</p></div>'
      '<img src="/lmsdata/source.png">'
    ),
    '<div class="search_unfine">No data</div><p>Important post</p>',
  ],
)
def test_ambiguous_board_does_not_clear_previous_references(private_dir, body):
  producer, _ = listing_producer(private_dir, body)

  with pytest.raises(Failure, match="board layout"):
    CourseTraversal(producer).board(
      CTL + "/Course.do?cmd=viewBoardContentsList",
      "listing",
      "Listing",
      FOLDER + "/Listing",
      COURSE,
    )

  assert producer.items["listing"]["children"] == ["listing/old"]
  assert producer.items["listing"]["status"] == "partial"


@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("outside", [False, True])
def test_ambiguous_activity_list_does_not_clear_previous_references(
  private_dir, team, outside
):
  marker = '<div class="search_unfine">No data</div>'
  content = "<p>Important activity</p>"
  body = (
    f'<div id="listBox">{marker}</div>{content}'
    if outside else f'<div id="listBox">{marker}{content}</div>'
  )
  producer, page = listing_producer(private_dir, body)

  with pytest.raises(Failure, match="activity list"):
    CourseTraversal(producer).activities(
      page,
      "listing",
      "Listing",
      FOLDER + "/Listing",
      CTL + ("/Teamact.do" if team else "/Report.do"),
      team,
      COURSE,
    )

  assert producer.items["listing"]["children"] == ["listing/old"]
  assert producer.items["listing"]["status"] == "partial"


@pytest.mark.parametrize("boxed", [False, True])
@pytest.mark.parametrize("menu", ["menu_00065", "menu_00067"])
def test_unsupported_section_keeps_content_beside_an_empty_marker(
  private_dir, boxed, menu
):
  marker = '<div class="search_unfine">No data</div>'
  if boxed:
    marker = '<div id="listBox">' + marker + "</div>"

  producer, _ = listing_producer(
    private_dir, marker + "<p>Important scheduled activity</p>"
  )
  recipe = MENUS[menu]
  key = COURSE + "/" + recipe[0]

  CourseTraversal(producer).section(
    "menu", recipe, key, recipe[0], FOLDER, COURSE
  )

  item = producer.items[key]
  assert item["status"] == "unsupported"
  assert item["empty_listing"] is False
  bodies = [
    event.data["body"]
    for event in list(producer.events.queue)
    if event.kind == "item"
    and event.data["key"] == key
    and event.data["body"] is not None
  ]
  assert "Important scheduled activity" in bodies[-1]


@pytest.mark.parametrize("scaffold", [False, True])
def test_explicit_empty_board_can_replace_previous_listing(
  private_dir, scaffold
):
  body = (
    '<div id="listBox">'
    '<div class="search_unfine">No data.</div></div>'
  )
  if scaffold:
    body += (
      '<input type="hidden" name="context" value="one">'
      '<div class="searchBox"><input name="query"><button>Search</button></div>'
      '<div class="listFooter"><a href="#top">Top</a></div>'
      "<script>initializeListing()</script>"
    )
  producer, page = listing_producer(private_dir, body)
  before = str(page)

  CourseTraversal(producer).board(
    CTL + "/Course.do?cmd=viewBoardContentsList",
    "listing",
    "Listing",
    FOLDER + "/Listing",
    COURSE,
  )

  assert producer.items["listing"]["children"] == []
  assert producer.items["listing"]["status"] == "empty"
  assert str(page) == before


def test_root_diagnostics_ignore_unreachable_historical_failures(archive):
  archive.item("historical", {
    "title": "Historical",
    "path": FOLDER + "/historical.md",
    "folder": FOLDER,
    "status": "failed",
    "message": "obsolete layout failure",
    "parent": COURSE,
  }, None)

  archive.flush_views()

  root = archive.read("documents/index.md", 4096).decode()
  assert "obsolete layout failure" not in root
  assert "## Incomplete" not in root
  assert "historical" in archive.data["items"]


def test_root_diagnostics_include_referenced_historical_failures(archive):
  archive.item("historical", {
    "title": "Historical",
    "path": FOLDER + "/historical.md",
    "folder": FOLDER,
    "status": "failed",
    "message": "retained layout failure",
    "parent": OWNER,
  }, None)
  archive.item(OWNER, {"related": ["historical"]}, None)

  archive.flush_views()

  root = archive.read("documents/index.md", 4096).decode()
  assert "retained layout failure" in root
  assert "## Incomplete" in root


def test_diagnostic_reachability_is_cycle_safe_without_weakening_export(archive):
  archive.item(OWNER, {"related": [COURSE]}, None)

  archive.flush_views()

  with pytest.raises(Failure, match="cycle"):
    list(archive.walk_course(COURSE))


def test_retained_coverage_survives_publication_export_and_reuse(archive, products):
  raw = b"<p>Original document</p>"
  key = add_document(archive, raw, "lesson.html")
  run_document(archive, key)
  previous = converted_body(archive, key)
  warning = "Native text contains U+FFFE on pages 1-3"
  products.body = b"New partial source text\n"
  products.coverage = [warning]
  archive.settings["convert"] = {
    **archive.settings["convert"],
    "pdf_coverage": archive.settings["convert"]["pdf_coverage"] + 1,
  }

  result = run_document(archive, key)
  node = result["nodes"][""]
  attempt = node["attempts"]["convert"]

  assert converted_body(archive, key) == previous
  assert node["products"]["convert"]["status"] == "complete"
  assert attempt["status"] == "partial"
  assert attempt["retained"] is True
  assert attempt["retry"] is False
  assert attempt["summary"] == {"coverage": [warning]}
  assert archive.data["items"][OWNER]["documents"] == [key]
  doc = archive.data["documents"][key]
  assert archive.read(doc["folder"] + "/lesson.html", 4096) == raw
  assert archive.reusable(doc, ["convert", "ocr"])
  assert "> latest attempt: " + warning in generated_text(archive, key, "text.md")
  assert "convert, latest attempt: " + warning in generated_text(
    archive, key, "index.md"
  )
  assert export_courses(archive, [COURSE]) == 3

  exported = archive.read("extracted/Course.txt", 16384).decode()
  footer = exported.rsplit("## Extraction notes\n", 1)[1]
  assert previous.decode() in exported
  assert "New partial source text" not in exported
  assert footer.count(warning) == 1
  assert "convert, latest attempt: " + warning in footer
  assert "Previous result retained" in footer

  products.calls.clear()
  run_document(archive, key)

  assert products.calls == []
  assert converted_body(archive, key) == previous
  assert export_courses(archive, [COURSE]) == 3
  assert archive.read("extracted/Course.txt", 16384).decode() == exported


def test_later_failed_retained_attempt_does_not_erase_known_coverage(
  archive, products
):
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  warning = "Pages 1-3 contain annotations not represented as native body text"
  products.coverage = [warning]
  run_document(archive, key, force=True)
  products.coverage = []
  products.error = Failure("later decoder failed")

  result = run_document(archive, key, force=True)
  attempt = result["nodes"][""]["attempts"]["convert"]

  assert attempt["retained"] is True
  assert attempt["retry"] is True
  assert attempt["summary"] == {"coverage": [warning]}
  assert attempt["problems"] == ["later decoder failed"]
  for filename in ("text.md", "index.md"):
    text = generated_text(archive, key, filename)
    assert "latest attempt: later decoder failed" in text
    assert "retained coverage, attempt unknown: " + warning in text
    assert "latest attempt: " + warning not in text

  assert export_courses(archive, [COURSE]) == 3
  footer = archive.read("extracted/Course.txt", 16384).decode().rsplit(
    "## Extraction notes\n", 1
  )[1]
  assert footer.count(warning) == 1
  assert "convert, retained coverage, attempt unknown: " + warning in footer
  assert "convert, latest attempt: later decoder failed" in footer
  assert "latest attempt: " + warning not in footer


def test_successful_replacement_clears_retained_attempt_coverage(
  archive, products
):
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  products.coverage = ["Previous coverage concern"]
  run_document(archive, key, force=True)
  products.coverage = []
  products.body = b"Successful replacement\n"

  result = run_document(archive, key, force=True)
  attempt = result["nodes"][""]["attempts"]["convert"]

  assert attempt["status"] == "complete"
  assert attempt["retained"] is False
  assert "summary" not in attempt
  assert converted_body(archive, key) == b"Successful replacement\n"
  assert "Previous coverage concern" not in generated_text(
    archive, key, "text.md"
  )
  assert export_courses(archive, [COURSE]) == 0
  assert "Previous coverage concern" not in archive.read(
    "extracted/Course.txt", 16384
  ).decode()


def test_recovered_text_replaces_an_incomplete_gallery_only_product(
  archive, products
):
  products.body = b""
  products.gallery = True
  products.problems = ["Native conversion unavailable"]
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  previous = run_document(archive, key)["nodes"][""]["products"]["convert"]

  assert previous["status"] == "partial"
  assert previous["markdown"] == "text.md"
  assert previous["content_bytes"] == 0

  products.body = b"Recovered source text\n"
  products.gallery = False
  products.problems = ["Recovery has limited coverage"]

  result = run_document(archive, key, force=True)
  node = result["nodes"][""]

  assert converted_body(archive, key) == b"Recovered source text\n"
  assert node["attempts"]["convert"]["retained"] is False
  assert node["products"]["convert"]["status"] == "partial"
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  assert export_courses(archive, [COURSE]) == 3
  text = archive.read("extracted/Course.txt", 16384).decode()
  assert "Recovered source text" in text
  assert "Recovery has limited coverage" in text
  assert "Native conversion unavailable" not in text


def test_failed_attempt_does_not_discard_an_existing_partial_gallery(
  archive, products
):
  products.body = b""
  products.gallery = True
  products.problems = ["Native conversion unavailable"]
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  products.error = Failure("decoder failed")

  result = run_document(archive, key, force=True)
  node = result["nodes"][""]
  doc = archive.data["documents"][key]

  assert node["attempts"]["convert"]["retained"] is True
  assert node["products"]["convert"]["markdown"] == "text.md"
  assert converted_body(archive, key) == b""
  assert b"## Extracted images" in archive.read(
    doc["folder"] + "/text.md", 4096
  )


def test_complete_empty_product_stays_protected_from_partial_replacement(
  archive, products
):
  products.body = b""
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  products.body = b"New partial text\n"
  products.problems = ["Incomplete recovery"]

  result = run_document(archive, key, force=True)
  node = result["nodes"][""]

  assert node["attempts"]["convert"]["retained"] is True
  assert node["products"]["convert"]["status"] == "complete"
  assert node["products"]["convert"]["markdown"] is None
  assert converted_body(archive, key) == b""


def test_longer_partial_text_does_not_override_existing_usable_partial_text(
  archive, products
):
  products.problems = ["Existing limitation"]
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  previous = converted_body(archive, key)
  products.body = b"Much longer replacement text\n" * 20
  products.problems = ["Different limitation"]

  result = run_document(archive, key, force=True)

  assert converted_body(archive, key) == previous
  assert result["nodes"][""]["attempts"]["convert"]["retained"] is True


def test_budget_rejected_recovery_keeps_coverage_and_previous_output(
  archive, products, monkeypatch
):
  products.body = b""
  products.gallery = True
  products.problems = ["Native conversion unavailable"]
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  products.body = b"Recovered source\n" * 100
  products.gallery = False
  products.problems = []
  warning = "Recovery covers ordinary paragraphs only"
  products.coverage = [warning]
  monkeypatch.setattr(processing, "CONTENT_LIMIT", 128)

  result = run_document(archive, key, force=True)
  node = result["nodes"][""]
  attempt = node["attempts"]["convert"]

  assert attempt["retained"] is True
  assert attempt["retry"] is True
  assert attempt["summary"] == {"coverage": [warning]}
  assert any("root byte budget" in problem for problem in attempt["problems"])
  assert node["products"]["convert"]["content_bytes"] == 0
  assert "retained coverage, attempt unknown: " + warning in generated_text(
    archive, key, "index.md"
  )
  assert export_courses(archive, [COURSE]) == 3
  text = archive.read("extracted/Course.txt", 16384).decode()
  assert text.count(warning) == 1
  assert "retained coverage, attempt unknown: " + warning in text
  assert "latest attempt: " + warning not in text


def test_coverage_takes_priority_over_the_generic_diagnostic_cap():
  coverage = [
    "Native text contains suspect code points",
    "Annotations are not represented as native text",
  ]
  product = outcome(
    {"version": 1},
    problems=[f"Image {index} unavailable" for index in range(20)],
    summary={"coverage": coverage},
  )

  assert len(product["problems"]) == 20
  assert product["problems"][:2] == coverage
  assert product["summary"]["coverage"] == coverage
  assert product["status"] == "partial"
  assert product["retry"] is False


def test_crc_limited_discovery_is_published_exported_and_reused(archive):
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as container:
    container.writestr("bad.txt", b"Bad source\n")
    container.writestr("good.txt", b"Healthy source\n")
    member = container.getinfo("bad.txt")
    offset = member.header_offset + 30 + len(member.filename.encode())

  raw = bytearray(stream.getvalue())
  raw[offset] ^= 1
  raw = bytes(raw)
  key = add_document(archive, raw)
  result = run_document(archive, key)
  doc = archive.data["documents"][key]

  assert result["nodes"][""]["retry"] is False
  assert result["nodes"][""]["problems"]
  assert archive.reusable(doc, ["convert", "ocr"])
  assert archive.read(doc["folder"] + "/source.zip", len(raw)) == raw
  assert export_courses(archive, [COURSE]) == 3
  text = archive.read("extracted/Course.txt", 16384).decode()
  assert "Healthy source" in text
  assert "Bad source" not in text
  assert "Bad CRC-32" in text

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

  repeated = run_document(archive, key, force=True)
  assert repeated["nodes"][""]["problems"] == result["nodes"][""]["problems"]
  assert repeated["nodes"][""]["retry"] is False
  assert archive.data["items"][OWNER]["documents"] == [key]
  assert export_courses(archive, [COURSE]) == 3
  assert archive.read("extracted/Course.txt", 16384).decode() == text


def test_successive_coverage_findings_survive_reopening_until_replacement(
  archive, products
):
  raw = b"<p>Archived original</p>"
  key = add_document(archive, raw, "lesson.html")
  result = run_document(archive, key)
  original_body = converted_body(archive, key)
  original_product = copy.deepcopy(result["nodes"][""]["products"]["convert"])
  original_page = copy.deepcopy(archive.data["items"][OWNER])
  first = "Native text contains U+FFFE on page 1"
  second = "Page 2 contains Highlight annotations not represented as native text"

  products.body = b"First partial candidate\n"
  products.coverage = [first]
  run_document(archive, key, force=True)
  products.body = b"Second partial candidate\n"
  products.coverage = [second]

  result = run_document(archive, key, force=True)
  node = result["nodes"][""]
  attempt = node["attempts"]["convert"]

  assert attempt["summary"] == {"coverage": [first, second]}
  assert attempt["problems"] == [second]
  assert attempt["retained"] is True
  assert attempt["retry"] is False
  assert converted_body(archive, key) == original_body
  assert node["products"]["convert"]["content_sha256"] == (
    original_product["content_sha256"]
  )
  assert node["products"]["convert"]["finished"] == original_product["finished"]
  assert archive.data["items"][OWNER] == original_page
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  for filename in ("text.md", "index.md"):
    text = generated_text(archive, key, filename)
    assert "retained coverage, attempt unknown: " + first in text
    assert "latest attempt: " + second in text
    assert "latest attempt: " + first not in text
  archive.validate()

  root = archive.root
  archive.close()
  reopened = Archive(root, create=False)

  try:
    reopened.prepare_export()
    doc = reopened.data["documents"][key]
    saved_attempt = doc["result"]["nodes"][""]["attempts"]["convert"]
    assert saved_attempt["summary"] == {"coverage": [first, second]}
    assert saved_attempt["problems"] == [second]
    assert converted_body(reopened, key) == original_body
    assert reopened.read(doc["folder"] + "/lesson.html", len(raw)) == raw
    before = copy.deepcopy(reopened.data)
    assert export_courses(reopened, [COURSE]) == 3
    assert reopened.data == before
    text = reopened.read("extracted/Course.txt", 16384).decode()
    footer = text.rsplit("## Extraction notes\n", 1)[1]
    assert footer.startswith("\nGenerated by kctl\nSources with notes: 1\n")
    assert footer.count(first) == 1
    assert footer.count(second) == 1
    assert "retained coverage, attempt unknown: " + first in footer
    assert "latest attempt: " + second in footer
    assert "latest attempt: " + first not in footer
    assert "Previous result retained" in footer
    assert "First partial candidate" not in text
    assert "Second partial candidate" not in text

    products.coverage = []
    products.body = b"Accepted replacement\n"
    replacement = run_document(reopened, key, force=True)
    latest = replacement["nodes"][""]["attempts"]["convert"]

    assert latest["retained"] is False
    assert latest["status"] == "complete"
    assert "summary" not in latest
    assert converted_body(reopened, key) == b"Accepted replacement\n"
    assert reopened.data["items"][OWNER] == original_page
    assert export_courses(reopened, [COURSE]) == 0
    text = reopened.read("extracted/Course.txt", 16384).decode()
    assert first not in text
    assert second not in text
    assert text.endswith(
      "## Extraction notes\n\nGenerated by kctl\nSources with notes: 0\n"
    )
    reopened.validate()
  finally:
    reopened.close()


def test_retained_coverage_overflow_survives_later_attempts_and_export(
  archive, products
):
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  run_document(archive, key)
  original_body = converted_body(archive, key)
  findings = [f"Coverage finding {index:02d}" for index in range(22)]
  products.body = b"Unaccepted partial body\n"
  products.coverage = findings[:10]
  run_document(archive, key, force=True)
  products.coverage = findings[10:]

  result = run_document(archive, key, force=True)
  expected = findings[:19] + [COVERAGE_OVERFLOW]
  attempt = result["nodes"][""]["attempts"]["convert"]

  assert attempt["summary"]["coverage"] == expected
  assert attempt["retained"] is True
  assert len(attempt["summary"]["coverage"]) == 20
  assert converted_body(archive, key) == original_body

  products.coverage = ["One later finding"]
  result = run_document(archive, key, force=True)
  assert result["nodes"][""]["attempts"]["convert"]["summary"] == {
    "coverage": expected,
  }
  archive.validate()
  root = archive.root
  archive.close()
  reopened = Archive(root, create=False)

  try:
    reopened.prepare_export()
    doc = reopened.data["documents"][key]
    attempt = doc["result"]["nodes"][""]["attempts"]["convert"]
    assert attempt["summary"]["coverage"] == expected
    assert reopened.reusable(doc, ["convert", "ocr"])
    assert converted_body(reopened, key) == original_body
    assert export_courses(reopened, [COURSE]) == 3
    text = reopened.read("extracted/Course.txt", 16384).decode()
    footer = text.rsplit("## Extraction notes\n", 1)[1]
    assert footer.startswith("\nGenerated by kctl\nSources with notes: 1\n")
    assert footer.count(COVERAGE_OVERFLOW) == 1
    assert "retained coverage, attempt unknown: " + COVERAGE_OVERFLOW in footer
    for finding in findings[:19]:
      assert footer.count(finding) == 1
      assert "retained coverage, attempt unknown: " + finding in footer
    assert "Unaccepted partial body" not in text
    assert "One later finding" in attempt["problems"]
    assert "convert, latest attempt: One later finding" in footer
    assert reopened.data["items"][OWNER]["documents"] == [key]
    reopened.validate()
  finally:
    reopened.close()


def test_published_and_latest_findings_are_attributed_without_duplicate_text(
  archive, products
):
  shared = "Shared coverage finding"
  old = "Native text contains U+FFFE: 1 on page 1"
  new = "Native text contains U+FFFE: 2 on page 1"
  products.coverage = [shared, old]
  key = add_document(archive, b"<p>Source</p>", "lesson.html")
  previous = run_document(archive, key)
  previous_body = converted_body(archive, key)
  previous_product = copy.deepcopy(
    previous["nodes"][""]["products"]["convert"]
  )
  products.body = b"Unaccepted replacement\n"
  products.coverage = [shared, new]

  result = run_document(archive, key, force=True)
  node = result["nodes"][""]
  expected = {
    shared: "published result / latest attempt: ",
    old: "published result: ",
    new: "latest attempt: ",
  }

  assert node["attempts"]["convert"]["retained"] is True
  assert node["attempts"]["convert"]["problems"] == [shared, new]
  assert node["attempts"]["convert"]["summary"]["coverage"] == [shared, old, new]
  assert node["products"]["convert"]["summary"] == previous_product["summary"]
  assert converted_body(archive, key) == previous_body

  index = generated_text(archive, key, "index.md")
  for finding, prefix in expected.items():
    assert index.count(finding) == 1
    assert "convert, " + prefix + finding in index

  header = generated_text(archive, key, "text.md")
  assert "> published result / latest attempt: " + shared in header
  assert "> published result: " + old in header
  assert "Additional limitations are recorded" in header

  before = copy.deepcopy(archive.data)
  assert export_courses(archive, [COURSE]) == 3
  assert archive.data == before
  exported = archive.read("extracted/Course.txt", 16384)
  footer = exported.decode().rsplit("## Extraction notes\n", 1)[1]
  for finding, prefix in expected.items():
    assert footer.count(finding) == 1
    assert "convert, " + prefix + finding in footer
  assert "Unaccepted replacement" not in exported.decode()

  products.calls.clear()
  run_document(archive, key)

  assert products.calls == []
  assert converted_body(archive, key) == previous_body
  assert export_courses(archive, [COURSE]) == 3
  assert archive.read("extracted/Course.txt", 16384) == exported
