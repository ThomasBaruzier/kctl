import threading

from collections import deque
from contextlib import contextmanager
from email.message import Message
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import kctl.reader as reader_module

from kctl.auth_html import soup_html, text_of
from kctl.core import CTL, HOME_URL, Failure
from kctl.reader import (
  Reader,
  canonical_fields,
  changed_fields,
  clean_content,
  field_value,
  form_fields,
  homepage_account,
  literal_calls,
  next_page,
  query_fields,
  source_url,
  syllabus_course_id,
)


def form(name="courseForm", fields=None, body=""):
  controls = "".join(
    f'<input type="hidden" name="{key}" value="{value}">'
    for key, value in (fields or {}).items()
  )
  return f'<form name="{name}">{controls}{body}</form>'


def course_page(course="course-1", name="courseForm", extra=None):
  fields = {"courseDTO.courseId": course, **(extra or {})}
  return form(name, fields)


def homepage(account="student-1"):
  return (
    form("mainForm", {"userType": "learner"})
    + '<div id="header"><div class="login"><fieldset><p>'
    + f"<span>Student ({account})</span>"
    + "</p></fieldset></div></div>"
    + '<a href="javascript:viewMyCourse(\'course-1\')">Course One</a>'
    + '<a onclick="viewMyCourse(\'course-2\')">Course Two</a>'
  )


@pytest.fixture
def upstream(monkeypatch):
  state = SimpleNamespace(responses=deque(), requests=[], closed=0)

  def enqueue(body, status=200, headers=(), charset="utf-8"):
    message = Message()
    message["Content-Type"] = "text/html; charset=" + charset
    for key, value in headers:
      message[key] = value

    state.responses.append(SimpleNamespace(
      headers=message,
      status=status,
      raw=body.encode(charset) if isinstance(body, str) else body,
    ))

  class HTTP:
    def __init__(self, jar, limit, stop):
      assert limit == 2000
      self.stop = stop

    @contextmanager
    def open(self, url, data=None):
      state.requests.append((url, data))
      assert state.responses, "unexpected course request"
      response = state.responses.popleft()
      try:
        yield response
      finally:
        state.closed += 1

    def read(self, response):
      return response.raw

  monkeypatch.setattr(reader_module, "HTTP", HTTP)
  state.enqueue = enqueue
  state.reader = Reader(object(), threading.Event())
  return state


def test_form_fields_preserve_context_and_selected_values():
  page = soup_html(form(
    fields={"courseDTO.courseId": "course-1", "context": "preserved"},
    body=(
      '<input name="visible" value="omit">'
      '<input type="HIDDEN" name="uppercase" value="keep">'
      '<input type="hidden" name="disabled" value="omit" disabled>'
      '<fieldset disabled><input type="hidden" name="nested"></fieldset>'
      '<select name="choice"><option value="a">A</option>'
      '<option value="b" selected>B</option></select>'
      '<select name="default"><option value="first">First</option></select>'
    ),
  ))

  assert form_fields(page, "courseForm") == [
    ("courseDTO.courseId", "course-1"),
    ("context", "preserved"),
    ("uppercase", "keep"),
    ("choice", "b"),
    ("default", "first"),
  ]


@pytest.mark.parametrize(
  "body",
  [
    "",
    form() + form(),
    form(body=form("nested")),
    form(body='<input type="hidden" name="x" form="other">'),
    form(body='<select name="x" multiple><option value="a"></option></select>'),
    form(body='<select name="x" form="other"></select>'),
    form(body=(
      '<select name="x"><option selected value="a"></option>'
      '<option selected value="b"></option></select>'
    )),
    form(body=(
      '<select name="x"><option selected disabled value="a"></option></select>'
    )),
  ],
)
def test_ambiguous_or_unsupported_course_forms_fail(body):
  with pytest.raises(Failure):
    form_fields(soup_html(body), "courseForm")


def test_form_field_count_is_bounded():
  body = "".join(
    f'<input type="hidden" name="field{index}" value="x">'
    for index in range(257)
  )

  with pytest.raises(Failure, match="too many"):
    form_fields(soup_html(form(body=body)), "courseForm")


