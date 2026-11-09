import copy
import queue
import threading

from collections import deque
from html import escape
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import kctl.courses as courses_module

from kctl.auth_html import soup_html
from kctl.core import CTL, Failure, StorageFailure, identity
from kctl.courses import MENUS, CourseTraversal, verify_receipt
from kctl.reader import Home, changed_fields, form_fields, source_url
from kctl.retrieval import Producer


COURSE = "course-1"
ROOT = "course/" + COURSE
FOLDER = "documents/Course"
TEAM = {
  "courseDTO.courseId": COURSE,
  "teamactInfoDTO.teamactId": "activity-1",
  "teamactInfoDTO.teamCategoryId": "category-1",
  "teamDTO.teamInfoId": "team-1",
}
AREA_FIELD = "teamactActivityDTO.activityId"


def form(name, fields=None, body=""):
  values = {"courseDTO.courseId": COURSE, **(fields or {})}
  controls = "".join(
    f'<input type="hidden" name="{escape(key)}" '
    f'value="{escape(value, quote=True)}">'
    for key, value in values.items()
  )
  return f'<form name="{name}">{controls}{body}</form>'


def page(body, name="courseForm", fields=None):
  return soup_html(form(
    name, fields, '<div id="bodyContent">' + body + "</div>"
  ))


def box(body):
  return '<div id="listBox">' + body + "</div>"


def card(title, body, link=""):
  return (
    '<dl class="element">'
    f"<dt><h4>{escape(title)}</h4>{link}</dt>"
    f'<dd><div class="cont">{body}</div></dd>'
    "</dl>"
  )


def action(name, *values, title="Open"):
  args = ", ".join("'" + value + "'" for value in values)
  return f'<a onclick="{name}({args})">{escape(title)}</a>'


def download_link(name="submission.pdf"):
  return action(
    "fileDownload", "display", "stored", "folder", title=name
  )


def receipt_card(team=False, attachments="", output="Output text"):
  rows = (
    "<tr><th>제출상태</th><td>Submitted</td></tr>"
    f"<tr><th>첨부파일</th><td>{attachments}</td></tr>"
  )
  if team:
    rows += f"<tr><th>산출물내용</th><td>{output}</td></tr>"

  return (
    '<dl class="element"><dt>제출정보</dt><dd>'
    f'<div class="cont"><table>{rows}</table></div>'
    "</dd></dl>"
  )


def empty_output_card():
  return (
    '<dl class="element"><dt>제출정보</dt><dd>'
    '<table class="boardListWrite" summary="산출물 제출 정보">'
    '<tr><td colspan="2">제출 내역이 없습니다.</td></tr>'
    "</table></dd></dl>"
  )


def own_team_page(options=("area-1",), fields=None):
  selector = (
    f'<select name="{AREA_FIELD}">'
    + "".join(
      f'<option value="{value}">Area {value}</option>'
      for value in options
    )
    + "</select>"
  )
  information = (
    '<dl class="element"><dt>Activity</dt><dd>'
    '<table class="boardListInfo">'
    "<tr><th>Instructions</th><td>Team instructions</td></tr>"
    "</table></dd></dl>"
  )
  roster = (
    '<dl class="studyTarget"><dd>'
    '<table class="boardListBasic">'
    "<tr><th>Members</th><td>Own team member</td></tr>"
    "</table></dd></dl>"
  )
  return page(
    selector + box(
      '<div class="listContent">' + information + roster + "</div>"
    ),
    "teamactForm",
    {**TEAM, **(fields or {})},
  )


def collaboration(body, *, area="area-1", fields=None, paginator=""):
  region = (
    '<div class="listContent"><dl class="teamList"><dd>'
    + body
    + "</dd></dl></div>"
  )
  return page(
    box(region) + paginator,
    "teamactForm",
    {**TEAM, AREA_FIELD: area, **(fields or {})},
  )


def collaboration_posts(text="Post", post_id="1"):
  return (
    '<div class="listContent">'
    f'<dl class="element" id="post-{post_id}">'
    f'<dt>Post {post_id}</dt><dd><div class="cont">{text}</div></dd>'
    "</dl></div>"
  )


def paginator(scope, current=1, total=2):
  base = "/Teamact.do?" + urlencode({
    "cmd": "viewTeamactInfo",
    "type": "coWork",
    **scope,
  }) + "&curPage="
  return (
    f"<script>new Paginator('pages', {total}, 10, {current}, "
    f"'{base}')</script>"
  )


