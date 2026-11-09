import copy
import queue
import threading

from contextlib import contextmanager
from email.message import Message
from html import escape
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import kctl.retrieval as retrieval

from kctl.archive import Archive
from kctl.auth_html import soup_html
from kctl.core import CTL, FILE_LIMIT, Failure, StorageFailure, identity
from kctl.exports import export_courses
from kctl.retrieval import Producer, page_markdown


COURSE = "course/one"
OWNER = COURSE + "/page"
FOLDER = "documents/Course"
PAGE = FOLDER + "/page.md"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-image"
TEXT_URL = CTL + "/fileDownServlet?" + urlencode({
  "rFileName": "reading.txt",
  "sFileName": "stored",
  "filePath": "folder",
})


@pytest.fixture
def capture(private_dir):
  archive = Archive(private_dir / "archive", create=True)
  archive.prepare()
  archive.item("index", {
    "title": "Archive",
    "path": "documents/index.md",
    "folder": "documents",
    "status": "archived",
    "parent": None,
    "children": [COURSE],
  }, None)
  archive.item(COURSE, {
    "title": "Course",
    "path": FOLDER + "/index.md",
    "folder": FOLDER,
    "status": "archived",
    "parent": "index",
    "course_id": "one",
    "include_submissions": True,
    "children": [OWNER],
  }, None)
  archive.item(OWNER, {
    "title": "Page",
    "path": PAGE,
    "folder": FOLDER,
    "status": "archived",
    "parent": COURSE,
    "documents": [],
    "related": [],
    "problems": [],
  }, "Previous source\n")
  state = SimpleNamespace(
    archive=archive,
    responses={},
    requests=[],
    closed=0,
    include=True,
  )

  @contextmanager
  def open_response(url, headers=None):
    state.requests.append((url, headers))
    assert url in state.responses, "unexpected attachment request: " + url
    raw, mime, status = state.responses[url]
    values = Message()
    values["Content-Type"] = mime
    values["Content-Length"] = str(len(raw))

    try:
      yield SimpleNamespace(status=status, headers=values, raw=raw)
    finally:
      state.closed += 1

  def chunks(response, limit, seconds):
    assert limit == FILE_LIMIT
    assert seconds == 600
    yield response.raw

  def run(markup, **options):
    page = soup_html(markup)
    before = str(page)
    events = queue.Queue()
    producer = Producer(
      reader=SimpleNamespace(http=SimpleNamespace(
        open=open_response,
        chunks=chunks,
      )),
      root=archive.root,
      snapshot=copy.deepcopy(archive.data),
      options=SimpleNamespace(
        refresh=False,
        include_submissions=state.include,
        course=None,
      ),
      events=events,
      stop=threading.Event(),
      home=None,
    )
    producer.content(
      OWNER,
      "Page",
      PAGE,
      [page],
      CTL + "/Course.do?cmd=viewStudyHome",
      parent=COURSE,
      **options,
    )
    assert str(page) == before

    while not events.empty():
      event = events.get_nowait()
      data = event.data
      if event.kind == "document":
        archive.document(data["key"], data["record"], data["work"])
      else:
        assert event.kind == "item"
        archive.item(data["key"], data["changes"], data["body"])

    archive.validate()
    item = archive.data["items"][OWNER]
    return archive.read_content(PAGE, item).decode()

  state.run = run

  try:
    yield state
  finally:
    archive.close()


def link(url=TEXT_URL, title="Download", classes=""):
  return (
    f'<a class="{classes}" href="{escape(url, quote=True)}">'
    + title + "</a>"
  )


def documents(capture):
  archive = capture.archive
  return [
    archive.data["documents"][key]
    for key in archive.data["items"][OWNER]["documents"]
  ]