def test_control_fields_are_canonicalized_without_losing_context_duplicates():
  values = [
    ("cmd", "viewStudyHome"),
    ("cmd", "viewStudyHome"),
    ("context", "one"),
    ("context", "two"),
  ]

  assert canonical_fields(values) == [
    ("cmd", "viewStudyHome"),
    ("context", "one"),
    ("context", "two"),
  ]


def test_conflicting_control_fields_are_rejected():
  with pytest.raises(Failure, match="conflicting field"):
    canonical_fields([("cmd", "one"), ("cmd", "two")])


def test_changed_fields_replace_all_old_occurrences():
  values = [("cmd", "old"), ("context", "keep"), ("cmd", "old")]

  assert changed_fields(values, {"cmd": "new"}) == [
    ("context", "keep"), ("cmd", "new"),
  ]
  assert field_value(values, "missing") is None


def test_query_field_count_is_bounded():
  query = urlencode([(f"field{index}", "x") for index in range(257)])

  with pytest.raises(Failure, match="malformed CTL query"):
    query_fields(query)


def test_source_urls_only_retain_source_identity_fields():
  value = source_url(
    "/Course.do;jsessionid=private?cmd=viewStudyHome&token=private",
    [("courseDTO.courseId", "course-1"), ("context", "private")],
  )

  assert value == (
    CTL + "/Course.do?cmd=viewStudyHome&courseDTO.courseId=course-1"
  )
  assert "private" not in value


def test_literal_calls_only_extract_plain_quoted_arguments():
  page = soup_html(
    '<a onclick="moveBoardView(\'one\', \'2\')">One</a>'
    '<a href="javascript:moveBoardView(\'two\',\'3\')">Two</a>'
    '<a onclick="moveBoardView(dynamic, \'4\')">Skip</a>'
    '<script>moveBoardView(\'script\', \'5\')</script>'
  )

  assert [call[1:] for call in literal_calls(page, "moveBoardView", 2)] == [
    ("one", "2"), ("two", "3"),
  ]


def test_request_preserves_allowed_context_and_encodes_unicode(upstream):
  upstream.reader.course = "course-1"
  target, data = upstream.reader.request(
    CTL + "/Course.do",
    [
      ("cmd", "viewStudyHome"),
      ("courseDTO.courseId", "course-1"),
      ("context", "자료"),
    ],
  )

  assert target == CTL + "/Course.do"
  assert parse_qs(data.decode())["context"] == ["자료"]
  assert upstream.requests == []


@pytest.mark.parametrize(
  "url",
  [
    CTL + "/Course.do?cmd=deleteBoardContents",
    CTL + "/Report.do?cmd=submitReport",
    CTL + "/User.do?cmd=logoutUser",
    "https://example.com/Course.do?cmd=viewStudyHome",
    CTL + "/Course.do?cmd=viewStudyHome&cmd=deleteBoardContents",
  ],
)
def test_unapproved_reads_are_rejected_before_network_access(upstream, url):
  with pytest.raises(Failure):
    upstream.reader.request(url)

  assert upstream.requests == []


def test_post_recipe_cannot_mix_query_and_body(upstream):
  with pytest.raises(Failure, match="URL query"):
    upstream.reader.request(
      CTL + "/Course.do?cmd=viewStudyHome",
      [("cmd", "viewStudyHome")],
    )


def test_outgoing_course_context_is_required(upstream):
  upstream.reader.course = "course-1"

  with pytest.raises(Failure, match="outgoing course"):
    upstream.reader.request(
      CTL + "/Course.do?cmd=viewStudyHome&courseDTO.courseId=course-2"
    )


@pytest.mark.parametrize(
  ("route", "command", "name", "expected"),
  [
    ("/Report.do", "viewReportSubmit", "gubun", "A"),
    ("/Course.do", "viewBoardContents", "gubun", "V"),
    ("/Teamact.do", "viewTeamactInfo", "type", "coWork"),
    ("/Teamact.do", "teamactOutputInfo", "gubun", "V"),
  ],
)
def test_detail_reads_require_the_known_read_mode(
  upstream, route, command, name, expected
):
  with pytest.raises(Failure, match="read mode"):
    upstream.reader.request(CTL + route, [("cmd", command)])

  target, data = upstream.reader.request(
    CTL + route, [("cmd", command), (name, expected)]
  )
  assert target == CTL + route
  assert parse_qs(data.decode())[name] == [expected]