@pytest.fixture
def setup(private_dir):
  state = SimpleNamespace(
    pages=deque(),
    posts=deque(),
    menus=deque(),
    calls=[],
    downloads=[],
    events=queue.Queue(),
  )

  def take(values):
    assert values, "unexpected reader operation"
    value = values.popleft()
    if isinstance(value, BaseException):
      raise value
    return value

  class Reader:
    course = COURSE

    def __init__(self):
      self.http = SimpleNamespace(
        requests=0, bytes=0, seconds=0.0
      )

    def page(self, url, name):
      state.calls.append(("page", url, name))
      return take(state.pages)

    def post(self, original, name, route, changes):
      fields = changed_fields(form_fields(original, name), changes)
      state.calls.append(("post", route, name, copy.deepcopy(changes)))
      result = take(state.posts)
      return result, source_url(CTL + route, fields)

    def menu(self, url, recipe):
      state.calls.append(("menu", url, recipe))
      return take(state.menus), source_url(
        CTL + recipe[1],
        [("cmd", recipe[2]), ("courseDTO.courseId", COURSE)],
      )

  producer = Producer(
    reader=Reader(),
    root=private_dir,
    snapshot={"items": {}, "documents": {}},
    options=SimpleNamespace(
      course=None, refresh=False, include_submissions=True,
    ),
    events=state.events,
    stop=threading.Event(),
    home=Home(soup_html("<p>Home</p>"), "student-1", {COURSE: "Course"}),
  )
  producer.index(
    ROOT,
    "Course",
    FOLDER,
    [],
    complete=False,
    parent="index",
    course_id=COURSE,
  )

  def download(owner, folder, url, name, image=False, *, role="attachment"):
    state.downloads.append({
      "owner": owner,
      "url": url,
      "name": name,
      "image": image,
      "role": role,
    })
    key = identity(owner, url)
    return key, {
      "folder": folder + "/Document",
      "original": name,
      "role": role,
    }

  producer.download = download
  state.producer = producer
  state.traversal = CourseTraversal(producer)
  return state


def captured_body(setup, key):
  bodies = [
    event.data["body"]
    for event in list(setup.events.queue)
    if event.kind == "item"
    and event.data["key"] == key
    and event.data["body"] is not None
  ]
  assert bodies, f"no captured body for {key}"
  return bodies[-1]


def test_course_run_deduplicates_navigation_and_marks_absent_menus(
  setup, monkeypatch
):
  board_url = "/Course.do?" + urlencode({
    "cmd": "viewBoardContentsList",
    "courseDTO.courseId": COURSE,
    "boardInfoDTO.boardInfoId": "board-1",
  })
  menu_url = "/Main.do?" + urlencode({
    "cmd": "moveCourseMenu",
    "courseDTO.courseId": COURSE,
    "mainDTO.menuId": "menu_00053",
  })
  setup.pages.append(page(
    f'<a href="{board_url}">Resources</a>'
    f'<a href="{board_url}">Resources again</a>'
    f'<a href="{menu_url}">Course information</a>'
    f'<a href="{menu_url}">Repeated menu</a>'
  ))
  calls = []
  monkeypatch.setattr(
    setup.traversal, "board",
    lambda *args: calls.append(("board", args)),
  )
  monkeypatch.setattr(
    setup.traversal, "section",
    lambda *args: calls.append(("section", args)),
  )

  setup.traversal.run(COURSE, "Course")

  assert [kind for kind, _ in calls] == ["board", "section"]
  root = setup.producer.items[ROOT]
  assert root["status"] == "archived"
  assert root["course_id"] == COURSE
  assert root["include_submissions"] is True
  assert len(root["children"]) == len(MENUS) + 1
  assert setup.producer.items[ROOT + "/exams"]["status"] == "absent"
  assert (FOLDER + "/attendance-offline.md").casefold() in setup.producer.used


def test_course_run_does_not_treat_unrecognized_navigation_as_complete(setup):
  setup.pages.append(page("<p>Changed course layout</p>"))

  with pytest.raises(Failure, match="no supported course navigation"):
    setup.traversal.run(COURSE, "Course")

  assert setup.producer.items[ROOT]["status"] == "partial"
  assert ROOT + "/exams" not in setup.producer.items


def test_course_run_requires_prepared_account_course_context(setup):
  setup.producer.reader.course = "other-course"

  with pytest.raises(Failure, match="not prepared"):
    setup.traversal.run(COURSE, "Course")

  assert setup.calls == []