def test_source_table_survives_cleaning_publication_and_export(capture):
  body = capture.run(
    '<div class="contentTitle"><h2>Navigation heading</h2></div>'
    '<div class="cont"><p>Assignment instructions</p><div class="btnBox">'
    "<table><tr><th>Deadline</th><td>Friday</td></tr></table>"
    "</div></div>"
  )

  assert "Assignment instructions" in body
  assert "| Deadline | Friday |" in body
  assert "Navigation heading" not in body
  assert capture.requests == []
  assert export_courses(capture.archive, [COURSE]) == 0
  exported = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert "| Deadline | Friday |" in exported


def test_direct_servlet_link_preserves_parameters_and_original_filename(capture):
  raw = b"Readable attachment\n"
  capture.responses[TEXT_URL] = raw, "text/plain", 200

  body = capture.run(
    "<p>Read this first.</p>"
    + link(title="Required reading", classes="btnBox")
  )

  assert capture.requests == [(TEXT_URL, {})]
  assert capture.closed == 1
  doc, = documents(capture)
  assert doc["url"] == TEXT_URL
  assert doc["original"] == "reading.txt"
  assert doc["name"] == "reading.txt"
  assert doc["role"] == "attachment"
  assert capture.archive.read(doc["folder"] + "/reading.txt", 4096) == raw
  assert "Required reading" in body
  assert "fileDownServlet" not in body
  assert export_courses(capture.archive, [COURSE]) == 0
  assert raw in capture.archive.read("extracted/Course.txt", 16384)


def test_direct_storage_link_uses_decoded_basename(capture):
  url = CTL + "/lmsdata/notes%20file.txt?download=1"
  capture.responses[url] = b"Storage source\n", "text/plain", 200

  body = capture.run(link(url, "Read the notes"))

  assert capture.requests == [(url, {})]
  doc, = documents(capture)
  assert doc["original"] == "notes file.txt"
  assert doc["url"] == url
  assert "Read the notes" in body


def test_script_and_direct_references_share_one_download(capture):
  capture.responses[TEXT_URL] = b"Shared source\n", "text/plain", 200

  body = capture.run(
    '<a onclick="fileDownload(\'reading.txt\', \'stored\', \'folder\')">'
    "reading.txt</a>"
    + link()
    + link(title="Second reference")
  )

  assert len(capture.requests) == 1
  assert len(documents(capture)) == 1
  assert "reading.txt" in body
  assert "Second reference" in body


def test_attachment_handler_does_not_replace_its_source_container(capture):
  capture.responses[TEXT_URL] = b"Attachment text\n", "text/plain", 200

  body = capture.run(
    '<div class="boardOption" '
    'onclick="fileDownload(\'reading.txt\', \'stored\', \'folder\')">'
    "<p>Keep these instructions.</p>"
    "<table><tr><th>Deadline</th><td>Friday</td></tr></table>"
    "</div>"
  )

  assert "Keep these instructions." in body
  assert "| Deadline | Friday |" in body
  assert len(documents(capture)) == 1


@pytest.mark.parametrize(
  "control",
  [
    '<button onclick="fileDownload(\'reading.txt\', \'stored\', \'folder\')">'
    "Download reading</button>",
    '<input type="button" value="Download reading" '
    'onclick="fileDownload(\'reading.txt\', \'stored\', \'folder\')">',
  ],
)
def test_attachment_buttons_keep_labels_after_capture(capture, control):
  capture.responses[TEXT_URL] = b"Source\n", "text/plain", 200

  body = capture.run(control)

  assert "Download reading" in body
  assert len(documents(capture)) == 1


def test_direct_failed_refresh_retains_only_the_observed_attachment(capture):
  capture.responses[TEXT_URL] = b"Existing source\n", "text/plain", 200
  capture.run(link())
  key = identity(OWNER, TEXT_URL)
  capture.archive.data["documents"][key]["etag"] = '"previous"'
  capture.responses[TEXT_URL] = b"upstream failed", "text/plain", 500

  body = capture.run("<p>Current instructions.</p>" + link())

  item = capture.archive.data["items"][OWNER]
  assert item["documents"] == [key]
  assert item["status"] == "partial"
  assert any("HTTP 500" in problem for problem in item["problems"])
  assert capture.requests[-1] == (
    TEXT_URL, {"If-None-Match": '"previous"'}
  )
  assert "Current instructions." in body
  assert export_courses(capture.archive, [COURSE]) == 3
  assert b"Existing source" in capture.archive.read("extracted/Course.txt", 16384)

  capture.run("<p>The attachment was removed.</p>")
  assert capture.archive.data["items"][OWNER]["documents"] == []
  assert key in capture.archive.data["documents"]


