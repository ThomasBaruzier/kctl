import base64
import copy
import io
import socket

import pytest

import kctl.conversion as conversion

from PIL import Image

from kctl.archive import Archive
from kctl.conversion import Engines, prepare_html
from kctl.core import CTL, digest, identity
from kctl.exports import export_courses
from kctl.processing import DocumentJob, Processor
from kctl.storage import signature


COURSE = "course/one"
OWNER = COURSE + "/page"
FOLDER = "documents/Course"
MEDIA_NOTE = "embedded HTML content is not executed or fetched"
PICTURE_NOTE = "HTML picture has no supported fallback image"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
  def unexpected(*args, **kwargs):
    pytest.fail("HTML media processing attempted network access or OCR startup")

  monkeypatch.setattr(socket, "create_connection", unexpected)
  monkeypatch.setattr(socket.socket, "connect", unexpected)
  monkeypatch.setattr(Engines, "recognize", unexpected)


@pytest.fixture
def archive(private_dir):
  value = Archive(private_dir / "archive", create=True)

  try:
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
      "status": "archived",
      "course_id": "one",
      "include_submissions": True,
      "children": [OWNER],
      "parent": "index",
    }, None)
    value.item(OWNER, {
      "title": "Page",
      "path": FOLDER + "/page.md",
      "folder": FOLDER,
      "status": "archived",
      "documents": [],
      "parent": COURSE,
    }, "Course instructions\n")
    yield value
  finally:
    value.close()


def add_html(archive, raw):
  url = CTL + "/fileDownServlet?id=html"
  key = identity(OWNER, url)
  work = archive.new_work()
  archive.mkdir(work + "/new")
  archive.write(work + "/new/lesson.html", raw)
  archive.document(key, {
    "owner": OWNER,
    "url": url,
    "name": "lesson.html",
    "folder": FOLDER + "/Lesson",
    "original": "lesson.html",
    "role": "attachment",
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": signature(archive.path(work + "/new/lesson.html")),
    "result": None,
    "attempts": {},
  }, work)
  archive.item(OWNER, {"documents": [key]}, None)
  return key