def test_inline_board_post_does_not_make_a_detail_request(setup):
  key = ROOT + "/boards/board-1"
  link = action("moveBoardView", "post-1", "7", title="Linked title")
  setup.pages.append(page(
    '<div class="listContent">'
    + card("Actual title", "<p>Lecture instructions</p>", link)
    + "</div>",
    fields={"boardInfoDTO.boardInfoId": "board-1"},
  ))
  url = CTL + "/Course.do?" + urlencode({
    "cmd": "viewBoardContentsList",
    "courseDTO.courseId": COURSE,
    "boardInfoDTO.boardInfoId": "board-1",
  })

  setup.traversal.board(url, key, "Resources", FOLDER + "/Resources", ROOT)

  child = key + "/post-1"
  assert setup.producer.items[key]["children"] == [child]
  assert setup.producer.items[child]["title"] == "Actual title"
  assert "Lecture instructions" in captured_body(setup, child)
  assert not any(call[0] == "post" for call in setup.calls)


def test_board_detail_retains_comment_completeness_warning(setup):
  key = ROOT + "/boards/board-1"
  link = action("moveBoardView", "post-1", "7", title="Announcement")
  setup.pages.append(page(link))
  setup.posts.append(page(
    '<div class="listContent">'
    + card("Announcement", "<p>Deadline Friday</p>")
    + '<input name="board.commentCnt" value="2">'
    + "</div>",
    fields={"boardContentsDTO.boardContentsId": "post-1"},
  ))

  setup.traversal.board(
    CTL + "/Course.do?cmd=viewBoardContentsList",
    key, "Board", FOLDER + "/Board", ROOT,
  )

  item = setup.producer.items[key + "/post-1"]
  assert item["status"] == "partial"
  assert item["problems"] == ["comment details not extracted"]
  assert "Deadline Friday" in captured_body(setup, key + "/post-1")
  post = next(call for call in setup.calls if call[0] == "post")
  assert post[3]["gubun"] == "V"
  assert post[3]["boardContentsDTO.boardNo"] == "7"


def test_failed_board_detail_does_not_complete_that_post(setup):
  key = ROOT + "/boards/board-1"
  setup.pages.append(page(action("moveBoardView", "post-1", "1")))
  setup.posts.append(page("<p>Unexpected layout</p>"))

  setup.traversal.board(
    CTL + "/Course.do?cmd=viewBoardContentsList",
    key, "Board", FOLDER + "/Board", ROOT,
  )

  assert setup.producer.items[key]["status"] == "archived"
  assert setup.producer.items[key + "/post-1"]["status"] == "failed"


def test_board_repeated_page_stops_without_claiming_complete_listing(
  setup, monkeypatch
):
  key = ROOT + "/boards/board-1"
  listing = page(card(
    "Post", "Content", action("moveBoardView", "post-1", "1")
  ))
  setup.pages.append(listing)
  monkeypatch.setattr(
    courses_module, "next_page", lambda *args, **kwargs: listing
  )

  with pytest.raises(Failure, match="board repeated"):
    setup.traversal.board(
      CTL + "/Course.do?cmd=viewBoardContentsList",
      key, "Board", FOLDER + "/Board", ROOT,
    )

  assert setup.producer.items[key]["status"] == "partial"
  assert setup.producer.items[key]["children"] == [key + "/post-1"]


def test_explicit_empty_board_is_a_complete_empty_listing(setup):
  key = ROOT + "/boards/board-1"
  setup.pages.append(page(
    '<div class="search_unfine">조회된 내용이 없습니다.</div>'
  ))

  setup.traversal.board(
    CTL + "/Course.do?cmd=viewBoardContentsList",
    key, "Board", FOLDER + "/Board", ROOT,
  )

  assert setup.producer.items[key]["status"] == "empty"
  assert setup.producer.items[key]["children"] == []


def test_board_source_table_survives_navigation_class_names(setup):
  key = ROOT + "/boards/board-1"
  setup.pages.append(page(card(
    "Task",
    '<p>Assignment instructions</p><div class="btnBox">'
    "<table><tr><th>Deadline</th><td>Friday</td></tr></table></div>",
    action("moveBoardView", "post-1", "1"),
  )))

  setup.traversal.board(
    CTL + "/Course.do?cmd=viewBoardContentsList",
    key, "Board", FOLDER + "/Board", ROOT,
  )

  body = captured_body(setup, key + "/post-1")
  assert "Assignment instructions" in body
  assert "| Deadline | Friday |" in body
  assert setup.producer.items[key + "/post-1"]["status"] == "archived"