@pytest.mark.parametrize("policy", ["submissions", "files"])
def test_exclusion_applies_to_direct_links_and_images_without_false_failures(
  capture, policy
):
  if policy == "submissions":
    capture.include = False

  body = capture.run(
    "<p>Submission status: submitted</p>"
    + link(title="Submitted file", classes="btnBox")
    + '<img src="/lmsdata/result.png" alt="Result diagram">',
    role="submission",
    include_files=policy != "files",
  )

  assert capture.requests == []
  assert documents(capture) == []
  assert "Submission status: submitted" in body
  assert "Submitted file" in body
  assert "Result diagram" in body
  item = capture.archive.data["items"][OWNER]
  assert item["status"] == "archived"
  assert item["problems"] == []


def test_exclusion_drops_old_submission_references_without_deleting_originals(
  capture
):
  capture.responses[TEXT_URL] = b"Private submitted work\n", "text/plain", 200
  capture.run(link(), role="submission")
  doc, = documents(capture)
  capture.requests.clear()
  capture.include = False

  capture.run("<p>Submitted</p>" + link(), role="submission")

  assert capture.requests == []
  assert documents(capture) == []
  assert capture.archive.read(doc["folder"] + "/reading.txt", 4096) == (
    b"Private submitted work\n"
  )


def test_source_image_outside_cont_is_captured(capture):
  url = CTL + "/lmsdata/result.png"
  capture.responses[url] = PNG, "image/png", 200

  body = capture.run(
    '<div class="boardOption"><div class="btnBox">'
    '<img src="/lmsdata/result.png" alt="Measured result"></div></div>'
  )

  assert body == "Measured result\n"
  doc, = documents(capture)
  assert doc["url"] == url
  assert doc["original"] == "result.png"
  assert capture.archive.read(doc["folder"] + "/result.png", 4096) == PNG
  assert capture.archive.data["items"][OWNER]["problems"] == []


def test_linked_image_capture_keeps_description_and_deduplicates_original(capture):
  url = CTL + "/lmsdata/result.png"
  capture.responses[url] = PNG, "image/png", 200

  body = capture.run(
    '<a class="btnBox" href="/lmsdata/result.png">'
    '<img src="/lmsdata/result.png" alt="Result diagram"></a>'
  )

  assert capture.requests == [(url, {})]
  assert len(documents(capture)) == 1
  assert "Result diagram" in body


@pytest.mark.parametrize(
  "attribute",
  ['aria-hidden="true"', 'role="presentation"', 'role="none"'],
)
def test_explicitly_decorative_images_are_not_downloaded(capture, attribute):
  body = capture.run(
    "<p>Source text</p>"
    f'<img src="/lmsdata/icon.png" alt="Decorative icon" {attribute}>'
  )

  assert capture.requests == []
  assert documents(capture) == []
  assert "Decorative icon" not in body
  assert capture.archive.data["items"][OWNER]["problems"] == []


def test_empty_alt_is_not_used_as_an_image_quality_heuristic(capture):
  url = CTL + "/lmsdata/result.png"
  capture.responses[url] = PNG, "image/png", 200

  body = capture.run('<img src="/lmsdata/result.png" alt="">')

  assert body == "\n"
  assert len(documents(capture)) == 1
  assert capture.archive.data["items"][OWNER]["status"] == "archived"


