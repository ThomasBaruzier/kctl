import copy
import queue
import threading

from html import escape
from types import SimpleNamespace

import pytest

import kctl.retrieval as retrieval

from kctl.archive import Archive
from kctl.auth_html import soup_html
from kctl.core import CTL, Failure
from kctl.courses import CourseTraversal, _empty_listing
from kctl.exports import export_courses
from kctl.reader import Reader, clean_content
from kctl.retrieval import Producer, _summary_table_rows, page_markdown


COURSE = "course/one"
BOARD = COURSE + "/board"
PAGE = COURSE + "/page"
FOLDER = "documents/Course"
REFERENCE = "https://example.com/reading?part=1&edition=2"


def listing(control, extra=""):
  return soup_html(
    '<form name="courseForm">'
    '<input type="hidden" name="courseDTO.courseId" value="one">'
    '<input type="hidden" name="boardInfoDTO.boardInfoId" value="board">'
    '<div id="bodyContent"><div class="boardOption">'
    '<input type="button" value="Search" onclick="javascript:searchSubmit();">'
    + control
    + '<a href="javascript:viewStudyHome()">Home</a></div>'
    '<div id="listBox"><div id="listBox">'
    '<div class="search_unfine">No data.</div>'
    "</div></div>"
    + extra
    + "</div></form>"
  )


def summary_table():
  return (
    '<table class="boardListInfo"><caption>Assignment information</caption>'
    "<thead><tr><th>Period</th><th>Grade date</th>"
    "<th>Late submission</th><th>Status</th></tr></thead>"
    "<tbody>\n\t<!-- layout -->\n"
    "<td>\n\tMonday - Friday\n\t</td>"
    "<td>\n\t</td><td>\n\tNot allowed\n\t</td>"
    '<td><a href="'
    + escape(REFERENCE, quote=True)
    + '">Submitted</a></td>'
    '<div class="clear"></div></tbody></table>'
  )


@pytest.fixture
def capture(private_dir):
  archive = Archive(private_dir / "archive", create=True)
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
    "course_id": "one",
    "include_submissions": True,
    "children": [BOARD, PAGE],
    "parent": "index",
  }, None)
  archive.item(BOARD, {
    "title": "Board",
    "path": FOLDER + "/Board/index.md",
    "folder": FOLDER + "/Board",
    "status": "failed",
    "message": "unrecognized board layout",
    "children": [BOARD + "/old"],
    "parent": COURSE,
  }, None)
  archive.item(PAGE, {
    "title": "Assignments",
    "path": FOLDER + "/page.md",
    "folder": FOLDER,
    "status": "archived",
    "documents": [],
    "parent": COURSE,
  }, "Previous instructions\n")
  events = queue.Queue()
  reads = []
  response = listing(
    '<a href="javascript:addBoardContentsForm()">Write</a>'
  )

  def read(url, form):
    reads.append((url, form))
    return response

  producer = Producer(
    reader=SimpleNamespace(course="one", page=read),
    root=archive.root,
    snapshot=copy.deepcopy(archive.data),
    options=SimpleNamespace(
      course=None, refresh=False, include_submissions=True,
    ),
    events=events,
    stop=threading.Event(),
    home=None,
  )

  def publish():
    while not events.empty():
      event = events.get_nowait()
      assert event.kind == "item"
      data = event.data
      archive.item(data["key"], data["changes"], data["body"])
    archive.validate()

  try:
    yield SimpleNamespace(
      archive=archive,
      producer=producer,
      publish=publish,
      reads=reads,
      response=response,
    )
  finally:
    archive.close()


@pytest.mark.parametrize(
  "handler",
  [
    "addBoardContentsForm()",
    "addBoardContentsForm ( )",
    "return addBoardContentsForm();",
  ],
)
@pytest.mark.parametrize("attribute", ["href", "onclick"])
def test_empty_board_accepts_exact_zero_argument_create_control(
  handler, attribute
):
  value = "javascript:" + handler if attribute == "href" else handler
  page = listing(f'<a {attribute}="{value}">Write</a>')
  before = str(page)

  assert _empty_listing(page)
  assert str(page) == before
  assert clean_content(page).select_one(".boardOption") is None


