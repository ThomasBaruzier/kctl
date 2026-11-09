import copy
import errno
import os
import queue
import threading

from collections import deque
from contextlib import contextmanager, nullcontext
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import kctl.auth as auth_module
import kctl.processing as processing
import kctl.reader as reader_module
import kctl.session as session_module
import kctl.workflows as workflows

from kctl.archive import Archive
from kctl.auth_html import soup_html
from kctl.cli import arguments
from kctl.core import (
  CTL,
  HOME_URL,
  TEXT_LIMIT,
  Failure,
  StorageFailure,
  digest,
  identity,
  named_kind,
  now,
  parse_json,
)
from kctl.exports import export_courses
from kctl.network import new_jar
from kctl.reader import Home
from kctl.records import outcome
from kctl.retrieval import Producer
from kctl.storage import decoder_input, signature


COURSE = "course/one"
OWNER = COURSE + "/page"
FOLDER = "documents/Course"
PAGE = FOLDER + "/page.md"
ATTACHMENT = b"Previously archived attachment\n"


@pytest.fixture
def archive(private_dir):
  value = Archive(private_dir / "archive", create=True)
  value.prepare()
  value.account("student-1")
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
    "title": "Page",
    "path": PAGE,
    "folder": FOLDER,
    "status": "archived",
    "parent": COURSE,
    "documents": [],
    "related": [],
    "problems": [],
  }, "Source instructions\n")
  value.flush_views()

  try:
    yield value
  finally:
    value.close()


def add_document(
  archive,
  *,
  name="reading.txt",
  raw=ATTACHMENT,
  role="attachment",
  stored="stored",
):
  url = CTL + "/fileDownServlet?" + urlencode({
    "rFileName": name,
    "sFileName": stored,
    "filePath": "folder",
  })
  key = identity(OWNER, url)
  number = len(archive.data["documents"]) + 1
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/new/" + name, raw)
  record = {
    "owner": OWNER,
    "url": url,
    "name": name,
    "folder": FOLDER + f"/Document-{number}",
    "original": name,
    "role": role,
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": signature(archive.path(work + "/new/" + name)),
    "etag": '"previous"',
    "result": None,
    "attempts": {},
  }
  archive.document(key, record, work)
  references = archive.data["items"][OWNER]["documents"]
  archive.item(OWNER, {"documents": [*references, key]}, None)
  return key


def refresh_page(archive, stored, *, role="attachment", include=True):
  requests = []

  @contextmanager
  def rejected(url, headers=None):
    requests.append((url, headers))
    yield SimpleNamespace(status=500)

  events = queue.Queue()
  producer = Producer(
    reader=SimpleNamespace(http=SimpleNamespace(open=rejected)),
    root=archive.root,
    snapshot=copy.deepcopy(archive.data),
    options=SimpleNamespace(
      refresh=False,
      include_submissions=include,
      course=None,
    ),
    events=events,
    stop=threading.Event(),
    home=Home(None, "student-1", {"one": "Course"}),
  )
  markup = "<p>Updated instructions</p>"

  if stored is not None:
    markup += (
      f'<a onclick="fileDownload(\'reading.txt\', \'{stored}\', \'folder\')">'
      "reading.txt</a>"
    )

  producer.content(
    OWNER,
    "Page",
    PAGE,
    [soup_html(markup)],
    CTL + "/Course.do?cmd=viewStudyHome",
    parent=COURSE,
    role=role,
  )

  while not events.empty():
    event = events.get_nowait()
    assert event.kind == "item"
    archive.item(
      event.data["key"],
      event.data["changes"],
      event.data["body"],
    )

  return requests


@pytest.mark.parametrize("stored", ["stored", "new", None])
def test_failed_refresh_retains_only_observed_existing_attachments(
  archive, stored
):
  key = add_document(archive)
  requests = refresh_page(archive, stored)

  assert archive.data["items"][OWNER]["documents"] == (
    [key] if stored == "stored" else []
  )
  assert len(requests) == (0 if stored is None else 1)
  if stored == "stored":
    assert requests[0][1] == {"If-None-Match": '"previous"'}

  assert export_courses(archive, [COURSE]) == (0 if stored is None else 3)
  text = archive.read("extracted/Course.txt", 4096)
  assert b"Updated instructions" in text
  assert (ATTACHMENT in text) is (stored == "stored")
  doc = archive.data["documents"][key]
  assert archive.read(doc["folder"] + "/reading.txt", 4096) == ATTACHMENT


def test_failed_refresh_does_not_conceal_a_damaged_retained_original(archive):
  key = add_document(archive)
  assert export_courses(archive, [COURSE]) == 0
  previous = archive.read("extracted/Course.txt", 4096)
  doc = archive.data["documents"][key]
  archive.remove(doc["folder"] + "/reading.txt")

  assert refresh_page(archive, "stored") == []
  assert archive.data["items"][OWNER]["documents"] == [key]
  assert export_courses(archive, [COURSE]) == 2
  assert archive.read("extracted/Course.txt", 4096) == previous