@pytest.mark.parametrize(
  "image",
  [
    '<img src="https://example.com/result.png" alt="Result">',
    '<img src="data:image/png;base64,AAAA" alt="Result">',
    '<img src="/other/result.png" alt="Result">',
    '<img srcset="/lmsdata/large.png 2x" alt="Result">',
    '<img src="/lmsdata/%2e%2e/private.png" alt="Result">',
  ],
)
def test_unsupported_source_images_are_reported_without_fetching(capture, image):
  body = capture.run(image)

  assert "Result" in body
  assert capture.requests == []
  assert documents(capture) == []
  item = capture.archive.data["items"][OWNER]
  assert item["status"] == "partial"
  assert item["empty_listing"] is False
  assert any("inline image not archived" in problem for problem in item["problems"])


@pytest.mark.parametrize("tag", ["iframe", "object", "canvas", "svg", "video"])
def test_retained_media_without_text_is_a_reported_gap(capture, tag):
  body = capture.run(f"<{tag}></{tag}>")

  assert body == "\n"
  assert capture.requests == []
  item = capture.archive.data["items"][OWNER]
  assert item["status"] == "partial"
  assert item["problems"] == ["embedded CTL content is not archived"]


def test_media_description_survives_without_a_fetch(capture):
  body = capture.run(
    '<div class="btnBox"><iframe src="https://example.com/exercise" '
    'title="Interactive exercise"></iframe></div>'
  )

  assert "Interactive exercise" in body
  assert capture.requests == []
  assert capture.archive.data["items"][OWNER]["status"] == "partial"


def test_blank_transport_frame_does_not_create_a_content_limitation(capture):
  body = capture.run(
    "<p>Complete source</p>"
    '<div class="listFooter"><iframe name="hiddenFrame"></iframe></div>'
  )

  assert "Complete source" in body
  assert capture.requests == []
  assert capture.archive.data["items"][OWNER]["problems"] == []


def test_external_download_link_remains_a_reference_not_a_network_request(capture):
  url = "https://example.com/fileDownServlet?id=guide"

  body = capture.run(link(url, "External reference"))

  assert url in body
  assert "External reference" in body
  assert capture.requests == []
  assert documents(capture) == []


def test_direct_download_parameters_are_not_filtered_by_page_markdown():
  problems = []
  body = page_markdown(soup_html(link()), problems)

  assert TEXT_URL in body
  assert "sFileName=stored" in body
  assert "filePath=folder" in body
  assert problems == []


def test_table_caption_and_heading_association_remain_in_source_order(capture):
  body = capture.run(
    "<h2>Assignments</h2>"
    "<table><caption>Required work</caption>"
    "<tr><th>Task</th><th>Deadline</th></tr>"
    "<tr><td>Report</td><td>Friday</td></tr></table>"
    "<h2>Schedule</h2>"
    "<table><tr><th>Week</th><th>Topic</th></tr>"
    "<tr><td>One</td><td>Introduction</td></tr></table>"
  )

  assert body.index("Assignments") < body.index("Required work")
  assert body.index("Required work") < body.index("| Report | Friday |")
  assert body.index("| Report | Friday |") < body.index("Schedule")
  assert body.index("Schedule") < body.index("| One | Introduction |")
  assert body.count("Required work") == 1
  assert "KCTLTABLE" not in body


def test_table_cell_blocks_do_not_split_rows_into_paragraphs(capture):
  body = capture.run(
    "<table><tr><th>Period</th><th>Status</th></tr>"
    "<tr><td><p>Monday</p><p>Friday</p></td>"
    "<td><div>Not submitted</div></td></tr></table>"
  )

  lines = body.splitlines()
  row = next(line for line in lines if "Monday" in line)
  assert "Friday" in row
  assert "Not submitted" in row
  assert "<br>" in row
  assert "| --- | --- |" in body


def test_header_only_table_has_a_valid_separator(capture):
  body = capture.run(
    "<table><tr><th>Week</th><th>Attendance</th></tr></table>"
  )

  assert "| Week | Attendance |\n| --- | --- |" in body


def test_headerless_table_does_not_invent_column_names(capture):
  body = capture.run(
    "<table><tr><td>Week 1</td><td>Present</td></tr></table>"
  )

  assert "|  |  |\n| --- | --- |\n| Week 1 | Present |" in body


