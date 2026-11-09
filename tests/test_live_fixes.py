import copy
import io
import queue
import threading
import zipfile

from html import escape
from types import SimpleNamespace

import pytest

import kctl.content as content

from PIL import Image

from kctl.archive import Archive
from kctl.auth_html import soup_html, text_of
from kctl.content import Output
from kctl.conversion import Engines, FileProcessor
from kctl.core import CTL, Failure, digest, identity, named_kind, text_language
from kctl.courses import (
  MENUS,
  TEAM_FIELDS,
  CourseTraversal,
  _content_region,
  _empty_listing,
)
from kctl.exports import export_courses
from kctl.processing import DocumentJob
from kctl.records import processing_settings
from kctl.retrieval import Producer
from kctl.storage import signature, write_new


COURSE = "course/one"
FOLDER = "documents/Course"
TEAM = {
  "courseDTO.courseId": "one",
  "teamactInfoDTO.teamactId": "activity-1",
  "teamactInfoDTO.teamCategoryId": "category-1",
  "teamDTO.teamInfoId": "team-1",
}
CATEGORY = "teamactInfoDTO.teamCategoryId"
EMPTY = '<div class="search_unfine">조회된 내용이 없습니다.</div>'


def listing(body=EMPTY, *, outside="", frame=""):
  return soup_html(
    '<div id="bodyContent"><!DOCTYPE html>'
    "<html><head><title>CTL title</title></head><body>"
    '<form name="courseForm">'
    '<input type="hidden" name="courseDTO.courseId" value="one">'
    '<div class="contentTitle"><h2>Navigation title</h2>'
    '<ul class="location"><li><a href="/">Home</a></li></ul></div>'
    '<div class="boardTab"><a href="/Course.do">Other menu</a></div>'
    '<div class="boardOption"><select name="searchKey">'
    '<option value="title">Search title</option></select>'
    '<input type="image" onclick="searchSubmit()">'
    '<a class="btn" onclick="viewStudyHome()">Course home</a></div>'
    '<div id="listBox"><div id="listBox">'
    + body
    + "</div></div>"
    + outside
    + "</form>"
    + frame
    + "</body></html></div>"
  )


@pytest.fixture
def producer(private_dir):
  return Producer(
    reader=SimpleNamespace(course="one"),
    root=private_dir,
    snapshot={"items": {}, "documents": {}},
    options=SimpleNamespace(
      course=None,
      refresh=False,
      include_submissions=True,
    ),
    events=queue.Queue(),
    stop=threading.Event(),
    home=None,
  )


def captured_body(producer, key):
  return [
    event.data["body"]
    for event in list(producer.events.queue)
    if event.kind == "item"
    and event.data["key"] == key
    and event.data["body"] is not None
  ][-1]


@pytest.mark.parametrize("src", [None, "", "about:blank"])
def test_realistic_empty_listing_ignores_only_known_page_scaffolding(src):
  attribute = "" if src is None else f' src="{src}"'
  page = listing(frame=f'<iframe name="hiddenFrame"{attribute}></iframe>')
  before = str(page)

  assert _empty_listing(page)
  assert text_of(_content_region(page)) == "조회된 내용이 없습니다."
  assert str(page) == before


@pytest.mark.parametrize(
  "extra",
  [
    "<p>Important instructions</p>",
    '<a onclick="newPostHandler()">Unrecognized post</a>',
    '<img src="/lmsdata/source.png">',
    '<div class="boardOption"><table><tr><td>Result</td></tr></table></div>',
    '<iframe name="hiddenFrame" src="/lesson"></iframe>',
    '<iframe name="hiddenFrame" srcdoc="<p>Lesson</p>"></iframe>',
    '<iframe name="hiddenFrame">Fallback lesson</iframe>',
    "<iframe></iframe>",
  ],
)
def test_page_scaffolding_does_not_hide_competing_content(extra):
  assert not _empty_listing(listing(outside=extra))


@pytest.mark.parametrize("container", ["listBox", "cont"])
def test_navigation_class_names_inside_source_content_are_preserved(container):
  source = '<div class="boardOption">Important source text</div>'
  if container == "listBox":
    page = listing(EMPTY + source)
  else:
    page = listing(outside='<div class="cont">' + source + "</div>")

  assert not _empty_listing(page)
  assert "Important source text" in text_of(_content_region(page))