@pytest.mark.parametrize(
  "handler",
  [
    "searchSubmit()",
    "javascript:searchSubmit();",
    "JavaScript:searchSubmit();",
    " javascript: searchSubmit(); ",
    "javascript:return searchSubmit();",
  ],
)
def test_empty_board_accepts_known_prefixed_search_input(handler):
  page = listing(
    '<a href="javascript:addBoardContentsForm()">Write</a>'
  )
  page.select_one("input[type=button]")["onclick"] = handler
  before = str(page)

  assert _empty_listing(page)
  assert str(page) == before
  assert clean_content(page).select_one(".boardOption") is None


@pytest.mark.parametrize(
  "control",
  [
    '<a href="javascript:addBoardContentsForm(\'one\')">Write</a>',
    '<a href="javascript:addBoardContentsForm();unknown()">Write</a>',
    '<a href="javascript:other.addBoardContentsForm()">Write</a>',
    '<a href="javascript:addBoardContentsFormExtra()">Write</a>',
    '<a href="javascript:addBoardContentsForm()" onclick="unknown()">Write</a>',
    '<a href="/Course.do" onclick="unknown()">Open</a>',
    '<button onclick="unknown()">Open</button>',
    '<div onclick="unknown()"><a href="#top">Open</a></div>',
    '<input type="button" onclick="javascript:unknown()" value="Open">',
    '<input type="button" onclick="javascript:searchSubmit();unknown()">',
    '<input type="button" onclick="javascript:javascript:searchSubmit()">',
    '<input type="button" onclick="return javascript:searchSubmit()">',
    '<button onclick="javascript:addBoardContentsForm(\'one\')">Write</button>',
  ],
)
def test_unknown_or_competing_controls_prevent_empty_board_recognition(control):
  page = listing(control)

  assert not _empty_listing(page)
  assert clean_content(page).select_one(".boardOption") is not None


def test_unknown_prefixed_handler_on_panel_prevents_empty_board_recognition():
  page = listing(
    '<a href="javascript:addBoardContentsForm()">Write</a>'
  )
  page.select_one(".boardOption")["onclick"] = "javascript:unknown()"

  assert not _empty_listing(page)
  assert clean_content(page).select_one(".boardOption") is not None


@pytest.mark.parametrize(
  "extra",
  [
    "<p>Required instructions</p>",
    '<a href="https://example.com/reading">Required reading</a>',
    "<table><tr><td>Source value</td></tr></table>",
    '<img src="/lmsdata/source.png">',
    '<div class="cont"><a href="javascript:addBoardContentsForm()">'
    "Source example</a></div>",
  ],
)
def test_recognized_create_control_does_not_hide_source_content(extra):
  page = listing(
    '<a href="javascript:addBoardContentsForm()">Write</a>', extra
  )

  assert not _empty_listing(page)


def test_create_control_remains_an_unapproved_network_operation():
  reader = Reader.__new__(Reader)
  reader.course = "one"

  with pytest.raises(Failure, match="unsupported CTL read"):
    reader.request(
      CTL + "/Course.do",
      [
        ("cmd", "addBoardContentsForm"),
        ("courseDTO.courseId", "one"),
      ],
    )