def test_complex_table_preserves_preformatted_source_and_nested_content(capture):
  body = capture.run(
    "<table><caption>Examples</caption><tr><td colspan='2'>"
    "<pre>def example():\n  return 1\n</pre></td></tr>"
    "<tr><td>Nested</td><td>"
    "<table><tr><td>Inner value</td></tr></table>"
    "</td></tr></table>"
  )

  assert "def example():\n  return 1" in body
  assert body.count("Inner value") == 1
  assert body.count("Examples") == 1
  assert "KCTLTABLE" not in body


def test_table_inline_links_and_pipes_remain_in_their_cell(capture):
  body = capture.run(
    "<table><tr><th>Expression</th><th>Reference</th></tr>"
    "<tr><td><code>a | b</code></td>"
    '<td><a href="https://example.com/reference">Details</a></td></tr></table>'
  )

  row = next(line for line in body.splitlines() if "Details" in line)
  assert r"a \| b" in row
  assert "https://example.com/reference" in row


def test_page_text_limit_is_applied_after_table_expansion(capture, monkeypatch):
  monkeypatch.setattr(retrieval, "TEXT_LIMIT", 20)

  with pytest.raises(Failure, match="text limit"):
    capture.run(
      "<table><tr><th>Heading</th><td>Source value</td></tr></table>"
    )

  assert capture.archive.read_content(
    PAGE, capture.archive.data["items"][OWNER]
  ) == b"Previous source\n"


def test_capture_does_not_soften_storage_failure(capture, monkeypatch):
  capture.responses[TEXT_URL] = b"Source\n", "text/plain", 200

  def fail(path):
    raise StorageFailure("output device failed")

  monkeypatch.setattr(retrieval, "signature", fail)

  with pytest.raises(StorageFailure, match="output device failed"):
    capture.run(link())

  assert documents(capture) == []
  assert capture.archive.read_content(
    PAGE, capture.archive.data["items"][OWNER]
  ) == b"Previous source\n"


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_external_reference_in_navigation_panel_reaches_export(capture, scheme):
  url = scheme + "://example.com/required-reading.pdf"
  body = capture.run(
    "<p>Assignment instructions</p>"
    '<div class="boardTab">'
    + link(url, "Required reading", "btnBox")
    + "</div>"
  )

  assert "Assignment instructions" in body
  assert "Required reading" in body
  assert url in body
  assert capture.requests == []
  assert documents(capture) == []
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  assert url in capture.archive.read("extracted/Course.txt", 16384).decode()


@pytest.mark.parametrize(
  "route",
  ["/Course.do", "/Unknown.do", "/reference.jsp", "/fileDownServletExtra"],
)
def test_nonattachment_ctl_links_keep_query_filtering(capture, route):
  url = CTL + route + ";jsessionid=path-secret?" + urlencode({
    "cmd": "viewReference",
    "token": "private-value",
    "context": "private-context",
  })

  body = capture.run(link(url, "Source reference", "btnBox"))

  assert "Source reference" in body
  assert CTL + route + "?cmd=viewReference" in body
  assert "path-secret" not in body
  assert "private-value" not in body
  assert "private-context" not in body
  assert capture.requests == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert "private-value" not in text
  assert "private-context" not in text


def test_session_normalized_attachment_link_keeps_download_parameters():
  url = TEXT_URL.replace("/fileDownServlet?", "/fileDownServlet;jsessionid=x?")
  problems = []

  body = page_markdown(soup_html(link(url)), problems)

  assert TEXT_URL in body
  assert "jsessionid" not in body
  assert problems == []