def test_excluded_submission_is_not_restored_or_requested(archive):
  key = add_document(archive, role="submission")

  assert refresh_page(
    archive, "stored", role="submission", include=False
  ) == []
  assert archive.data["items"][OWNER]["documents"] == []
  assert key in archive.data["documents"]


def test_submission_policy_applies_before_dashboard_failure(archive, monkeypatch):
  key = add_document(archive, role="submission")
  other = "course/two"
  archive.item(other, {
    "title": "Other",
    "path": "documents/Other/index.md",
    "folder": "documents/Other",
    "status": "empty",
    "parent": "index",
    "course_id": "two",
    "include_submissions": True,
    "children": [],
  }, None)
  archive.item("index", {"children": [COURSE, other]}, None)
  assert export_courses(archive, [COURSE]) == 0
  assert ATTACHMENT in archive.read("extracted/Course.txt", 4096)
  root = archive.root
  archive.checkpoint()
  archive.close()
  requests = []
  verified_page = object()

  def expired(url, form):
    requests.append(url)
    raise Failure("session expired", 1)

  def home(page):
    assert page is verified_page
    return Home(page, "student-1", {"one": "Course", "two": "Other"})

  reader = SimpleNamespace(
    course="",
    home=home,
    page=expired,
    http=SimpleNamespace(requests=0, bytes=0, seconds=0),
  )
  state = SimpleNamespace(
    account=lambda: "student-1",
    save_cookies=lambda jar: None,
  )
  monkeypatch.setattr(workflows, "State", lambda path: nullcontext(state))
  monkeypatch.setattr(
    workflows, "authenticate", lambda *args, **kwargs: (new_jar(), verified_page)
  )
  monkeypatch.setattr(workflows, "Reader", lambda *args: reader)

  code = workflows.run_sync(arguments([
    "--state-dir", str(root.parent / "state"),
    "sync", str(root), "--course", "one", "--no-submissions",
  ]))

  assert code == 1
  assert requests == [CTL + "/Study.do?cmd=viewStudyMyClassroom"]
  reopened = Archive(root, create=False)

  try:
    assert reopened.data["items"][COURSE]["include_submissions"] is False
    assert reopened.data["items"][other]["include_submissions"] is True
    assert reopened.data["items"][OWNER]["documents"] == [key]
    assert reopened.data["documents"][key]["result"] is None
    assert reopened.document_keys([COURSE]) == []
    text = reopened.read("extracted/Course.txt", 4096)
    assert b"Source instructions" in text
    assert ATTACHMENT not in text
    doc = reopened.data["documents"][key]
    assert reopened.read(doc["folder"] + "/reading.txt", 4096) == ATTACHMENT
  finally:
    reopened.close()


@pytest.mark.parametrize("enrolled", [False, True])
def test_sync_reuses_auth_homepage_and_still_validates_enrollment(
  archive, monkeypatch, enrolled
):
  archive.checkpoint()
  root = archive.root
  archive.close()
  requests = []
  retrievals = []
  jars = []
  headers = Message()
  headers["Content-Type"] = "text/html; charset=utf-8"
  markup = (
    '<form name="mainForm" method="post" action="/Main.do">'
    '<input type="hidden" name="userType" value="learner"></form>'
    '<div id="header"><div class="login"><fieldset><p>'
    "<span>Student (student-1)</span>"
    "</p></fieldset></div></div>"
  )
  if enrolled:
    markup += '<a onclick="viewMyCourse(\'one\')">Course</a>'

  class HTTP:
    def __init__(self, jar, limit, stop=None):
      self.jar = jar
      jars.append(jar)

    @contextmanager
    def open(self, url, data=None, probe=False):
      requests.append((url, data, probe))
      assert url == HOME_URL
      yield SimpleNamespace(status=200, headers=headers)

    def read(self, response):
      return markup.encode()

  def retrieve(current, processor, reader, home, options, stop):
    assert home.account == "student-1"
    assert home.courses == {"one": "Course"}
    assert home.page.select_one('a[onclick="viewMyCourse(\'one\')"]') is not None
    retrievals.append(home)
    return {"status": "complete", "code": 0}

  monkeypatch.setattr(auth_module, "HTTP", HTTP)
  monkeypatch.setattr(reader_module, "HTTP", HTTP)
  monkeypatch.setattr(
    session_module, "read_config", lambda *names: {name: "" for name in names}
  )
  monkeypatch.setattr(workflows, "_retrieve", retrieve)
  options = arguments([
    "--state-dir", str(root.parent / "state"),
    "sync", str(root), "--course", "one",
  ])

  if enrolled:
    assert workflows.run_sync(options) == 0
  else:
    with pytest.raises(Failure, match="enrolled course"):
      workflows.run_sync(options)

  assert requests == [(HOME_URL, None, True)]
  assert len(retrievals) == int(enrolled)
  assert all(len(jar) == 0 for jar in jars)
  reopened = Archive(root, create=False)

  try:
    if enrolled:
      assert reopened.data["run"]["status"] == "complete"
      assert b"Source instructions" in reopened.read(
        "extracted/Course.txt", 4096
      )
    else:
      assert reopened.data["run"] is None
      assert not reopened.exists("extracted/Course.txt")
  finally:
    reopened.close()