def test_course_information_captures_only_the_two_supported_regions(setup):
  setup.menus.append(page(
    box("<p>Course description</p>")
    + box("<p>Instructor information</p>")
    + box("<p>Unrelated panel</p>")
  ))
  key = ROOT + "/course"

  setup.traversal.section(
    "menu", MENUS["menu_00053"], key, "Course information", FOLDER, ROOT
  )

  body = captured_body(setup, key)
  assert "Course description" in body
  assert "Instructor information" in body
  assert "Unrelated panel" not in body


@pytest.mark.parametrize("heading", ["합 계", "Total"])
def test_grade_page_retains_recognized_total_table(setup, heading):
  setup.menus.append(page(
    f"<table><tr><th>{heading}</th><td>95</td></tr></table>"
    '<table id="table1"><tr><th>Assignment</th><td>90</td></tr></table>',
    "scoreForm",
  ))
  key = ROOT + "/grades"

  setup.traversal.section(
    "menu", MENUS["menu_00106"], key, "Grades", FOLDER, ROOT
  )

  body = captured_body(setup, key)
  assert "95" in body
  assert "90" in body


def test_grade_page_does_not_capture_an_unrelated_previous_table(setup):
  setup.menus.append(page(
    "<table><tr><th>Navigation</th><td>Unrelated</td></tr></table>"
    '<table id="table1"><tr><th>Grade</th><td>95</td></tr></table>',
    "scoreForm",
  ))
  key = ROOT + "/grades"

  setup.traversal.section(
    "menu", MENUS["menu_00106"], key, "Grades", FOLDER, ROOT
  )

  assert "95" in captured_body(setup, key)
  assert "Unrelated" not in captured_body(setup, key)


@pytest.mark.parametrize(
  ("menu", "kind", "form_name"),
  [
    ("menu_00065", "discussion", "forumForm"),
    ("menu_00067", "exams", "etestForm"),
  ],
)
def test_explicit_empty_unsupported_sections_can_be_omitted_from_export(
  setup, menu, kind, form_name
):
  setup.menus.append(page(
    box('<div class="search_unfine">No data.</div>'), form_name
  ))
  key = ROOT + "/" + kind

  setup.traversal.section(
    "menu", MENUS[menu], key, kind, FOLDER, ROOT
  )

  assert setup.producer.items[key]["status"] == "empty"
  assert setup.producer.items[key]["empty_listing"] is True


def test_no_data_in_one_region_does_not_hide_other_exam_content(setup):
  setup.menus.append(page(
    box('<div class="search_unfine">No data</div>')
    + box("<p>Exam scheduled for Friday</p>"),
    "etestForm",
  ))
  key = ROOT + "/exams"

  setup.traversal.section(
    "menu", MENUS["menu_00067"], key, "Exams", FOLDER, ROOT
  )

  item = setup.producer.items[key]
  assert item["status"] == "unsupported"
  assert item["empty_listing"] is False
  assert "Exam scheduled for Friday" in captured_body(setup, key)


def test_attendance_adds_its_offline_related_page(setup):
  setup.menus.append(page(box("<p>Online attendance</p>"), "attendForm"))
  setup.posts.append(page(box("<p>Offline attendance</p>"), "attendForm"))
  key = ROOT + "/attendance"

  setup.traversal.section(
    "menu", MENUS["menu_00082"], key, "Attendance", FOLDER, ROOT
  )

  assert setup.producer.items[key]["related"] == [key + "/offline"]
  assert "Offline attendance" in captured_body(setup, key + "/offline")
  post = next(call for call in setup.calls if call[0] == "post")
  assert post[3]["boardInfoDTO.gubun"] == "offline"


def test_attendance_with_real_rows_is_not_an_empty_listing(setup):
  original = page("", "attendForm")
  setup.posts.append(page(
    box(
      '<div class="search_unfine">No data</div>'
      "<table><tr><td>Week 1</td><td>Present</td></tr></table>"
    ),
    "attendForm",
  ))
  key = ROOT + "/attendance/offline"

  setup.traversal.offline_attendance(original, key, FOLDER, ROOT)

  assert setup.producer.items[key]["empty_listing"] is False
  assert "Present" in captured_body(setup, key)