@pytest.mark.parametrize(
  ("role", "include", "expected"),
  [
    ("attachment", True, 1),
    ("attachment", False, 1),
    ("submission", True, 1),
    ("submission", False, 0),
  ],
)
@pytest.mark.parametrize("picture", [False, True])
def test_image_download_control_captures_attachment_not_button_icon(
  capture, role, include, expected, picture
):
  url = CTL + "/fileDownServlet?" + urlencode({
    "rFileName": "lecture.pdf",
    "sFileName": "stored",
    "filePath": "folder",
  })
  raw = b"%PDF-synthetic attachment"
  capture.responses[url] = raw, "application/pdf", 200
  capture.include = include
  control = (
    '<img class="btnBox" src="/images/download.gif" alt="Download lecture" '
    'onclick="fileDownload(\'lecture.pdf\', \'stored\', \'folder\')">'
  )
  if picture:
    control = (
      '<picture><source srcset="/images/download-large.gif">'
      + control + "</picture>"
    )

  body = capture.run(
    "<p>Submission status: submitted</p>" + control,
    role=role,
  )

  assert "Submission status: submitted" in body
  assert "Download lecture" in body
  assert len(capture.requests) == expected
  assert len(documents(capture)) == expected
  assert capture.archive.data["items"][OWNER]["problems"] == []
  if expected:
    doc, = documents(capture)
    assert capture.requests == [(url, {})]
    assert doc["name"] == "lecture.pdf"
    assert doc["original"] == "lecture.pdf"
    assert doc["role"] == role
    assert capture.archive.read(doc["folder"] + "/lecture.pdf", 4096) == raw


def test_image_control_and_direct_reference_share_attachment_identity(capture):
  capture.responses[TEXT_URL] = b"Shared source\n", "text/plain", 200
  body = capture.run(
    '<img src="/images/download.gif" alt="Download reading" '
    'onclick="fileDownload(\'reading.txt\', \'stored\', \'folder\')">'
    + link(title="Direct reference")
  )

  assert capture.requests == [(TEXT_URL, {})]
  assert len(documents(capture)) == 1
  assert "Download reading" in body
  assert "Direct reference" in body
  assert capture.archive.data["items"][OWNER]["problems"] == []


@pytest.mark.parametrize("include", [False, True])
def test_picture_fallback_is_not_a_separate_missing_resource(capture, include):
  url = CTL + "/lmsdata/result.png"
  capture.responses[url] = PNG, "image/png", 200
  capture.include = include

  body = capture.run(
    '<div class="boardOption"><picture>'
    '<source srcset="/lmsdata/large.png 2x">'
    '<img src="/lmsdata/result.png" alt="Measured result">'
    "</picture></div>",
    role="submission",
  )

  assert body == "Measured result\n"
  assert len(capture.requests) == int(include)
  assert len(documents(capture)) == int(include)
  item = capture.archive.data["items"][OWNER]
  assert item["status"] == "archived"
  assert item["problems"] == []
  if include:
    assert capture.requests == [(url, {})]


def test_decorative_picture_does_not_create_a_false_limitation(capture):
  body = capture.run(
    "<p>Source instructions</p><picture>"
    '<source srcset="/images/large-icon.png">'
    '<img src="/images/icon.png" role="presentation" alt="Icon">'
    "</picture>"
  )

  assert body == "Source instructions\n"
  assert capture.requests == []
  assert documents(capture) == []
  assert capture.archive.data["items"][OWNER]["problems"] == []


@pytest.mark.parametrize("include", [False, True])
def test_source_only_picture_reports_unsupported_selection_unless_excluded(
  capture, include
):
  capture.include = include

  body = capture.run(
    "<p>Submission status: submitted</p>"
    '<picture><source srcset="/lmsdata/result.png"></picture>',
    role="submission",
  )

  assert "Submission status: submitted" in body
  assert capture.requests == []
  assert documents(capture) == []
  item = capture.archive.data["items"][OWNER]
  assert item["status"] == ("partial" if include else "archived")
  assert item["problems"] == (
    ["picture has no supported fallback image"] if include else []
  )