def test_oversized_generated_index_is_rebuilt_without_reading_it(
  archive, monkeypatch
):
  relative = "documents/index.md"
  with archive.path(relative).open("r+b") as stream:
    stream.truncate(TEXT_LIMIT + 1)

  original_read = archive.read

  def read(path, limit):
    assert path != relative, "oversized generated index must not be read"
    return original_read(path, limit)

  monkeypatch.setattr(archive, "read", read)
  archive.prepare()

  assert archive.path(relative).stat().st_size < TEXT_LIMIT
  assert b"# Archive" in original_read(relative, 4096)


def install_text(archive, key):
  doc = copy.deepcopy(archive.data["documents"][key])
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(
    work + "/new/" + doc["original"],
    archive.read(doc["folder"] + "/" + doc["original"], 4096),
  )
  body = b"Shared converted source\n"
  archive.write(work + "/new/text.md", body)
  product = outcome(
    archive.settings["convert"],
    outputs={"text.md"},
    markdown="text.md",
    content_bytes=len(body),
    content_sha256=digest(body),
  )
  node = {
    "name": doc["name"],
    "original": doc["original"],
    "kind": named_kind(doc["original"]),
    "bytes": doc["bytes"],
    "sha256": doc["sha256"],
    "children": [],
    "problems": [],
    "retry": False,
    "visuals": None,
    "products": {"convert": product},
    "attempts": {},
  }
  doc["result"] = {
    "source": doc["sha256"],
    "settings": copy.deepcopy(archive.settings["walk"]),
    "nodes": {"": node},
    "files": {"text.md": len(body)},
    "finished": now(),
  }
  archive.install("documents", key, doc, work)


def test_oversized_damaged_product_allows_healthy_duplicate_fallback(archive):
  first = add_document(archive, name="reading.pdf", raw=b"%PDF-identical")
  second = add_document(
    archive, name="reading.pdf", raw=b"%PDF-identical", stored="copy"
  )
  install_text(archive, first)
  install_text(archive, second)
  doc = archive.data["documents"][first]

  with archive.path(doc["folder"] + "/text.md").open("r+b") as stream:
    stream.truncate(TEXT_LIMIT + 1)

  assert export_courses(archive, [COURSE]) == 0
  text = archive.read("extracted/Course.txt", 4096)
  assert text.count(b"Shared converted source") == 1


def test_decoder_eof_cannot_hide_a_recorded_storage_failure(private_file):
  path = private_file("source.bin", b"source")
  failure = StorageFailure("local read failed")

  with pytest.raises(StorageFailure) as captured:
    with decoder_input(path) as stream:
      stream.raw.failure = failure
      raise EOFError("decoder wrapped the read failure")

  assert captured.value is failure


def test_decoder_eof_without_storage_failure_is_unchanged(private_file):
  path = private_file("source.bin", b"source")

  with pytest.raises(EOFError):
    with decoder_input(path):
      raise EOFError("truncated document")


def test_worker_job_read_io_failure_is_reported_as_fatal(archive, monkeypatch):
  work = archive.new_work()
  archive.write(work + "/job.json", b"{}")
  messages = deque([os.fsencode(archive.path(work)), b""])
  sent = []
  connection = SimpleNamespace(
    recv_bytes=lambda limit: messages.popleft(),
    send_bytes=sent.append,
    close=lambda: None,
  )

  def fail(path, limit):
    raise OSError(errno.EIO, "job device failure")

  monkeypatch.setattr(processing, "read_private", fail)
  monkeypatch.setattr(
    processing, "Engines", lambda: SimpleNamespace(close=lambda: None)
  )
  monkeypatch.setattr(processing.signal, "pthread_sigmask", lambda *args: None)
  monkeypatch.setattr(processing.signal, "signal", lambda *args: None)
  monkeypatch.setattr(processing.os, "umask", lambda value: None)
  monkeypatch.setattr(processing.os, "environ", {})

  processing.worker_main(connection)

  message = parse_json(archive.read(work + "/result.json", 4096))
  assert sent == [b"1"]
  assert message["result"] is None
  assert message["error"] is None
  assert "archive I/O failed" in message["fatal"]