def test_assignment_single_argument_link_keeps_summary_without_guessing_receipt(
  setup
):
  key = ROOT + "/assignments"
  listing = page(
    card(
      "Assignment",
      "<p>Complete the exercise</p>",
      action("submitReport", "assignment-1"),
    ),
    "reportForm",
  )

  setup.traversal.activities(
    listing, key, "Assignments", FOLDER + "/assignments",
    CTL + "/Report.do?cmd=viewReportInfoPageList", False, ROOT,
  )

  child = key + "/assignment-1"
  assert "Complete the exercise" in captured_body(setup, child)
  assert setup.producer.items[child]["related"] == []
  assert not any(call[0] == "post" for call in setup.calls)


def test_duplicate_identical_activity_controls_are_not_ambiguous(setup):
  key = ROOT + "/assignments"
  link = action("submitReport", "assignment-1")
  listing = page(
    card("Assignment", "Instructions", link + link), "reportForm"
  )

  setup.traversal.activities(
    listing, key, "Assignments", FOLDER + "/assignments",
    CTL + "/Report.do?cmd=viewReportInfoPageList", False, ROOT,
  )

  assert setup.producer.items[key]["children"] == [key + "/assignment-1"]


def test_conflicting_activity_controls_are_rejected(setup):
  listing = page(card(
    "Assignment",
    "Instructions",
    action("submitReport", "one") + action("submitReport", "two"),
  ), "reportForm")

  with pytest.raises(Failure, match="ambiguous activity identity"):
    setup.traversal.activities(
      listing, ROOT + "/assignments", "Assignments",
      FOLDER + "/assignments", CTL + "/Report.do",
      False, ROOT,
    )


def test_unlinked_assignments_remain_source_content(setup):
  key = ROOT + "/assignments"
  listing = page(
    card("Unlinked task", "<p>Read chapter 3 before Monday</p>"),
    "reportForm",
  )

  setup.traversal.activities(
    listing, key, "Assignments", FOLDER + "/assignments",
    CTL + "/Report.do", False, ROOT,
  )

  assert key + "/unlinked" in setup.producer.items[key]["children"]
  assert "Read chapter 3 before Monday" in captured_body(
    setup, key + "/unlinked"
  )


def test_individual_receipt_only_captures_the_receipt_card(setup):
  listing = page("", "reportForm")
  setup.posts.append(page(
    card("Assignment description", "Already captured separately")
    + receipt_card(attachments=download_link()),
    "reportForm",
    {"reportInfoDTO.reportInfoId": "assignment-1"},
  ))
  key = ROOT + "/assignments/assignment-1/submission"

  setup.traversal.receipt(
    listing, ("assignment-1", "N"), key, FOLDER, ROOT
  )

  body = captured_body(setup, key)
  assert "Submitted" in body
  assert "Already captured separately" not in body
  assert setup.downloads[0]["role"] == "submission"
  post = setup.calls[0]
  assert post[3]["cmd"] == "viewReportSubmit"
  assert post[3]["gubun"] == "A"
  assert post[3]["reportInfoDTO.randomYn"] == "N"


def test_receipt_exclusion_covers_files_and_inline_images(setup):
  setup.producer.options.include_submissions = False
  listing = page("", "reportForm")
  assets = download_link() + '<img src="/lmsdata/submission.png" alt="Diagram">'
  setup.posts.append(page(
    card("Assignment", "Instructions") + receipt_card(attachments=assets),
    "reportForm",
    {"reportInfoDTO.reportInfoId": "assignment-1"},
  ))
  key = ROOT + "/assignments/assignment-1/submission"

  setup.traversal.receipt(
    listing, ("assignment-1", "N"), key, FOLDER, ROOT
  )

  assert setup.downloads == []
  assert setup.producer.items[key]["documents"] == []
  assert "Submitted" in captured_body(setup, key)
  assert "Diagram" in captured_body(setup, key)
  assert setup.producer.items[key]["empty_listing"] is False


@pytest.mark.parametrize("include", [False, True])
def test_receipt_direct_link_uses_the_same_submission_policy(setup, include):
  setup.producer.options.include_submissions = include
  url = CTL + "/fileDownServlet?" + urlencode({
    "rFileName": "submitted.pdf",
    "sFileName": "stored",
    "filePath": "folder",
  })
  assets = f'<a class="btnBox" href="{escape(url)}">Download submission</a>'
  setup.posts.append(page(
    card("Assignment", "Instructions") + receipt_card(attachments=assets),
    "reportForm",
    {"reportInfoDTO.reportInfoId": "assignment-1"},
  ))
  key = ROOT + "/receipt"

  setup.traversal.receipt(
    page("", "reportForm"), ("assignment-1", "N"), key, FOLDER, ROOT
  )

  assert len(setup.downloads) == int(include)
  if include:
    assert setup.downloads[0]["url"] == url
    assert setup.downloads[0]["name"] == "submitted.pdf"
    assert setup.downloads[0]["role"] == "submission"

  body = captured_body(setup, key)
  assert "Submitted" in body
  assert "Download submission" in body
  assert "fileDownServlet" not in body


