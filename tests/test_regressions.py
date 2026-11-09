import copy
import io
import zipfile

from types import SimpleNamespace

import pytest

import kctl.auth as auth_module
import kctl.content as content_module
import kctl.exports as exports_module
import kctl.session as session

from kctl.archive import Archive
from kctl.content import Blob, ContentWalker, Output
from kctl.conversion import Engines
from kctl.core import (
  Failure,
  SourceProblem,
  StorageFailure,
  digest,
  identity,
  named_kind,
)
from kctl.exports import CourseExporter, export_courses
from kctl.network import new_jar
from kctl.processing import DocumentJob
from kctl.records import outcome, processing_settings
from kctl.session import State, authenticate
from kctl.storage import signature, write_new


COURSE = "course/one"
FOLDER = "documents/Course"
FIRST = COURSE + "/first"
SECOND = COURSE + "/second"


@pytest.fixture
def archive(private_dir):
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
    "parent": "index",
    "status": "archived",
    "course_id": "one",
    "include_submissions": True,
    "children": [FIRST, SECOND],
  }, None)

  for key, name in ((FIRST, "first.md"), (SECOND, "second.md")):
    value.item(key, {
      "title": name,
      "path": FOLDER + "/" + name,
      "folder": FOLDER,
      "parent": COURSE,
      "status": "archived",
      "documents": [],
      "related": [],
      "problems": [],
    }, f"Instructions for {name}\n")

  try:
    yield value
  finally:
    value.close()