def test_home_returns_reusable_page_account_and_enrollment(upstream):
  upstream.enqueue(homepage())
  result = upstream.reader.home()

  assert result.account == "student-1"
  assert result.courses == {
    "course-1": "Course One",
    "course-2": "Course Two",
  }
  assert form_fields(result.page, "mainForm") == [("userType", "learner")]
  assert upstream.requests == [(HOME_URL, None)]


def test_home_can_validate_an_existing_page_without_another_request(upstream):
  page = soup_html(homepage())
  before = str(page)

  result = upstream.reader.home(page)

  assert result.page is page
  assert result.account == "student-1"
  assert result.courses == {
    "course-1": "Course One",
    "course-2": "Course Two",
  }
  assert str(page) == before
  assert upstream.requests == []


@pytest.mark.parametrize(
  ("body", "message"),
  [
    (homepage().replace("learner", "guest"), "session expired"),
    (homepage() + '<input type="password">', "session expired"),
    (homepage().replace("learner", "administrator"), "role"),
    (homepage().replace('name="mainForm"', 'name="other"'), "course form"),
    (homepage() + form("mainForm"), "course form"),
    (
      homepage().replace(
        "</form>",
        '<input type="hidden" name="userType" value="other"></form>',
        1,
      ),
      "conflicting field",
    ),
    (
      homepage() + "<p>An error has occurred while processing</p>",
      "application error",
    ),
    (homepage().replace("(student-1)", "(not an account)"), "archive account"),
    (form("mainForm", {"userType": "learner"}), "enrolled course"),
  ],
)
def test_supplied_homepage_does_not_bypass_reader_validation(
  upstream, body, message
):
  with pytest.raises(Failure, match=message):
    upstream.reader.home(soup_html(body))

  assert upstream.requests == []


@pytest.mark.parametrize("supplied", [False, True])
def test_home_cannot_be_requested_inside_a_course_context(upstream, supplied):
  upstream.reader.course = "course-1"

  with pytest.raises(Failure, match="within a course context"):
    upstream.reader.home(soup_html(homepage()) if supplied else None)

  assert upstream.requests == []


def test_home_requires_verified_account_and_recognized_courses(upstream):
  upstream.enqueue(form("mainForm", {"userType": "learner"}))

  with pytest.raises(Failure, match="enrolled course"):
    upstream.reader.home()

  upstream.enqueue(homepage().replace("(student-1)", "(other account)"))

  with pytest.raises(Failure, match="archive account"):
    upstream.reader.home()


def test_ambiguous_account_labels_are_rejected():
  page = soup_html(homepage().replace(
    "</p>", "<span>Other (student-2)</span></p>"
  ))

  with pytest.raises(Failure, match="archive account"):
    homepage_account(page)


@pytest.mark.parametrize(
  ("body", "status", "message"),
  [
    (form("mainForm", {"userType": "guest"}), 200, "session expired"),
    ('<input type="password">', 200, "session expired"),
    ("An error has occurred while processing", 200, "application error"),
    ("", 302, "unexpected redirect"),
    ("", 500, "HTTP 500"),
  ],
)
def test_reader_rejects_expired_and_failed_pages(
  upstream, body, status, message
):
  upstream.enqueue(body, status=status)

  with pytest.raises(Failure, match=message):
    upstream.reader.page(HOME_URL, "mainForm")

  assert upstream.closed == 1


def test_returned_course_context_must_match(upstream):
  upstream.reader.course = "course-1"
  upstream.enqueue(course_page("course-2"))

  with pytest.raises(Failure, match="returned course"):
    upstream.reader.page(
      CTL + "/Course.do?cmd=viewStudyHome&courseDTO.courseId=course-1",
      "courseForm",
    )


def test_course_page_uses_declared_character_encoding(upstream):
  upstream.enqueue(
    form("mainForm", {"userType": "learner"}, "<p>자료</p>"),
    charset="cp949",
  )
  page = upstream.reader.page(HOME_URL, "mainForm")

  assert "자료" in page.get_text()