def test_realistic_empty_board_replaces_old_references(producer):
  key = COURSE + "/boards/one"
  producer.items[key] = {
    "title": "Board",
    "folder": FOLDER + "/Board",
    "path": FOLDER + "/Board/index.md",
    "status": "archived",
    "parent": COURSE,
    "children": [key + "/old"],
  }
  producer.reader.page = lambda *args: listing()

  CourseTraversal(producer).board(
    CTL + "/Course.do?cmd=viewBoardContentsList",
    key,
    "Board",
    FOLDER + "/Board",
    COURSE,
  )

  assert producer.items[key]["status"] == "empty"
  assert producer.items[key]["children"] == []


@pytest.mark.parametrize("menu", ["menu_00065", "menu_00067"])
def test_empty_section_has_no_navigation_or_transport_frame_warning(
  producer, menu
):
  page = listing(frame='<iframe name="hiddenFrame"></iframe>')
  recipe = MENUS[menu]
  key = COURSE + "/" + recipe[0]
  producer.reader.menu = lambda *args: (page, CTL + recipe[1])

  CourseTraversal(producer).section(
    "menu", recipe, key, recipe[0], FOLDER, COURSE
  )

  assert producer.items[key]["status"] == "empty"
  assert producer.items[key]["empty_listing"] is True
  assert producer.items[key]["problems"] == []
  body = captured_body(producer, key)
  assert body.count("조회된 내용이 없습니다.") == 1
  assert "Navigation title" not in body
  assert "Other menu" not in body


def test_nonempty_section_retains_unknown_content_without_navigation(producer):
  page = listing(outside="<p>Exam scheduled Friday</p>")
  producer.reader.menu = lambda *args: (page, CTL + "/Etest.do")
  key = COURSE + "/exams"

  CourseTraversal(producer).section(
    "menu", MENUS["menu_00067"], key, "Exams", FOLDER, COURSE
  )

  assert producer.items[key]["status"] == "unsupported"
  assert producer.items[key]["empty_listing"] is False
  body = captured_body(producer, key)
  assert "Exam scheduled Friday" in body
  assert "Navigation title" not in body


def test_offline_attendance_preserves_rows_outside_an_empty_box(producer):
  response = listing(outside=(
    "<table><tr><th>Week 1</th><td>Present</td></tr></table>"
  ))
  producer.reader.post = lambda *args: (response, CTL + "/Attend.do")
  key = COURSE + "/attendance/offline"

  CourseTraversal(producer).offline_attendance(
    soup_html(""), key, FOLDER, COURSE
  )

  assert producer.items[key]["empty_listing"] is False
  body = captured_body(producer, key)
  assert "Present" in body
  assert "Navigation title" not in body


def team_page(fields, body=""):
  controls = "".join(
    f'<input type="hidden" name="{escape(name)}" '
    f'value="{escape(value, quote=True)}">'
    for name, value in fields.items()
  )
  return soup_html(
    '<div id="bodyContent"><form name="teamactForm">'
    + controls + body + "</form></div>"
  )


def empty_receipt():
  return (
    '<dl class="element"><dt>Activity</dt><dd>Instructions</dd></dl>'
    '<dl class="element"><dt><h4><i></i>제출정보</h4></dt><dd>'
    '<table class="boardListWrite" summary="산출물 제출 정보">'
    '<caption>산출물 제출 정보</caption><tbody><td colspan="2">'
    "<ul><li>제출 내역이 없습니다.</li></ul>"
    "</td></tbody></table></dd></dl>"
  )


@pytest.mark.parametrize("category", [None, "category-1"])
def test_output_accepts_missing_or_matching_category_only(producer, category):
  fields = {name: value for name, value in TEAM.items() if name != CATEGORY}
  if category is not None:
    fields[CATEGORY] = category

  response = team_page(fields, empty_receipt())
  producer.reader.post = lambda *args: (response, CTL + "/Teamact.do")
  key = COURSE + "/output"

  CourseTraversal(producer).team_output(
    team_page(TEAM), key, FOLDER, COURSE
  )

  assert producer.items[key]["status"] == "empty"
  assert producer.items[key]["empty_listing"] is False
  assert "제출 내역이 없습니다." in captured_body(producer, key)