def add_document(archive, owner=FIRST, name="reading.pdf", raw=b"%PDF-same"):
  number = len(archive.data["documents"]) + 1
  url = f"https://ctl.kmu.ac.kr/fileDownServlet?id={number}"
  key = identity(owner, url)
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/new/" + name, raw)
  record = {
    "owner": owner,
    "url": url,
    "name": name,
    "folder": FOLDER + f"/Document-{number}",
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
  references = archive.data["items"][owner]["documents"]
  archive.item(owner, {"documents": [*references, key]}, None)
  return key


def install_result(
  archive,
  key,
  *,
  body=b"Recorded source\n",
  problems=(),
  children=(),
):
  doc = copy.deepcopy(archive.data["documents"][key])
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(
    work + "/new/" + doc["original"],
    archive.read(doc["folder"] + "/" + doc["original"], 1024 * 1024),
  )
  root = {
    "name": doc["name"],
    "original": doc["original"],
    "kind": named_kind(doc["original"]),
    "bytes": doc["bytes"],
    "sha256": doc["sha256"],
    "children": [],
    "problems": list(problems),
    "retry": False,
    "visuals": None,
    "products": {},
    "attempts": {},
  }
  nodes = {"": root}
  files = {}

  if body is not None:
    product = outcome(
      archive.settings["convert"],
      outputs={"text.md"} if body else set(),
      markdown="text.md" if body else None,
      content_bytes=len(body),
      content_sha256=digest(body),
    )
    if body:
      header = b"# Generated\n\n"
      product["header_bytes"] = len(header)
      raw = header + body
      archive.write(work + "/new/text.md", raw)
      files["text.md"] = len(raw)
    root["products"]["convert"] = product

  for index, (name, raw) in enumerate(children, 1):
    folder = f"contents/member-{index}"
    root["children"].append(folder)
    nodes[folder] = {
      "name": name,
      "original": name,
      "kind": named_kind(name),
      "bytes": len(raw),
      "sha256": digest(raw),
      "children": [],
      "problems": [],
      "retry": False,
      "visuals": None,
      "products": {},
      "attempts": {},
    }
    relative = folder + "/" + name
    archive.write(work + "/new/" + relative, raw)
    files[relative] = len(raw)

  if problems or children or root["kind"] == "zip":
    archive.write(work + "/new/index.md", b"Details\n")
    files["index.md"] = len(b"Details\n")

  doc["result"] = {
    "source": doc["sha256"],
    "settings": copy.deepcopy(archive.settings["walk"]),
    "nodes": nodes,
    "files": files,
    "finished": "2026-11-04T00:00:00+00:00",
  }
  archive.install("documents", key, doc, work)
  return archive.data["documents"][key]


def retain_export(archive):
  archive.mkdir("extracted")
  archive.write("extracted/Course.txt", b"Previous complete course\n")


def exported(archive):
  return archive.read("extracted/Course.txt", 1024 * 1024).decode()


@pytest.mark.parametrize("damage", ["product", "original"])
def test_empty_duplicate_cannot_replace_damaged_recorded_text(archive, damage):
  first = add_document(archive, FIRST)
  second = add_document(archive, SECOND)
  doc = install_result(archive, first, body=b"Important recorded text\n")
  install_result(archive, second, body=b"")
  retain_export(archive)
  relative = "text.md" if damage == "product" else doc["original"]
  archive.remove(doc["folder"] + "/" + relative)

  assert export_courses(archive, [COURSE]) == 2
  assert exported(archive) == "Previous complete course\n"


def test_valid_nonempty_duplicate_can_still_replace_damaged_text(archive):
  first = add_document(archive, FIRST)
  second = add_document(archive, SECOND)
  doc = install_result(archive, first, body=b"Shared source\n")
  install_result(archive, second, body=b"Shared source\n")
  archive.remove(doc["folder"] + "/text.md")

  assert export_courses(archive, [COURSE]) == 0
  text = exported(archive)
  assert text.count("Shared source") == 1
  assert "## Sources\n\n" in text
  assert "- Course/first.md / reading.pdf" in text
  assert "- Course/second.md / reading.pdf" in text
  footer = text.rsplit("## Extraction notes\n", 1)[1]
  assert "Sources with notes: 1" in footer
  assert "using result from identical original" in footer


@pytest.mark.parametrize("operation", ["signature", "file_digest"])
def test_missing_original_after_path_check_can_use_duplicate(
  archive, monkeypatch, operation
):
  first = add_document(archive, FIRST)
  second = add_document(archive, SECOND)
  doc = install_result(archive, first)
  install_result(archive, second)
  target = archive.path(doc["folder"] + "/" + doc["original"])
  original = getattr(exports_module, operation)

  def disappear(path):
    if path == target:
      raise FileNotFoundError(str(path))
    return original(path)

  monkeypatch.setattr(exports_module, operation, disappear)

  assert export_courses(archive, [COURSE]) == 0
  assert exported(archive).count("Recorded source") == 1


@pytest.mark.parametrize("moment", ["before_read", "after_read"])
def test_disappearing_product_is_a_candidate_failure_not_global_storage_error(
  archive, monkeypatch, moment
):
  first = add_document(archive, FIRST)
  second = add_document(archive, SECOND)
  doc = install_result(archive, first)
  install_result(archive, second)
  target = doc["folder"] + "/text.md"
  original_read = archive.read

  def disappear(relative, limit):
    if relative != target:
      return original_read(relative, limit)
    if moment == "before_read":
      raise FileNotFoundError(relative)
    raw = original_read(relative, limit)
    archive.remove(relative)
    return raw

  monkeypatch.setattr(archive, "read", disappear)

  assert export_courses(archive, [COURSE]) == 0
  assert exported(archive).count("Recorded source") == 1


def test_late_missing_input_preserves_existing_export(archive, monkeypatch):
  key = add_document(archive)
  doc = install_result(archive, key)
  retain_export(archive)
  exporter = CourseExporter(archive, COURSE)
  exporter.gather()
  exporter.select()
  target = archive.path(doc["folder"] + "/" + doc["original"])
  original_signature = exports_module.signature

  def disappear(path):
    if path == target:
      raise FileNotFoundError(str(path))
    return original_signature(path)

  monkeypatch.setattr(exports_module, "signature", disappear)

  with pytest.raises(SourceProblem, match="recorded input is missing"):
    exporter.write()

  assert exported(archive) == "Previous complete course\n"


def test_genuine_digest_io_error_is_still_fatal_storage_failure(
  archive, monkeypatch
):
  first = add_document(archive, FIRST)
  second = add_document(archive, SECOND)
  install_result(archive, first)
  install_result(archive, second)
  retain_export(archive)

  def fail(path):
    raise OSError("device error")

  monkeypatch.setattr(exports_module, "file_digest", fail)

  with pytest.raises(StorageFailure, match="I/O failed"):
    export_courses(archive, [COURSE])

  assert exported(archive) == "Previous complete course\n"


def test_duplicate_container_diagnostics_are_not_suppressed_by_cleaner_inventory(
  archive, capsys
):
  first = add_document(archive, FIRST, "project.zip", b"same archive")
  second = add_document(archive, SECOND, "project.zip", b"same archive")
  install_result(
    archive,
    first,
    body=None,
    children=[("one.txt", b"First source\n")],
  )
  install_result(
    archive,
    second,
    body=None,
    problems=["remaining contents omitted: traversal budget reached"],
    children=[
      ("one.txt", b"First source\n"),
      ("two.txt", b"Second source\n"),
    ],
  )
  capsys.readouterr()

  assert export_courses(archive, [COURSE]) == 3
  text = exported(archive)

  assert text.count("First source") == 1
  assert text.count("Second source") == 1
  assert "traversal budget" in capsys.readouterr().err
  footer = text.rsplit("## Extraction notes\n", 1)[1]
  assert "traversal budget" in footer
  assert "Course/second.md / project.zip" in footer
  assert "Course/first.md / project.zip" not in footer


def test_attachment_sources_distinguish_pages_sharing_a_folder(archive):
  first = add_document(archive, FIRST, "same.pdf", b"%PDF-first")
  second = add_document(archive, SECOND, "same.pdf", b"%PDF-second")
  install_result(archive, first, body=b"First attachment\n")
  install_result(archive, second, body=b"Second attachment\n")

  assert export_courses(archive, [COURSE]) == 0
  text = exported(archive)

  assert "## Course/first.md / same.pdf\n\nFirst attachment" in text
  assert "## Course/second.md / same.pdf\n\nSecond attachment" in text


def test_claimed_protected_output_reports_ownership_before_duplication(private_dir):
  path = private_dir / "source.txt"
  write_new(path, b"keep")
  output = Output(private_dir, protected={"source.txt": Blob(path, 4)})
  output.claim("source.txt")

  with pytest.raises(Failure, match="protected"):
    with output.open("source.txt"):
      pass

  assert path.read_bytes() == b"keep"


@pytest.mark.parametrize("raw", [b"invalid-\xff", b"text\x00binary"])
def test_text_inspection_records_nonretryable_limitations_without_derivative(
  private_dir, raw
):
  path = private_dir / "source.txt"
  write_new(path, raw)
  output = Output(private_dir)
  walker = ContentWalker(output)
  node = walker.node("", "source.txt", "source.txt", len(raw), digest(raw))
  walker.walk("")

  assert node["kind"] == ".txt"
  assert node["products"] == {}
  assert node["retry"] is False
  assert node["problems"]
  assert "text inspection" in node["problems"][0]
  assert path.read_bytes() == raw
  assert output.files == {}


def test_large_text_is_checked_before_reading(private_dir, monkeypatch):
  raw = b"large text"
  write_new(private_dir / "source.txt", raw)
  monkeypatch.setattr(content_module, "TEXT_LIMIT", 2)

  def unexpected(*args):
    raise AssertionError("oversized text must not be read")

  monkeypatch.setattr(content_module, "read_private", unexpected)
  walker = ContentWalker(Output(private_dir))
  node = walker.node("", "source.txt", "source.txt", len(raw), digest(raw))

  assert node["problems"] == ["text inspection: text original exceeds text limit"]
  assert node["retry"] is False


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_valid_passthrough_text_remains_an_original_only(
  private_dir, encoding
):
  raw = "자료\n  indented source\n".encode(encoding)
  write_new(private_dir / "source.txt", raw)
  walker = ContentWalker(Output(private_dir))
  node = walker.node("", "source.txt", "source.txt", len(raw), digest(raw))

  assert node["problems"] == []
  assert node["retry"] is False
  assert node["products"] == {}
  assert (private_dir / "source.txt").read_bytes() == raw


def test_invalid_text_in_container_does_not_prevent_other_members(private_dir):
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w") as container:
    container.writestr("bad.txt", b"invalid-\xff")
    container.writestr("good.py", b"print('source')\n")
  raw = stream.getvalue()
  write_new(private_dir / "project.zip", raw)
  walker = ContentWalker(Output(private_dir))
  walker.node("", "project.zip", "project.zip", len(raw), digest(raw))
  walker.walk("")

  children = [
    walker.nodes[folder] for folder in walker.nodes[""]["children"]
  ]
  assert len(children) == 2
  assert children[0]["problems"]
  assert children[0]["retry"] is False
  assert children[1]["problems"] == []


def test_text_validation_is_published_and_cached_as_a_known_limitation(archive):
  key = add_document(archive, FIRST, "invalid.txt", b"invalid-\xff")
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
    "force": False,
    "work": work,
  }
  engines = Engines()

  try:
    message = DocumentJob(spec, engines).run()
  finally:
    engines.close()

  message.update(notes=[], seconds=0)
  archive.publish(spec, message)
  current = archive.data["documents"][key]
  node = current["result"]["nodes"][""]

  assert node["problems"]
  assert node["retry"] is False
  assert node["products"] == {}
  assert "index.md" in current["result"]["files"]
  assert "text.md" not in current["result"]["files"]
  assert archive.reusable(current, ["convert", "ocr"])
  assert export_courses(archive, [COURSE]) == 3
  assert "text inspection" in exported(archive)