def test_invalid_course_encoding_fails_without_replacement(upstream):
  upstream.enqueue(b"\xff")

  with pytest.raises(Failure, match="encoding"):
    upstream.reader.page(HOME_URL, "mainForm")


def test_post_verifies_returned_item_identity(upstream):
  upstream.reader.course = "course-1"
  original = soup_html(course_page(extra={"context": "keep"}))
  upstream.enqueue(course_page(extra={
    "boardContentsDTO.boardContentsId": "wrong",
  }))

  with pytest.raises(Failure, match="item context"):
    upstream.reader.post(
      original,
      "courseForm",
      "/Course.do",
      {
        "cmd": "viewBoardContents",
        "gubun": "V",
        "boardContentsDTO.boardContentsId": "expected",
      },
    )


def test_post_preserves_hidden_context_but_omits_it_from_source_url(upstream):
  upstream.reader.course = "course-1"
  original = soup_html(course_page(extra={"context": "private"}))
  upstream.enqueue(course_page(extra={
    "boardContentsDTO.boardContentsId": "post-1",
  }))

  _, source = upstream.reader.post(
    original,
    "courseForm",
    "/Course.do",
    {
      "cmd": "viewBoardContents",
      "gubun": "V",
      "boardContentsDTO.boardContentsId": "post-1",
    },
  )

  fields = parse_qs(upstream.requests[0][1].decode())
  assert fields["context"] == ["private"]
  assert "private" not in source


def test_syllabus_can_use_the_verified_external_selector(upstream):
  upstream.reader.course = "course-1"
  selector = (
    '<select name="courseDTO.courseId">'
    '<option selected value="course-1,metadata">Course</option>'
    "</select>"
  )
  upstream.enqueue(form("courseForm") + selector)

  page = upstream.reader.page(
    CTL + "/Course.do?cmd=viewCoursePlanChapterListNew"
    "&courseDTO.courseId=course-1",
    "courseForm",
  )
  assert syllabus_course_id(page) == "course-1"


@pytest.mark.parametrize(
  "selector",
  [
    '<input name="courseDTO.courseId" value="course-1">',
    '<select name="courseDTO.courseId" multiple></select>',
    '<select name="courseDTO.courseId" disabled></select>',
    '<select name="courseDTO.courseId" form="courseForm"></select>',
    (
      '<select name="courseDTO.courseId">'
      '<option value="course-1">Course</option></select>'
    ),
    (
      '<select name="courseDTO.courseId">'
      '<option selected disabled value="course-1">Course</option></select>'
    ),
    (
      '<select name="courseDTO.courseId">'
      '<option selected value="course-1">One</option>'
      '<option selected value="course-2">Two</option></select>'
    ),
  ],
)
def test_syllabus_selector_is_not_guessed(selector):
  with pytest.raises(Failure):
    syllabus_course_id(soup_html(selector))


def test_menu_redirect_is_checked_then_read(upstream):
  upstream.reader.course = "course-1"
  destination = (
    "/Course.do?cmd=viewCourseInfo&courseDTO.courseId=course-1"
  )
  upstream.enqueue("", status=302, headers=[("Location", destination)])
  upstream.enqueue(course_page())
  recipe = ("course", "/Course.do", "viewCourseInfo", "courseForm")

  _, source = upstream.reader.menu(
    CTL + "/Main.do?cmd=moveCourseMenu&courseDTO.courseId=course-1"
    "&mainDTO.menuId=menu_00053",
    recipe,
  )

  assert source == CTL + destination
  assert len(upstream.requests) == 2


@pytest.mark.parametrize(
  "destination",
  [
    "https://example.com/",
    "/Course.do?cmd=viewStudyHome&courseDTO.courseId=course-1",
    "/Course.do?cmd=viewCourseInfo&courseDTO.courseId=course-2",
  ],
)
def test_menu_cannot_change_origin_recipe_or_course(upstream, destination):
  upstream.reader.course = "course-1"
  upstream.enqueue("", status=302, headers=[("Location", destination)])

  with pytest.raises(Failure):
    upstream.reader.menu(
      CTL + "/Main.do?cmd=moveCourseMenu&courseDTO.courseId=course-1",
      ("course", "/Course.do", "viewCourseInfo", "courseForm"),
    )

  assert len(upstream.requests) == 1