@pytest.mark.parametrize(
  ("field", "value"),
  [
    (field, value)
    for field in TEAM_FIELDS
    for value in ("", "wrong")
  ] + [
    (field, None) for field in TEAM_FIELDS if field != CATEGORY
  ],
)
def test_output_still_requires_matching_returned_identity(producer, field, value):
  fields = dict(TEAM)
  if value is None:
    fields.pop(field)
  else:
    fields[field] = value

  response = team_page(fields, empty_receipt())
  producer.reader.post = lambda *args: (response, CTL + "/Teamact.do")

  with pytest.raises(Failure, match="context mismatch"):
    CourseTraversal(producer).team_output(
      team_page(TEAM), COURSE + "/output", FOLDER, COURSE
    )


def processor(root, kind, raw, engines=None):
  source = root / ("original" + kind)
  write_new(source, raw)
  destination = root / "candidate"
  destination.mkdir(mode=0o700)
  node = {
    "name": source.name,
    "original": source.name,
    "kind": kind,
    "bytes": len(raw),
    "sha256": digest(raw),
  }
  return FileProcessor(
    source,
    Output(destination),
    "",
    node,
    "convert",
    processing_settings(),
    engines or Engines(),
  )


@pytest.mark.parametrize("fail", [False, True])
def test_real_pdfium_bitmap_closes_image_before_buffer(
  private_dir, monkeypatch, caplog, fail
):
  import pypdfium2 as pdfium

  value = processor(private_dir, ".pdf", b"%PDF-test")
  raw = pdfium.raw.FPDFBitmap_Create(8, 8, 0)
  assert raw
  bitmap = pdfium.PdfBitmap.from_raw(raw)
  pdfium.raw.FPDFBitmap_FillRect(raw, 0, 0, 8, 8, 0xFFFFFFFF)
  pdfium.raw.FPDFBitmap_FillRect(raw, 0, 0, 1, 1, 0xFF000000)
  images = []
  original_to_pil = bitmap.to_pil
  original_close = bitmap.close

  def to_pil():
    image = original_to_pil()
    images.append(image)
    return image

  def close():
    with pytest.raises(ValueError):
      images[0].getpixel((0, 0))
    original_close()

  monkeypatch.setattr(bitmap, "to_pil", to_pil)
  monkeypatch.setattr(bitmap, "close", close)

  if fail:
    def reject(*args):
      raise Failure("image publication failed")

    monkeypatch.setattr(value, "accept_image", reject)

    with pytest.raises(Failure, match="image publication"):
      value._bitmap(bitmap, "Image", 1)
  else:
    value._bitmap(bitmap, "Image", 1)
    path = value.output.resolve(value.images[0]["path"]).path
    with Image.open(path) as image:
      assert image.size == (8, 8)
      assert image.getpixel((0, 0)) == (0, 0, 0)
      assert image.getpixel((7, 7)) == (255, 255, 255)

  assert not bitmap.raw
  assert "potentially unsafe operation" not in caplog.text


def test_office_crc_failure_identifies_member_and_keeps_healthy_images(private_dir):
  image_data = io.BytesIO()
  with Image.new("RGB", (8, 8), "white") as image:
    image.putpixel((0, 0), (0, 0, 0))
    image.save(image_data, format="PNG")
  image_bytes = image_data.getvalue()
  package = io.BytesIO()

  with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as archive:
    archive.writestr("[Content_Types].xml", "<Types/>")
    archive.writestr("ppt/presentation.xml", "<presentation/>")
    archive.writestr("ppt/media/bad.png", image_bytes)
    archive.writestr("ppt/media/good.png", image_bytes)
    member = archive.getinfo("ppt/media/bad.png")
    offset = (
      member.header_offset + 30
      + len(member.filename.encode()) + len(member.extra)
    )

  raw = bytearray(package.getvalue())
  raw[offset] ^= 1
  raw = bytes(raw)
  engines = SimpleNamespace(markdown=lambda *args: "Healthy source text\n")
  value = processor(private_dir, ".pptx", raw, engines)

  product, visuals = value.run()

  assert value.source.read_bytes() == raw
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["retry"] is False
  assert len(visuals["images"]) == 1
  assert "good.png" in visuals["images"][0]["label"]
  assert any(
    "ppt/media/bad.png" in message and "Bad CRC-32" in message
    for message in product["problems"]
  )


@pytest.mark.parametrize("extension", [".puml", ".plantuml"])
def test_plantuml_is_passthrough_source(extension):
  assert named_kind("diagrams/example" + extension.upper()) == extension
  assert text_language(extension) == "plantuml"


def run_document(archive, key):
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
  return archive.data["documents"][key]["result"]