def test_unsupported_picture_fallback_has_one_image_limitation(capture):
  body = capture.run(
    '<picture><source srcset="https://example.com/large.png">'
    '<img src="https://example.com/result.png" alt="Result"></picture>'
  )

  assert body == "Result\n"
  assert capture.requests == []
  problems = capture.archive.data["items"][OWNER]["problems"]
  assert len(problems) == 1
  assert "inline image not archived" in problems[0]


@pytest.mark.parametrize(
  "unwrapped",
  [
    "<tbody><td>Submitted</td></tbody>",
    "<td>Submitted</td>",
    "<tbody><div><td>Submitted</td></div></tbody>",
  ],
)
def test_unwrapped_table_cells_survive_publication_and_export(capture, unwrapped):
  body = capture.run(
    "<h2>Receipt</h2>"
    "<table><caption>Submission record</caption>"
    "<tr><th>Submission status</th></tr>"
    + unwrapped
    + "</table><p>Following instructions</p>"
  )

  assert "```" in body
  assert body.count("Submission record") == 1
  assert body.count("Submission status") == 1
  assert body.count("Submitted") == 1
  assert body.index("Submission status") < body.index("Submitted")
  assert body.index("Submitted") < body.index("Following instructions")
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert "Submitted" in text
  assert "Submission record" in text


def test_nested_table_cells_do_not_trigger_false_unaccounted_cell_detection(
  capture
):
  body = capture.run(
    "<table><caption>Outer table</caption>"
    "<tr><td>Outer value</td><td>"
    "<table><tr><td>Nested value</td></tr></table>"
    "</td></tr></table>"
  )

  assert body.count("Outer table") == 1
  assert body.count("Outer value") == 1
  assert body.count("Nested value") == 1
  assert "```" in body
  assert capture.archive.data["items"][OWNER]["problems"] == []


@pytest.mark.parametrize("header", ["", "<tr><th>References</th></tr>"])
@pytest.mark.parametrize(
  ("url", "expected"),
  [
    (
      "https://example.com/required-reading.pdf?edition=2&part=1",
      "https://example.com/required-reading.pdf?edition=2&part=1",
    ),
    (
      "/Reference.do;jsessionid=path-secret?cmd=viewReference&token=private-value",
      CTL + "/Reference.do?cmd=viewReference",
    ),
  ],
)
def test_malformed_table_references_survive_capture_and_export(
  capture, header, url, expected
):
  body = capture.run(
    "<h2>Resources</h2><table><caption>Reading list</caption>"
    + header
    + "<tbody><td>"
    + link(url, "<strong>Required reading</strong>")
    + "</td><td><pre>def example():\n  return 1\n</pre></td></tbody>"
    "</table><p>Following instructions</p>"
  )

  assert "```" in body
  assert body.count("Reading list") == 1
  assert body.count("Required reading") == 1
  assert body.count(expected) == 1
  assert "def example():\n  return 1" in body
  assert body.index("Reading list") < body.index("Required reading")
  assert body.index("Required reading") < body.index(expected)
  assert body.index(expected) < body.index("def example")
  assert body.index("def example") < body.index("Following instructions")
  assert "path-secret" not in body
  assert "private-value" not in body
  assert capture.requests == []
  assert documents(capture) == []
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert "Required reading" in text
  assert text.count(expected) == 1
  assert "path-secret" not in text
  assert "private-value" not in text


@pytest.mark.parametrize("visible", [False, True])
def test_malformed_table_keeps_url_only_and_empty_reference_labels(
  capture, visible
):
  url = "https://example.com/reading.pdf"
  body = capture.run(
    "<table><tbody><td>"
    + link(url, url if visible else "")
    + "</td></tbody></table>"
  )

  assert body.count(url) == 1
  assert capture.requests == []
  assert export_courses(capture.archive, [COURSE]) == 0
  assert capture.archive.read("extracted/Course.txt", 16384).decode().count(url) == 1


def test_malformed_table_does_not_restore_executable_link_targets(capture):
  body = capture.run(
    "<table><tr><th>Instructions</th></tr><tbody><td>"
    + link("javascript:showRequiredMaterial()", "Required material")
    + "</td></tbody></table>"
  )

  assert "Required material" in body
  assert "javascript:" not in body
  assert "showRequiredMaterial" not in body
  assert capture.requests == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert "Required material" in text
  assert "showRequiredMaterial" not in text