def process_html(archive, key):
  document = copy.deepcopy(archive.data["documents"][key])
  work = archive.new_work()
  archive.mkdir(work + "/new")
  spec = {
    "key": key,
    "root": str(archive.root),
    "document": document,
    "signature": copy.deepcopy(document["signature"]),
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

  archive.publish(spec, {**message, "notes": [], "seconds": 0})
  return archive.data["documents"][key]["result"]["nodes"][""]


def source_text(archive, key):
  doc = archive.data["documents"][key]
  product = doc["result"]["nodes"][""]["products"]["convert"]
  return archive.read_content(
    doc["folder"] + "/" + product["markdown"],
    product,
    size=doc["result"]["files"][product["markdown"]],
  ).decode()


@pytest.mark.parametrize(
  ("markup", "expected", "note"),
  [
    (
      '<audio src="https://example.com/recording.mp3" title="Audio lesson">'
      "</audio>",
      ["Audio lesson"],
      MEDIA_NOTE,
    ),
    (
      '<video aria-label="Demonstration">'
      '<source src="https://example.com/clip.mp4">'
      "<p>Static fallback instructions</p></video>",
      ["Demonstration", "Static fallback instructions"],
      MEDIA_NOTE,
    ),
    (
      '<video src="https://example.com/clip.mp4"></video>',
      [],
      MEDIA_NOTE,
    ),
    (
      '<picture title="Measured result">'
      '<source srcset="https://example.com/result.webp"></picture>',
      ["Measured result"],
      PICTURE_NOTE,
    ),
    (
      '<picture><source srcset="https://example.com/large.webp">'
      '<img src="https://example.com/result.png" alt="Remote diagram">'
      "</picture>",
      ["Remote diagram"],
      "linked HTML images are not fetched",
    ),
  ],
)
def test_html_media_limitations_reach_publication_and_export(
  archive, markup, expected, note
):
  raw = ("<h1>Lesson</h1><p>Static source text</p>" + markup).encode()
  key = add_html(archive, raw)
  node = process_html(archive, key)
  text = source_text(archive, key)

  assert "Lesson" in text
  assert "Static source text" in text
  for description in expected:
    assert text.count(description) == 1
  assert "https://example.com" not in text

  for step in ("convert", "ocr"):
    product = node["products"][step]
    assert product["status"] == "partial"
    assert product["retry"] is False
    assert product["summary"]["coverage"] == [note]
    assert product["problems"] == [note]

  assert node["visuals"]["status"] == "partial"
  assert node["visuals"]["retry"] is False
  assert node["visuals"]["problems"] == [note]
  assert node["visuals"]["images"] == []
  assert archive.read(FOLDER + "/Lesson/lesson.html", len(raw)) == raw
  assert archive.data["items"][OWNER]["documents"] == [key]
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  archive.validate()

  assert export_courses(archive, [COURSE]) == 3
  exported = archive.read("extracted/Course.txt", 16384).decode()
  footer = exported.rsplit("## Extraction notes\n", 1)[1]
  assert "Static source text" in exported
  assert footer.startswith("\nGenerated by kctl\nSources with notes: 1\n")
  assert "Course/page.md / lesson.html" in footer
  assert note in footer


def test_media_fallback_preserves_code_links_and_distinct_description(archive):
  raw = (
    '<h1>Lesson</h1><video title="Demonstration">'
    "<p>Static explanation</p>"
    '<pre><code class="language-python">def example():\n  return 1\n'
    "</code></pre>"
    '<a href="https://example.com/reference">Reference</a>'
    '<source src="https://example.com/video.mp4"></video>'
  ).encode()
  key = add_html(archive, raw)

  node = process_html(archive, key)

  text = source_text(archive, key)
  assert "Demonstration" in text
  assert "Static explanation" in text
  assert "```python\ndef example():\n  return 1\n```" in text
  assert "https://example.com/reference" in text
  assert "https://example.com/video.mp4" not in text
  assert "KCTLHTML" not in text
  assert node["products"]["convert"]["summary"]["coverage"] == [MEDIA_NOTE]
  assert export_courses(archive, [COURSE]) == 3


def test_embedded_picture_fallback_has_no_wrapper_limitation(archive):
  stream = io.BytesIO()
  with Image.new("RGB", (8, 8), "white") as image:
    image.save(stream, format="PNG")
  uri = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
  raw = (
    "<h1>Lesson</h1><picture>"
    '<source srcset="https://example.com/alternative.webp">'
    f'<img src="{uri}" alt="Measured result"></picture>'
  ).encode()
  key = add_html(archive, raw)

  node = process_html(archive, key)

  for step in ("convert", "ocr"):
    assert node["products"][step]["status"] == "complete"
    assert node["products"][step]["problems"] == []
    assert "coverage" not in node["products"][step]["summary"]

  assert node["visuals"]["status"] == "complete"
  assert len(node["visuals"]["images"]) == 1
  assert node["visuals"]["images"][0]["blank"] is True
  assert node["products"]["ocr"]["summary"]["blank"] == 1
  assert "Measured result" in source_text(archive, key)
  assert "base64" not in source_text(archive, key)
  assert export_courses(archive, [COURSE]) == 0
  assert archive.read("extracted/Course.txt", 16384).decode().endswith(
    "## Extraction notes\n\nGenerated by kctl\nSources with notes: 0\n"
  )


@pytest.mark.parametrize(
  "markup",
  [
    '<picture aria-hidden="true" title="Icon">'
    '<source srcset="https://example.com/icon.webp"></picture>',
    '<picture role="presentation">'
    '<img src="https://example.com/icon.png" alt="Icon"></picture>',
    '<picture><source srcset="https://example.com/icon.webp">'
    '<img role="none" src="https://example.com/icon.png" alt="Icon"></picture>',
  ],
)
def test_decorative_pictures_do_not_create_false_limitations(markup):
  content = prepare_html(("<p>Instructions</p>" + markup).encode())

  assert content.problems == []
  assert content.images == []
  assert b"Instructions" in content.markup
  assert b"Icon" not in content.markup
  assert b"https://example.com" not in content.markup


def test_nested_media_has_one_diagnostic_and_keeps_fallback_text():
  content = prepare_html(
    b'<object title="Example"><video><audio>Fallback explanation</audio>'
    b'</video></object>'
  )

  assert content.problems == [MEDIA_NOTE]
  assert content.markup.count(b"Example") == 1
  assert content.markup.count(b"Fallback explanation") == 1


def test_media_inspection_invalidates_old_products_and_visual_cache(
  archive, monkeypatch
):
  raw = (
    b"<p>Previously extracted source</p>"
    b'<video title="Video description" src="https://example.com/video.mp4">'
    b"</video>"
  )
  key = add_html(archive, raw)
  current = copy.deepcopy(archive.settings)
  assert current["visuals"]["html_media"] == 1
  archive.settings["visuals"].pop("html_media")
  assert archive.settings["walk"] == current["walk"]
  for step in ("convert", "ocr"):
    assert "html_media" not in archive.settings[step]["visuals"]
    assert archive.settings[step]["version"] == current[step]["version"]

  with monkeypatch.context() as patch:
    patch.setattr(conversion, "_html_media", lambda page: [])
    previous = copy.deepcopy(process_html(archive, key))

  previous_text = source_text(archive, key)
  assert previous["visuals"]["status"] == "complete"
  assert all(
    previous["products"][step]["status"] == "complete"
    for step in ("convert", "ocr")
  )
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  archive.settings = current
  assert not archive.reusable(archive.data["documents"][key], ["convert", "ocr"])

  inspected = []
  original_prepare = conversion.prepare_html

  def prepare(raw):
    inspected.append(raw)
    return original_prepare(raw)

  monkeypatch.setattr(conversion, "prepare_html", prepare)
  node = process_html(archive, key)

  assert inspected == [raw, raw]
  assert source_text(archive, key) == previous_text
  for step in ("convert", "ocr"):
    attempt = node["attempts"][step]
    assert attempt["settings"] == current[step]
    assert attempt["status"] == "partial"
    assert attempt["retained"] is True
    assert attempt["retry"] is False
    assert attempt["summary"]["coverage"] == [MEDIA_NOTE]
    assert node["products"][step]["content_sha256"] == (
      previous["products"][step]["content_sha256"]
    )

  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  assert export_courses(archive, [COURSE]) == 3
  exported = archive.read("extracted/Course.txt", 16384)
  assert MEDIA_NOTE.encode() in exported
  assert b"Previous result retained" in exported
  assert b"Sources with notes: 1" in exported

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

  assert inspected == [raw, raw]
  assert export_courses(archive, [COURSE]) == 3
  assert archive.read("extracted/Course.txt", 16384) == exported
  assert archive.read(FOLDER + "/Lesson/lesson.html", len(raw)) == raw
  assert archive.data["items"][OWNER]["documents"] == [key]
  archive.validate()