def test_processing_settings_invalidate_discovery_before_text_validation():
  settings = processing_settings()

  assert settings["walk"]["version"] > 1
  assert settings["walk"]["text_bytes"] == content_module.TEXT_LIMIT


@pytest.fixture
def auth_state(private_dir, monkeypatch):
  monkeypatch.chdir(private_dir)
  for name in session.CONFIG_NAMES:
    monkeypatch.delenv(name, raising=False)

  scenario = SimpleNamespace(
    home=("learner", "student-1"),
    page=object(),
    calls=[],
  )

  class Auth:
    def __init__(self, jar):
      self.jar = jar

    def home(self, probe=False):
      scenario.calls.append(("home", probe))
      if isinstance(scenario.home, BaseException):
        raise scenario.home
      return (*scenario.home, scenario.page)

    def password_login(self, student_id, password=None):
      scenario.calls.append(("login", student_id, password))
      return scenario.page

  monkeypatch.setattr(auth_module, "Auth", Auth)

  with State(private_dir / "state") as state:
    state.save_login(new_jar(), "student-1")
    yield state, scenario


def test_valid_session_never_requests_password_configuration(
  auth_state, monkeypatch
):
  state, scenario = auth_state
  reads = []

  def read_config(*names):
    reads.append(names)
    assert names == ("KCTL_STUDENT_ID",)
    return {"KCTL_STUDENT_ID": "student-1"}

  monkeypatch.setattr(session, "read_config", read_config)
  jar, page = authenticate(state, interactive=False, quiet=True)

  try:
    assert page is scenario.page
    assert reads == [("KCTL_STUDENT_ID",)]
    assert scenario.calls == [("home", True)]
  finally:
    jar.clear()