def paginator_page(fields, base, total=2, current=1, name="courseForm"):
  script = (
    f"<script>new Paginator('pages', {total}, 10, {current}, "
    f"'{base}')</script>"
  )
  return soup_html(form(name, fields) + script)


def test_board_pagination_checks_outgoing_and_returned_scope(upstream):
  upstream.reader.course = "course-1"
  fields = {
    "courseDTO.courseId": "course-1",
    "boardInfoDTO.boardInfoId": "board-1",
  }
  base = (
    "/Course.do?cmd=viewBoardContentsList&courseDTO.courseId=course-1"
    "&boardInfoDTO.boardInfoId=board-1&curPage="
  )
  page = paginator_page(fields, base)
  upstream.enqueue(form("courseForm", fields))

  following = next_page(page, upstream.reader, "courseForm", 1)

  assert following is not None
  assert upstream.requests[0][0].endswith("curPage=2")


def test_pagination_rejects_changed_returned_board(upstream):
  upstream.reader.course = "course-1"
  fields = {
    "courseDTO.courseId": "course-1",
    "boardInfoDTO.boardInfoId": "board-1",
  }
  base = (
    "/Course.do?cmd=viewBoardContentsList&courseDTO.courseId=course-1"
    "&boardInfoDTO.boardInfoId=board-1&curPage="
  )
  upstream.enqueue(form("courseForm", {
    **fields, "boardInfoDTO.boardInfoId": "board-2",
  }))

  with pytest.raises(Failure, match="returned paginator context"):
    next_page(
      paginator_page(fields, base), upstream.reader, "courseForm", 1
    )


@pytest.mark.parametrize(
  ("total", "returned"),
  [(101, 1), (2, 2), (0, 2)],
)
def test_pagination_limits_and_current_page_are_checked(
  upstream, total, returned
):
  page = paginator_page({}, "/Course.do?curPage=", total, returned)

  with pytest.raises(Failure, match="paginator"):
    next_page(page, upstream.reader, "courseForm", 1)

  assert upstream.requests == []


def test_unrecognized_paginator_is_not_treated_as_complete(upstream):
  page = soup_html("<script>new Paginator(variable)</script>")

  with pytest.raises(Failure, match="unsupported paginator"):
    next_page(page, upstream.reader, "courseForm", 1)


def test_final_or_absent_paginator_does_not_request_another_page(upstream):
  final = paginator_page({}, "/Course.do?curPage=", total=1)
  assert next_page(final, upstream.reader, "courseForm", 1) is None
  assert next_page(soup_html("<p>End</p>"), upstream.reader, "courseForm", 1) is None
  assert upstream.requests == []


def test_collaboration_pagination_preserves_full_team_scope(upstream):
  scope = {
    "courseDTO.courseId": "course-1",
    "teamactInfoDTO.teamactId": "activity-1",
    "teamactInfoDTO.teamCategoryId": "category-1",
    "teamDTO.teamInfoId": "team-1",
    "teamactActivityDTO.activityId": "area-1",
  }
  base = "/Teamact.do?" + urlencode({
    "cmd": "viewTeamactInfo", "type": "coWork", **scope,
  }) + "&curPage="
  page = paginator_page(scope, base, name="teamactForm")
  upstream.reader.course = "course-1"
  upstream.enqueue(form("teamactForm", scope))

  assert next_page(
    page, upstream.reader, "teamactForm", 1, collaboration=scope
  ) is not None
  assert parse_qs(urlsplit(upstream.requests[0][0]).query)[
    "teamDTO.teamInfoId"
  ] == ["team-1"]


def test_collaboration_pagination_cannot_change_team(upstream):
  scope = {
    "courseDTO.courseId": "course-1",
    "teamDTO.teamInfoId": "team-1",
  }
  base = (
    "/Teamact.do?cmd=viewTeamactInfo&type=coWork"
    "&courseDTO.courseId=course-1&teamDTO.teamInfoId=team-2&curPage="
  )
  page = paginator_page(scope, base, name="teamactForm")

  with pytest.raises(Failure, match="scope"):
    next_page(
      page, upstream.reader, "teamactForm", 1, collaboration=scope
    )

  assert upstream.requests == []