def test_plantuml_rediscovery_updates_existing_archive_without_force(
  private_dir, monkeypatch
):
  source = b"@startuml\nAlice -> Bob: Hello\n@enduml\n"
  package = io.BytesIO()
  with zipfile.ZipFile(package, "w") as container:
    container.writestr("diagrams/example.puml", source)
  raw = package.getvalue()
  archive = Archive(private_dir / "archive", create=True)

  try:
    archive.prepare()
    archive.item("index", {
      "title": "Archive",
      "path": "documents/index.md",
      "folder": "documents",
      "status": "archived",
      "children": [COURSE],
      "parent": None,
    }, None)
    archive.item(COURSE, {
      "title": "Course",
      "path": FOLDER + "/index.md",
      "folder": FOLDER,
      "status": "archived",
      "children": [COURSE + "/page"],
      "parent": "index",
      "course_id": "one",
      "include_submissions": True,
    }, None)
    owner = COURSE + "/page"
    archive.item(owner, {
      "title": "Diagrams",
      "path": FOLDER + "/page.md",
      "folder": FOLDER,
      "status": "archived",
      "parent": COURSE,
      "documents": [],
    }, "Diagram source\n")

    url = CTL + "/fileDownServlet?id=diagrams"
    key = identity(owner, url)
    work = archive.new_work()
    archive.mkdir(work + "/new")
    archive.write(work + "/new/diagrams.zip", raw)
    archive.document(key, {
      "owner": owner,
      "url": url,
      "name": "diagrams.zip",
      "folder": FOLDER + "/Diagrams",
      "original": "diagrams.zip",
      "role": "attachment",
      "bytes": len(raw),
      "sha256": digest(raw),
      "signature": signature(archive.path(work + "/new/diagrams.zip")),
      "result": None,
      "attempts": {},
    }, work)
    archive.item(owner, {"documents": [key]}, None)
    current_settings = copy.deepcopy(archive.settings)
    archive.settings["walk"].pop("text_kinds")
    original_kind = content.local_kind

    def old_kind(path, name=None):
      if path.suffix.lower() == ".puml":
        return "unsupported"
      return original_kind(path, name)

    with monkeypatch.context() as patch:
      patch.setattr(content, "local_kind", old_kind)
      previous = run_document(archive, key)

    child = previous["nodes"][""]["children"][0]
    assert previous["nodes"][child]["kind"] == "unsupported"

    archive.settings = current_settings
    assert not archive.reusable(
      archive.data["documents"][key], ["convert", "ocr"]
    )
    result = run_document(archive, key)

    assert result["nodes"][child]["kind"] == ".puml"
    assert result["nodes"][child]["products"] == {}
    assert archive.data["items"][owner]["documents"] == [key]
    assert archive.read(FOLDER + "/Diagrams/diagrams.zip", len(raw)) == raw
    assert export_courses(archive, [COURSE]) == 0
    text = archive.read("extracted/Course.txt", 4096).decode()
    assert "```plantuml\n" + source.decode() + "```" in text
    assert archive.reusable(
      archive.data["documents"][key], ["convert", "ocr"]
    )
  finally:
    archive.close()


@pytest.mark.parametrize(
  "panel",
  [
    (
      '<div class="boardOption"><span data-source '
      'onclick="fileDownload(\'guide.pdf\', \'stored\', \'folder\')">'
      "Required guide</span></div>"
    ),
    (
      '<div class="boardOption" data-source '
      'onclick="fileDownload(\'guide.pdf\', \'stored\', \'folder\')">'
      "Required guide</div>"
    ),
    (
      '<div class="boardOption"><a data-source '
      'href="/fileDownServlet?id=guide">Required guide</a></div>'
    ),
    (
      '<a class="btnBox" data-source href="/lmsdata/guide.pdf">'
      "Required guide</a>"
    ),
    (
      '<div class="boardOption"><div class="btnBox">'
      '<img data-source src="/lmsdata/result.png"></div></div>'
    ),
    '<img class="boardOption" data-source src="/lmsdata/result.png">',
    '<div class="boardOption"><svg data-source><path d="M0 0"/></svg></div>',
    '<div class="boardOption"><iframe data-source src="/lesson"></iframe></div>',
  ],
)
def test_navigation_panels_preserve_attachments_and_media(panel):
  page = listing(outside=panel)
  before = str(page)
  original = page.select_one("[data-source]")
  retained = _content_region(page).select_one("[data-source]")

  assert retained is not None
  assert str(retained) == str(original)
  assert not _empty_listing(page)
  assert str(page) == before