def test_password_only_configuration_does_not_break_valid_saved_session(
  auth_state, private_dir
):
  state, scenario = auth_state
  path = private_dir / ".env"
  write_new(path, b"KCTL_PASSWORD=unused-secret\n")
  path.chmod(0o644)

  jar, page = authenticate(state, interactive=False, quiet=True)

  try:
    assert page is scenario.page
    assert scenario.calls == [("home", True)]
    assert state.account() == "student-1"
  finally:
    jar.clear()


def test_password_only_configuration_is_rejected_when_login_is_needed(
  auth_state, private_dir
):
  state, scenario = auth_state
  scenario.home = ("guest", None)
  write_new(private_dir / ".env", b"KCTL_PASSWORD=secret\n")

  with pytest.raises(Failure, match="explicit KCTL_STUDENT_ID"):
    authenticate(state, interactive=False)

  assert scenario.calls == [("home", True)]


def test_credentials_are_loaded_after_session_probe_not_before(
  auth_state, monkeypatch
):
  state, scenario = auth_state
  scenario.home = ("guest", None)
  reads = []

  def read_config(*names):
    reads.append(names)
    if "KCTL_PASSWORD" in names:
      assert scenario.calls == [("home", True)]
    configured = {
      "KCTL_STUDENT_ID": "student-1",
      "KCTL_PASSWORD": "configured-password",
    }
    return {name: configured[name] for name in names}

  monkeypatch.setattr(session, "read_config", read_config)
  jar, page = authenticate(state, interactive=False, quiet=True)

  try:
    assert page is scenario.page
    assert reads == [
      ("KCTL_STUDENT_ID",),
      ("KCTL_STUDENT_ID", "KCTL_PASSWORD"),
    ]
    assert scenario.calls == [
      ("home", True),
      ("login", "student-1", "configured-password"),
    ]
  finally:
    jar.clear()


def test_probe_transport_failure_does_not_load_password_configuration(
  auth_state, monkeypatch
):
  state, scenario = auth_state
  scenario.home = Failure("connection failed")

  def read_config(*names):
    assert names == ("KCTL_STUDENT_ID",)
    return {"KCTL_STUDENT_ID": "student-1"}

  monkeypatch.setattr(session, "read_config", read_config)

  with pytest.raises(Failure, match="connection failed"):
    authenticate(state, interactive=False)

  assert scenario.calls == [("home", True)]


def test_account_configuration_cannot_change_between_probe_and_login(
  auth_state, monkeypatch
):
  state, scenario = auth_state
  scenario.home = ("guest", None)

  def read_config(*names):
    if names == ("KCTL_STUDENT_ID",):
      return {"KCTL_STUDENT_ID": "student-1"}
    return {
      "KCTL_STUDENT_ID": "other-student",
      "KCTL_PASSWORD": "secret",
    }

  monkeypatch.setattr(session, "read_config", read_config)

  with pytest.raises(Failure, match="configuration changed"):
    authenticate(state, interactive=False)

  assert scenario.calls == [("home", True)]
  assert state.account() == "student-1"