@pytest.mark.parametrize("spanning", [False, True])
def test_table_caption_links_survive_capture_and_export(capture, spanning):
  url = "https://example.com/required-reading.pdf?edition=2"
  attribute = ' colspan="2"' if spanning else ""
  body = capture.run(
    "<h2>Resources</h2><table><caption>"
    + link(url, "<strong>Required reading</strong>")
    + "</caption><tr><th>Task</th><th>Status</th></tr>"
    + f"<tr><td{attribute}>Review the document</td>"
    + ("" if spanning else "<td>Required</td>")
    + "</tr></table><p>Following instructions</p>"
  )

  assert body.count("Required reading") == 1
  assert body.count(url) == 1
  assert "**Required reading**" in body
  assert body.index("Resources") < body.index("Required reading")
  assert body.index(url) < body.index("Review the document")
  assert body.index("Review the document") < body.index("Following instructions")
  assert ("```" in body) is spanning
  assert capture.requests == []
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert text.count(url) == 1
  assert "Required reading" in text
  assert "Review the document" in text


@pytest.mark.parametrize(
  "content",
  [
    "<tr><td>Submitted</td></tr>Deadline: Friday",
    "<tr><td>Submitted</td>Deadline: Friday</tr>",
    "<tbody><tr><td>Submitted</td></tr>Deadline: Friday</tbody>",
  ],
)
def test_table_text_outside_cells_survives_capture_and_export(capture, content):
  body = capture.run(
    "<h2>Receipt</h2><table><caption>Submission record</caption>"
    "<tr><th>Status</th></tr>"
    + content
    + "</table><p>Following instructions</p>"
  )

  assert "```" in body
  assert body.count("Submission record") == 1
  assert body.count("Submitted") == 1
  assert body.count("Deadline: Friday") == 1
  assert body.index("Submission record") < body.index("Status")
  assert body.index("Status") < body.index("Submitted")
  assert body.index("Submitted") < body.index("Deadline: Friday")
  assert body.index("Deadline: Friday") < body.index("Following instructions")
  assert capture.requests == []
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert text.count("Deadline: Friday") == 1
  assert "Submitted" in text


def test_table_reference_outside_cells_keeps_its_filtered_destination(capture):
  url = "/Reference.do?cmd=viewReference&token=private-value"
  expected = CTL + "/Reference.do?cmd=viewReference"
  body = capture.run(
    "<table><caption>References</caption>"
    "<tr><th>Task</th></tr><tr><td>Review the document</td></tr>"
    + link(url, "Required reference")
    + "</table>"
  )

  assert "```" in body
  assert body.count("Required reference") == 1
  assert body.count(expected) == 1
  assert body.index("Review the document") < body.index("Required reference")
  assert "private-value" not in body
  assert capture.requests == []
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert text.count(expected) == 1
  assert "private-value" not in text


def test_table_layout_whitespace_does_not_trigger_text_fallback(capture):
  body = capture.run(
    "<table>\n"
    "  <caption>Submission record</caption>\n"
    "  <thead>\n"
    "    <tr><th>Status</th><th>Deadline</th></tr>\n"
    "  </thead>\n"
    "  <tbody>\n"
    "    <tr><td>Submitted</td><td>Friday</td></tr>\n"
    "  </tbody>\n"
    "</table>"
  )

  assert "```" not in body
  assert "| Status | Deadline |" in body
  assert "| --- | --- |" in body
  assert "| Submitted | Friday |" in body
  assert body.count("Submission record") == 1
  assert capture.archive.data["items"][OWNER]["problems"] == []
  assert export_courses(capture.archive, [COURSE]) == 0
  text = capture.archive.read("extracted/Course.txt", 16384).decode()
  assert "| Submitted | Friday |" in text