def test_empty_board_and_assignment_summary_reach_publication_and_export(capture):
  url = (
    CTL + "/Course.do?cmd=viewBoardContentsList"
    "&courseDTO.courseId=one&boardInfoDTO.boardInfoId=board"
  )
  before = str(capture.response)
  CourseTraversal(capture.producer).board(
    url, BOARD, "Board", FOLDER + "/Board", COURSE
  )
  capture.producer.content(
    PAGE,
    "Assignments",
    FOLDER + "/page.md",
    [soup_html("<h2>Instructions</h2>" + summary_table())],
    CTL + "/Report.do?cmd=viewReportInfoPageList",
    parent=COURSE,
  )
  capture.publish()

  assert capture.reads == [(url, "courseForm")]
  assert str(capture.response) == before
  assert capture.archive.data["items"][BOARD]["status"] == "empty"
  assert capture.archive.data["items"][BOARD]["children"] == []
  assert capture.archive.data["items"][PAGE]["problems"] == []
  assert capture.archive.data["items"][PAGE]["documents"] == []
  assert COURSE not in capture.archive.incomplete()
  assert export_courses(capture.archive, [COURSE]) == 0

  raw = capture.archive.read("extracted/Course.txt", 16384)
  text = raw.decode()
  assert "| Period | Grade date | Late submission | Status |" in text
  assert "| Monday - Friday |  | Not allowed | [Submitted](" in text
  assert REFERENCE in text
  assert "Assignment information" in text
  assert "```" not in text
  assert "\t" not in text
  assert "unrecognized board layout" not in text
  assert "addBoardContentsForm" not in text
  assert "searchSubmit" not in text
  assert text.endswith(
    "## Extraction notes\n\nGenerated by kctl\nSources with notes: 0\n"
  )

  assert export_courses(capture.archive, [COURSE]) == 0
  assert capture.archive.read("extracted/Course.txt", 16384) == raw


def test_summary_recognition_does_not_mutate_source():
  table = soup_html(summary_table()).table
  before = str(table)

  rows = _summary_table_rows(table)

  assert rows is not None
  assert [len(row) for row in rows] == [4, 4]
  assert rows[1][1].get_text(strip=True) == ""
  assert str(table) == before


@pytest.mark.parametrize(
  ("old", "new"),
  [
    ('class="boardListInfo"', 'class="other"'),
    ("<thead>", ""),
    ("</thead>", ""),
    ("<td>\n\t</td>", ""),
    ("</tbody>", "<td>Extra value</td></tbody>"),
    ("</tbody>", "<tr><td>Extra row</td></tr></tbody>"),
    ("</thead>", "<tr><th>Extra heading</th></tr></thead>"),
    ("</table>", "<tbody><td>Other body</td></tbody></table>"),
    ("<th>Period</th>", "<td>Period</td>"),
    ("<td>\n\t</td>", '<td colspan="1"></td>'),
    (
      '<div class="clear"></div>',
      '<div class="clear">Required outside text</div>',
    ),
    (
      '<div class="clear"></div>',
      '<div class="clear"><a href="https://example.com/">Reference</a></div>',
    ),
    (
      '<div class="clear"></div>',
      '<div class="clear" onclick="unknown()"></div>',
    ),
    ("<td>\n\t</td>", "<div><td></td></div>"),
    ("<td>\n\t</td>", "<td><pre>  source\n    line</pre></td>"),
    (
      "<td>\n\t</td>",
      "<td><table><tr><td>Nested source</td></tr></table></td>",
    ),
  ],
)
def test_other_malformed_summary_layouts_keep_the_existing_fallback(old, new):
  markup = summary_table().replace(old, new)
  table = soup_html(markup).table
  assert _summary_table_rows(table) is None

  text = page_markdown(clean_content(soup_html(markup)), [])

  assert "```" in text
  assert "Monday - Friday" in text
  assert "Submitted" in text
  assert REFERENCE in text
  if "Required outside text" in markup:
    assert "Required outside text" in text
  if "<pre>" in markup:
    assert "  source\n    line" in text
  if "Nested source" in markup:
    assert text.count("Nested source") == 1


def test_summary_expansion_limit_is_checked_before_cell_conversion(monkeypatch):
  import markdownify

  monkeypatch.setattr(retrieval, "TEXT_LIMIT", 20)

  def unexpected(*args, **kwargs):
    pytest.fail("oversized summary reached cell conversion")

  monkeypatch.setattr(markdownify, "markdownify", unexpected)

  with pytest.raises(Failure, match="text limit"):
    page_markdown(soup_html(summary_table()), [])