def test_cleaning_accepts_fragments_without_a_page_wrapper():
  original = soup_html(
    "<!DOCTYPE html><head><title>Navigation</title></head>"
    '<div class="contentTitle"><h2>Page title</h2></div>'
    '<div class="searchBox"><label>Search</label><input name="q">'
    "<button>Search</button></div>"
    '<input type="hidden" name="context" value="private">'
    "<!-- internal comment --><script>initialize()</script>"
    "<style>.example { color: red }</style>"
    '<table><tr><th>Deadline</th><td>Friday</td></tr></table>'
  )
  before = str(original)

  cleaned = clean_content(original)

  assert cleaned is not original
  assert text_of(cleaned) == "Deadline Friday"
  assert cleaned.select_one("table") is not None
  assert cleaned.select_one("head, script, style, input") is None
  assert "DOCTYPE" not in str(cleaned)
  assert "internal comment" not in str(cleaned)
  assert str(original) == before
  assert str(clean_content(cleaned)) == str(cleaned)


@pytest.mark.parametrize(
  "panel",
  ["boardOption", "btnBox", "listFooter", "searchBox", "search_area"],
)
def test_unexplained_prose_in_action_panels_is_not_deleted(panel):
  original = soup_html(
    f'<div class="{panel}">Submit the report on Friday.'
    '<a href="#top">Top</a></div>'
  )

  assert "Submit the report on Friday." in text_of(clean_content(original))


@pytest.mark.parametrize(
  "panel",
  ["contentTitle", "boardTab", "boardOption", "btnBox", "groupTab"],
)
def test_source_table_inside_navigation_class_is_preserved(panel):
  original = soup_html(
    f'<div class="{panel}">'
    "<table><tr><th>Deadline</th><td>Friday</td></tr></table>"
    "</div>"
  )

  cleaned = clean_content(original)

  assert cleaned.select_one("table") is not None
  assert "Friday" in text_of(cleaned)


@pytest.mark.parametrize(
  "wrapper",
  [
    '<div id="listBox">{}</div>',
    '<div class="cont">{}</div>',
    '<div class="listContent">{}</div>',
    '<dl class="element"><dd>{}</dd></dl>',
    "<table><tr><td>{}</td></tr></table>",
    "<pre>{}</pre>",
  ],
)
def test_navigation_class_does_not_override_source_scope(wrapper):
  source = '<span class="btnBox">Source label</span>'
  original = soup_html(wrapper.format(source))

  assert str(clean_content(original)) == str(original)


@pytest.mark.parametrize(
  "source",
  [
    (
      '<span onclick="fileDownload(\'guide.pdf\', \'stored\', \'folder\')">'
      "Guide</span>"
    ),
    '<a href="/fileDownServlet?rFileName=guide.pdf&amp;sFileName=stored">Guide</a>',
    '<a href="/lmsdata/guide.pdf">Guide</a>',
    '<img src="/lmsdata/figure.png">',
    '<picture><source srcset="/lmsdata/figure.png"></picture>',
    "<svg><path d='M0 0'/></svg>",
    '<audio src="/lmsdata/recording.mp3"></audio>',
    '<video src="/lmsdata/recording.mp4"></video>',
    '<iframe src="/lesson"></iframe>',
    '<object data="/lesson"></object>',
    '<embed src="/lesson">',
    "<canvas></canvas>",
  ],
)
def test_navigation_panels_cannot_swallow_attachments_or_media(source):
  original = soup_html(
    '<div class="boardOption"><div class="btnBox">'
    + source + "</div></div>"
  )

  assert str(clean_content(original)) == str(original)


def test_attachment_control_on_panel_itself_is_preserved():
  original = soup_html(
    '<div class="btnBox" '
    'onclick="fileDownload(\'guide.pdf\', \'stored\', \'folder\')">'
    "Guide</div>"
  )

  assert str(clean_content(original)) == str(original)