def test_receipt_rejects_a_returned_different_assignment(setup):
  setup.posts.append(page(
    card("Assignment", "Instructions") + receipt_card(),
    "reportForm",
    {"reportInfoDTO.reportInfoId": "other"},
  ))

  with pytest.raises(Failure, match="context mismatch"):
    setup.traversal.receipt(
      page("", "reportForm"),
      ("assignment-1", "N"),
      ROOT + "/receipt",
      FOLDER,
      ROOT,
    )


def test_failed_receipt_refresh_keeps_previous_body_and_references(setup):
  key = ROOT + "/receipt"
  setup.producer.content(
    key, "Submission", FOLDER + "/submission.md",
    [soup_html("<p>Previous receipt</p>")], CTL + "/Report.do",
    parent=ROOT, role="submission",
  )
  setup.producer.items[key]["documents"] = ["previous-submission"]
  setup.posts.append(page("<p>Changed layout</p>", "reportForm"))

  setup.producer.attempt(
    key, "Submission", setup.traversal.receipt,
    page("", "reportForm"), ("assignment-1", "N"), key, FOLDER, ROOT,
  )

  assert setup.producer.items[key]["status"] == "failed"
  assert setup.producer.items[key]["documents"] == ["previous-submission"]
  assert captured_body(setup, key) == "Previous receipt"


def test_missing_team_context_is_absent_without_guessing_a_team(setup):
  fields = {
    "courseDTO.courseId": COURSE,
    "teamactInfoDTO.teamactId": "activity-1",
    "teamactInfoDTO.teamCategoryId": "category-1",
  }
  setup.posts.append(page("", "teamactForm", fields))
  key = ROOT + "/team-activities/activity-1/own-team"

  setup.traversal.own_team(
    page("", "teamactForm"),
    ("activity-1", "category-1"),
    key,
    FOLDER + "/own-team",
    ROOT,
  )

  assert setup.producer.items[key]["status"] == "absent"
  assert setup.producer.items[key]["message"] == "no current-team context"
  assert len(setup.calls) == 1


def test_own_team_indexes_information_unique_areas_and_output(
  setup, monkeypatch
):
  detail = own_team_page(options=("area-1", "area-1", "output"))
  setup.posts.append(detail)
  key = ROOT + "/team-activities/activity-1/own-team"
  calls = []

  monkeypatch.setattr(
    setup.traversal, "team_area",
    lambda *args: calls.append(("area", args[1], args[2])),
  )
  monkeypatch.setattr(
    setup.traversal, "team_output",
    lambda *args: calls.append(("output", args[1])),
  )

  setup.traversal.own_team(
    page("", "teamactForm"),
    ("activity-1", "category-1"),
    key,
    FOLDER + "/own-team",
    ROOT,
  )

  assert setup.producer.items[key]["children"] == [
    key + "/info",
    key + "/areas/area-1",
    key + "/areas/output",
    key + "/output",
  ]
  assert "Own team member" in captured_body(setup, key + "/info")
  assert "Team instructions" in captured_body(setup, key + "/info")
  assert [call[0] for call in calls] == ["area", "area", "output"]
  assert setup.calls[0][3]["curPage"] == "1"


def test_own_team_rejects_changed_activity_scope(setup):
  setup.posts.append(own_team_page(fields={
    "teamactInfoDTO.teamactId": "other-activity",
  }))

  with pytest.raises(Failure, match="context mismatch"):
    setup.traversal.own_team(
      page("", "teamactForm"),
      ("activity-1", "category-1"),
      ROOT + "/own-team",
      FOLDER + "/own-team",
      ROOT,
    )


def test_collaboration_retains_posts_and_comments_but_not_comment_editor(setup):
  content = (
    '<div class="listContent"><dl class="element"><dt>Post</dt><dd>'
    '<div class="cont">Displayed post</div>'
    '<div class="comment">Displayed comment</div>'
    '<div class="partition" id="commentRegForm_1">'
    "Editor instructions<textarea>Draft</textarea></div>"
    "</dd></dl></div>"
  )
  response = collaboration(content)
  setup.posts.append(response)
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT, "Team discussion"
  )

  body = captured_body(setup, key)
  assert "Displayed post" in body
  assert "Displayed comment" in body
  assert "Editor instructions" not in body
  assert "Draft" not in body
  assert response.select_one("#commentRegForm_1") is not None
  assert setup.producer.items[key]["title"] == "Team discussion"
  assert setup.producer.items[key]["status"] == "partial"
  assert any(
    "comment completeness" in problem
    for problem in setup.producer.items[key]["problems"]
  )


def test_collaboration_pagination_preserves_all_pages_and_scope(setup):
  scope = {**TEAM, AREA_FIELD: "area-1"}
  setup.posts.append(collaboration(
    collaboration_posts("First page", "1"),
    paginator=paginator(scope),
  ))
  setup.pages.append(collaboration(
    collaboration_posts("Second page", "2"),
    paginator=paginator(scope, current=2),
  ))
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT
  )

  body = captured_body(setup, key)
  assert "First page" in body
  assert "Second page" in body
  reads = [call for call in setup.calls if call[0] == "page"]
  assert len(reads) == 1
  assert "teamDTO.teamInfoId=team-1" in reads[0][1]
  assert "curPage=2" in reads[0][1]


def test_collaboration_repeated_page_is_not_appended_twice(setup):
  scope = {**TEAM, AREA_FIELD: "area-1"}
  content = collaboration_posts("Only once", "1")
  setup.posts.append(collaboration(content, paginator=paginator(scope)))
  setup.pages.append(collaboration(
    content, paginator=paginator(scope, current=2)
  ))
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT
  )

  assert captured_body(setup, key).count("Only once") == 1
  assert any(
    "repeated a page" in problem
    for problem in setup.producer.items[key]["problems"]
  )


def test_explicit_empty_collaboration_is_marked_as_a_listing(setup):
  setup.posts.append(collaboration(
    '<div class="search_unfine">등록된 내용이 없습니다.</div>'
  ))
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT
  )

  item = setup.producer.items[key]
  assert item["status"] == "empty"
  assert item["empty_listing"] is True
  assert item["problems"] == []


def test_empty_collaboration_with_an_attachment_is_not_discarded(setup):
  setup.posts.append(collaboration(
    '<div class="search_unfine">No data</div>' + download_link()
  ))
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT
  )

  assert setup.producer.items[key]["status"] == "partial"
  assert setup.producer.items[key]["empty_listing"] is False
  assert len(setup.downloads) == 1


def test_empty_collaboration_advertising_more_pages_is_partial(setup):
  scope = {**TEAM, AREA_FIELD: "area-1"}
  setup.posts.append(collaboration(
    '<div class="search_unfine">No data</div>',
    paginator=paginator(scope),
  ))
  setup.pages.append(collaboration(collaboration_posts("Later content", "2")))
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT
  )

  assert "Later content" in captured_body(setup, key)
  assert setup.producer.items[key]["empty_listing"] is False
  assert any(
    "advertises further pages" in problem
    for problem in setup.producer.items[key]["problems"]
  )


def test_collaboration_pagination_failure_retains_already_captured_content(
  setup, monkeypatch
):
  setup.posts.append(collaboration(collaboration_posts("Keep this", "1")))

  def fail(*args, **kwargs):
    raise Failure("paginator changed scope")

  monkeypatch.setattr(courses_module, "next_page", fail)
  key = ROOT + "/own-team/areas/area-1"

  setup.traversal.team_area(
    own_team_page(), "area-1", key, FOLDER + "/Area", ROOT
  )

  assert "Keep this" in captured_body(setup, key)
  assert any(
    "pagination incomplete" in problem
    for problem in setup.producer.items[key]["problems"]
  )


@pytest.mark.parametrize(
  "error",
  [Failure("session expired", 1), StorageFailure("disk failed")],
)
def test_collaboration_authentication_and_storage_errors_are_not_softened(
  setup, monkeypatch, error
):
  setup.posts.append(collaboration(collaboration_posts("Post", "1")))

  def fail(*args, **kwargs):
    raise error

  monkeypatch.setattr(courses_module, "next_page", fail)

  with pytest.raises(type(error), match=error.message):
    setup.traversal.team_area(
      own_team_page(), "area-1", ROOT + "/area", FOLDER + "/Area", ROOT
    )