@pytest.mark.parametrize("src", [None, "", "about:blank"])
def test_blank_transport_frames_are_removed_inside_nested_wrappers(src):
  attribute = "" if src is None else f' src="{src}"'
  original = soup_html(
    '<div id="listBox"><div class="listFooter">'
    f'<iframe name="hiddenFrame"{attribute}></iframe>'
    "</div><p>No data</p></div>"
  )

  cleaned = clean_content(original)

  assert cleaned.select_one("iframe") is None
  assert text_of(cleaned) == "No data"
  assert original.select_one("iframe") is not None


@pytest.mark.parametrize(
  "frame",
  [
    '<iframe name="hiddenFrame" src="/lesson"></iframe>',
    '<iframe name="hiddenFrame" srcdoc=""></iframe>',
    '<iframe name="hiddenFrame">Fallback content</iframe>',
    '<iframe name="hiddenFrame"><span></span></iframe>',
    '<iframe name="otherFrame"></iframe>',
    "<iframe></iframe>",
  ],
)
def test_nontransport_or_content_bearing_frames_remain_available(frame):
  original = soup_html('<div class="btnBox">' + frame + "</div>")

  cleaned = clean_content(original)

  assert cleaned.select_one("iframe") is not None
  assert str(cleaned) == str(original)


def test_cleaning_does_not_change_response_forms_or_pagination():
  page = soup_html(form(
    fields={"courseDTO.courseId": "course-1", "context": "preserved"},
    body=(
      '<div class="searchBox"><input name="q"><button>Search</button></div>'
      '<p>Source material</p>'
      "<script>new Paginator('pages', 2, 10, 1, '/Course.do?curPage=')</script>"
    ),
  ))
  before = str(page)
  fields = form_fields(page, "courseForm")

  cleaned = clean_content(page)

  assert "Source material" in text_of(cleaned)
  assert cleaned.select_one("script") is None
  assert form_fields(page, "courseForm") == fields
  assert str(page) == before


@pytest.mark.parametrize(
  "href",
  [
    "https://example.com/reading.pdf",
    "http://example.com/reference",
    "//example.com/reference",
    "reading.pdf",
    "/Reference.do?token=value",
    "/Course.do?cmd=viewBoardContents&gubun=V",
    "https://example.com/Main.do?cmd=viewHome",
    "mailto:instructor@example.com",
  ],
)
@pytest.mark.parametrize("panel", ["btnBox", "boardTab", "contentTitle"])
def test_reference_destination_takes_precedence_over_navigation_class(href, panel):
  original = soup_html(
    f'<div class="{panel}"><a class="btnBox" href="{href}">'
    "Required reference</a></div>"
  )

  cleaned = clean_content(original)

  assert str(cleaned) == str(original)
  assert cleaned.a["href"] == href


@pytest.mark.parametrize(
  "anchor",
  [
    '<a href="/">Home</a>',
    '<a href="/index.jsp">Home</a>',
    '<a href="/Course.do">Course</a>',
    '<a href="/Main.do?cmd=viewHome">Home</a>',
    '<a href="/Course.do?cmd=viewBoardContentsList">Board</a>',
    '<a href="#top">Top</a>',
    '<a onclick="viewStudyHome()">Home</a>',
    '<a href="javascript:searchSubmit()">Search</a>',
  ],
)
def test_known_navigation_remains_removable(anchor):
  cleaned = clean_content(soup_html(
    '<div class="btnBox">' + anchor + "</div><p>Source</p>"
  ))

  assert cleaned.select_one(".btnBox") is None
  assert text_of(cleaned) == "Source"


@pytest.mark.parametrize(
  "anchor",
  [
    '<a onclick="showRequiredMaterial()">Required material</a>',
    '<a href="javascript:showRequiredMaterial()">Required material</a>',
    '<a href="/Course.do?cmd=unknownView">Required material</a>',
    '<a href="/Course.do?cmd=viewHome">Required material</a>',
    '<a href="/Main.do?cmd=viewHome&cmd=unknownView">Required material</a>',
  ],
)
def test_unknown_or_conflicting_navigation_is_preserved(anchor):
  original = soup_html('<div class="btnBox">' + anchor + "</div>")

  assert str(clean_content(original)) == str(original)