@pytest.mark.parametrize(
  "changes",
  [
    {"teamDTO.teamInfoId": "other-team"},
    {"teamactInfoDTO.teamCategoryId": ""},
    {AREA_FIELD: "other-area"},
  ],
)
def test_collaboration_requires_complete_matching_returned_scope(setup, changes):
  setup.posts.append(collaboration(
    collaboration_posts(), fields=changes
  ))

  with pytest.raises(Failure, match="context mismatch"):
    setup.traversal.team_area(
      own_team_page(), "area-1", ROOT + "/area", FOLDER + "/Area", ROOT
    )


def test_empty_team_output_is_preserved_as_meaningful_receipt_content(setup):
  setup.producer.options.include_submissions = False
  setup.posts.append(page(
    card("Activity", "Not the receipt") + empty_output_card(),
    "teamactForm",
    TEAM,
  ))
  key = ROOT + "/own-team/output"

  setup.traversal.team_output(
    own_team_page(), key, FOLDER + "/own-team", ROOT
  )

  body = captured_body(setup, key)
  assert "제출 내역이 없습니다." in body
  assert "Not the receipt" not in body
  assert setup.producer.items[key]["status"] == "empty"
  assert setup.producer.items[key]["empty_listing"] is False
  assert setup.downloads == []


def test_populated_team_output_uses_submission_role_for_all_assets(setup):
  assets = download_link() + '<img src="/lmsdata/output.png" alt="Result">'
  setup.posts.append(page(
    card("Activity", "Instructions")
    + receipt_card(team=True, attachments=assets, output="Our result"),
    "teamactForm",
    TEAM,
  ))
  key = ROOT + "/own-team/output"

  setup.traversal.team_output(
    own_team_page(), key, FOLDER + "/own-team", ROOT
  )

  assert "Our result" in captured_body(setup, key)
  assert len(setup.downloads) == 2
  assert all(item["role"] == "submission" for item in setup.downloads)


@pytest.mark.parametrize("include", [False, True])
def test_team_output_image_outside_cont_is_not_silently_lost(setup, include):
  setup.producer.options.include_submissions = include
  receipt = receipt_card(team=True).replace(
    "</dd>",
    '<img src="/lmsdata/output.png" alt="Result diagram"></dd>',
  )
  setup.posts.append(page(
    card("Activity", "Instructions") + receipt, "teamactForm", TEAM
  ))
  key = ROOT + "/own-team/output"

  setup.traversal.team_output(
    own_team_page(), key, FOLDER + "/own-team", ROOT
  )

  assert len(setup.downloads) == int(include)
  if include:
    assert setup.downloads[0]["role"] == "submission"
    assert setup.downloads[0]["image"] is True
  assert "Result diagram" in captured_body(setup, key)


def test_team_output_cannot_switch_team_context(setup):
  setup.posts.append(page(
    card("Activity", "Instructions") + receipt_card(team=True),
    "teamactForm",
    {**TEAM, "teamDTO.teamInfoId": "other-team"},
  ))

  with pytest.raises(Failure, match="context mismatch"):
    setup.traversal.team_output(
      own_team_page(), ROOT + "/output", FOLDER, ROOT
    )


@pytest.mark.parametrize(
  "body",
  [
    empty_output_card().replace('colspan="2"', 'colspan="3"'),
    empty_output_card().replace(
      "제출 내역이 없습니다.", "제출 내역이 없습니다.<a href='/file'>File</a>"
    ),
    empty_output_card().replace(
      "</table>", "<tr><td>Unexpected output</td></tr></table>"
    ),
    empty_output_card().replace(
      'summary="산출물 제출 정보"', 'summary="Other table"'
    ),
    empty_output_card().replace("<dt>제출정보</dt>", "<dt>Other heading</dt>"),
    empty_output_card().replace(
      "</td>", '<input name="hidden-output"></td>'
    ),
  ],
)
def test_mixed_or_unrecognized_empty_team_receipts_are_rejected(body):
  with pytest.raises(Failure, match="empty team-output layout"):
    verify_receipt(soup_html(body).dl, team=True)


def test_receipt_validation_accepts_known_english_headings():
  body = receipt_card(team=True)
  body = body.replace("제출상태", "Submission status")
  body = body.replace("첨부파일", "Attached files")
  body = body.replace("산출물내용", "Output content")

  assert verify_receipt(soup_html(body).dl, team=True) is False


def test_team_receipt_requires_output_content_heading():
  with pytest.raises(Failure, match="content heading"):
    verify_receipt(soup_html(receipt_card()).dl, team=True)


def test_individual_receipt_does_not_guess_from_a_generic_empty_message():
  with pytest.raises(Failure, match="receipt layout"):
    verify_receipt(soup_html(empty_output_card()).dl, team=False)
