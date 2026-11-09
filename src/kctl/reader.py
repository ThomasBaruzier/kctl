from __future__ import annotations

import copy
import re
import threading

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .auth_html import soup_html, text_of
from .core import (
  CTL,
  CTL_HOST,
  HOME_URL,
  Failure,
  bounded_text,
  reader_url,
  require,
  valid_id,
)
from .network import HTTP


READ_ROUTES = {
  "/Main.do": {"viewHome", "moveCourseMenu"},
  "/Study.do": {"viewStudyMyClassroom"},
  "/Course.do": {
    "viewStudyHome",
    "viewCourseInfo",
    "viewCoursePlanChapterListNew",
    "viewBoardContentsList",
    "viewBoardContents",
  },
  "/Report.do": {"viewReportInfoPageList", "viewReportSubmit"},
  "/Teamact.do": {
    "viewTeamactList", "viewTeamactInfo", "teamactOutputInfo",
  },
  "/Lesson.do": {"viewLessonContentsList"},
  "/Attend.do": {"viewAttendLearnerList"},
  "/Score.do": {"viewScorePerson"},
  "/Forum.do": {"viewForumInfoList"},
  "/Etest.do": {"viewEtestInfoList"},
}

CONTROL_FIELDS = frozenset({
  "cmd", "gubun", "type", "curPage", "courseDTO.courseId",
  "boardInfoDTO.boardInfoId", "boardContentsDTO.boardContentsId",
  "reportInfoDTO.reportInfoId", "reportInfoDTO.randomYn",
  "teamactInfoDTO.teamactId", "teamactInfoDTO.teamCategoryId",
  "teamactActivityDTO.activityId", "teamDTO.teamInfoId",
})

READ_MODES = {
  "viewReportSubmit": ("gubun", "A"),
  "viewBoardContents": ("gubun", "V"),
  "viewTeamactInfo": ("type", "coWork"),
  "teamactOutputInfo": ("gubun", "V"),
}

PAGINATORS = {
  "courseForm": ("/Course.do", "viewBoardContentsList"),
  "reportForm": ("/Report.do", "viewReportInfoPageList"),
  "teamactForm": ("/Teamact.do", "viewTeamactList"),
}

EMPTY_TEXT = (
  "조회된 내용이 없습니다",
  "등록된 내용이 없습니다",
  "검색된 내용이 없습니다",
  "There is no",
  "No data",
  "No results",
)

NAVIGATION_PANELS = (
  ".contentTitle, .boardTab, .boardOption, .btnBox, .listFooter, "
  ".searchBox, .groupTab, .search_area"
)
NAVIGATION_COMMANDS = frozenset({
  "viewHome", "viewStudyHome", "viewStudyMyClassroom", "moveCourseMenu",
  "viewBoardContentsList", "viewReportInfoPageList", "viewTeamactList",
  "viewForumInfoList", "viewEtestInfoList",
})
NAVIGATION_HANDLER = re.compile(
  r"(?:return\s+)?(?:"
  r"(?:searchSubmit|viewStudyHome|viewHome|moveCourseMenu)\s*\([^()]*\)"
  r"|addBoardContentsForm\s*\(\s*\)"
  r")\s*;?"
)
SOURCE_CLASSES = frozenset({"cont", "listContent", "element"})
MEDIA_TAGS = frozenset({
  "img", "picture", "svg", "audio", "video", "iframe",
  "object", "embed", "canvas",
})
ATTACHMENT_CONTROL = re.compile(
  r"\bfileDownload\s*\(|/(?:fileDownServlet|lmsdata/)"
)


@dataclass
class Home:
  page: Any
  account: str
  courses: dict[str, str]


def _navigation_link(node: Any) -> bool:
  href = str(node.get("href", "")).strip()
  handler = str(node.get("onclick", "")).strip()

  if handler.lower().startswith("javascript:"):
    handler = handler.split(":", 1)[1].strip()

  if handler and NAVIGATION_HANDLER.fullmatch(handler) is None:
    return False

  if not href or href.startswith("#"):
    return True

  if href.lower().startswith("javascript:"):
    script = href.split(":", 1)[1].strip()
    return NAVIGATION_HANDLER.fullmatch(script) is not None

  try:
    parts = urlsplit(reader_url(href))
    if not parts.query:
      return parts.path in READ_ROUTES or parts.path in {"/", "/index.jsp"}

    command = field_value(query_fields(parts.query), "cmd")
    return (
      command in NAVIGATION_COMMANDS
      and command in READ_ROUTES.get(parts.path, ())
    )
  except Failure:
    return False


def _navigation_panel(node: Any) -> bool:
  if any(
    parent.get("id") == "listBox"
    or parent.name in {"table", "pre", "code"}
    or SOURCE_CLASSES.intersection(parent.get("class", []))
    for parent in (node, *node.parents)
  ):
    return False

  if node.name in MEDIA_TAGS or node.select(
    "#listBox, dl.element, .cont, .listContent, table, pre, code, "
    "blockquote, img, picture, svg, audio, video, iframe, object, embed, canvas"
  ):
    return False

  if any(
    ATTACHMENT_CONTROL.search(
      str(control.get("onclick", "")) + " " + str(control.get("href", ""))
    )
    for control in (node, *node.select("[onclick], [href]"))
  ):
    return False

  if any(
    not _navigation_link(control)
    for control in (node, *node.select("a, [onclick]"))
    if control.name == "a" or control.has_attr("onclick")
  ):
    return False

  if (
    node.name in {"a", "button", "input", "select"}
    or {"contentTitle", "boardTab", "groupTab"}.intersection(node.get("class", []))
  ):
    return True

  return all(
    not value.strip()
    or any(
      parent.name in {"a", "button", "select", "label"}
      for parent in value.parents
    )
    for value in node.find_all(string=True)
  )


def clean_content(fragment: Any) -> Any:
  from bs4 import Comment, Declaration, Doctype, ProcessingInstruction

  region = copy.deepcopy(fragment)
  declarations = (Comment, Declaration, Doctype, ProcessingInstruction)

  for node in list(region.find_all(
    string=lambda value: isinstance(value, declarations)
  )):
    node.extract()

  for node in list(region.select("head, script, style, input[type=hidden]")):
    if node.parent is not None:
      node.decompose()

  for frame in list(region.select("iframe[name=hiddenFrame]")):
    if (
      str(frame.get("src", "")).strip() in {"", "about:blank"}
      and not frame.has_attr("srcdoc")
      and not text_of(frame)
      and frame.find(True) is None
    ):
      frame.decompose()

  for node in list(region.select(NAVIGATION_PANELS)):
    if node.parent is not None and _navigation_panel(node):
      node.decompose()

  return region


def query_fields(query: str) -> list[tuple[str, str]]:
  try:
    return parse_qsl(
      query, keep_blank_values=True, max_num_fields=256
    )
  except (ValueError, UnicodeError):
    raise Failure("malformed CTL query") from None


def field_value(fields: list[tuple[str, str]], name: str) -> str | None:
  values = [value for key, value in fields if key == name]
  require(
    not values or len(set(values)) == 1,
    f"conflicting field: {name}",
  )
  return values[0] if values else None


def _disabled(node: Any) -> bool:
  return (
    node.has_attr("disabled")
    or node.find_parent("fieldset", attrs={"disabled": True}) is not None
  )


def form_fields(page: Any, name: str) -> list[tuple[str, str]]:
  forms = page.find_all("form", attrs={"name": name})
  require(
    len(forms) == 1 and forms[0].find("form") is None,
    "missing or ambiguous course form",
  )
  result = []

  for node in forms[0].select("input[name]"):
    if str(node.get("type", "")).lower() != "hidden" or _disabled(node):
      continue
    require(
      not node.has_attr("form"),
      "explicitly associated course control",
    )
    result.append((node["name"], node.get("value", "")))

  for node in forms[0].select("select[name]"):
    if _disabled(node):
      continue

    require(
      not node.has_attr("form") and not node.has_attr("multiple"),
      "unsupported course selector",
    )
    choices = node.select("option[selected]") or node.select("option")[:1]
    require(len(choices) <= 1, "ambiguous course selection")

    if choices:
      option = choices[0]
      require(
        not option.has_attr("disabled")
        and option.find_parent("optgroup", attrs={"disabled": True}) is None,
        "disabled course selection",
      )
      result.append((node["name"], option.get("value", "")))

  _validate_fields(result)
  return result


def _validate_fields(fields: list[tuple[str, str]]) -> None:
  require(len(fields) <= 256, "too many course form fields")

  for key, value in fields:
    require(
      bounded_text(key, 256)
      and key
      and isinstance(value, str)
      and len(value) <= 65536
      and "\x00" not in value,
      "malformed course form field",
    )


def changed_fields(
  fields: list[tuple[str, str]], changes: dict[str, str]
) -> list[tuple[str, str]]:
  return [
    (key, value) for key, value in fields if key not in changes
  ] + list(changes.items())


def canonical_fields(
  fields: list[tuple[str, str]]
) -> list[tuple[str, str]]:
  _validate_fields(fields)
  result = []
  seen = set()

  for key, value in fields:
    if key in CONTROL_FIELDS:
      field_value(fields, key)
      if key in seen:
        continue
      seen.add(key)
    result.append((key, value))

  return result


def source_url(
  url: str, fields: list[tuple[str, str]] | None = None
) -> str:
  parts = urlsplit(reader_url(url))
  values = query_fields(parts.query) + (fields or [])
  selected = [
    (key, value)
    for key, value in canonical_fields(values)
    if key in CONTROL_FIELDS
  ]
  return urlunsplit((
    "https", CTL_HOST, parts.path, urlencode(selected), "",
  ))


def literal_calls(
  node: Any, name: str, count: int
) -> list[tuple[Any, ...]]:
  require(node is not None, "content region missing")
  argument = r"\s*'([^'\\\r\n]*)'\s*"
  pattern = re.compile(
    r"\b" + re.escape(name) + r"\("
    + ",".join([argument] * count) + r"\)"
  )
  result = []

  for tag in node.select("[href], [onclick]"):
    handler = tag.get("onclick", "") + " " + tag.get("href", "")
    result.extend(
      (tag, *match.groups()) for match in pattern.finditer(handler)
    )

  return result


def syllabus_course_id(page: Any) -> str:
  controls = page.find_all(attrs={"name": "courseDTO.courseId"})
  require(
    len(controls) == 1,
    "missing or ambiguous syllabus course selector",
  )
  selector = controls[0]
  require(
    selector.name == "select"
    and selector.find_parent("form") is None
    and not selector.has_attr("form")
    and not selector.has_attr("multiple")
    and not _disabled(selector),
    "unsupported syllabus course selector",
  )

  selected = selector.select("option[selected]")
  require(
    len(selected) == 1,
    "missing or ambiguous syllabus course selection",
  )
  option = selected[0]
  require(
    not option.has_attr("disabled")
    and option.find_parent("optgroup", attrs={"disabled": True}) is None,
    "disabled syllabus course selection",
  )
  value = option.get("value")
  require(
    isinstance(value, str) and value and "\x00" not in value,
    "missing syllabus course selection value",
  )
  course_id = value.split(",", 1)[0]
  require(valid_id(course_id), "invalid syllabus course selection")
  return course_id


def homepage_account(page: Any) -> str:
  accounts = []

  for span in page.select("#header .login fieldset p span"):
    match = re.search(r"\(([^()\s]+)\)\s*$", text_of(span))
    if match:
      accounts.append(match[1])

  require(
    len(set(accounts)) == 1 and valid_id(accounts[0]),
    "cannot verify archive account",
  )
  return accounts[0]


def _page_fields(page: Any, form: str) -> list[tuple[str, str]]:
  require(
    page.select_one(
      'input[name="userType"][value="guest"], input[type=password]'
    ) is None,
    "session expired; run kctl login",
    1,
  )
  require(
    "an error has occurred while processing" not in text_of(page).lower(),
    "CTL application error",
  )
  return form_fields(page, form)


class Reader:
  def __init__(self, jar: Any, stop: threading.Event):
    self.http = HTTP(jar, 2000, stop)
    self.course = ""

  def request(
    self,
    url: str,
    fields: list[tuple[str, str]] | None = None,
  ) -> tuple[str, bytes | None]:
    parts = urlsplit(reader_url(url))
    query = query_fields(parts.query)
    require(fields is None or not query, "POST recipe has URL query")
    values = canonical_fields(query if fields is None else fields)
    command = field_value(values, "cmd")

    require(
      command in READ_ROUTES.get(parts.path, set()),
      "unsupported CTL read",
    )

    if self.course:
      require(
        field_value(values, "courseDTO.courseId") == self.course,
        "outgoing course context mismatch",
      )

    if command in READ_MODES:
      name, expected = READ_MODES[command]
      require(
        field_value(values, name) == expected,
        "unsupported read mode",
      )

    encoded = urlencode(values)
    target = urlunsplit((
      "https",
      CTL_HOST,
      parts.path,
      encoded if fields is None else "",
      "",
    ))
    return target, None if fields is None else encoded.encode("ascii")

  def page(
    self,
    url: str,
    form: str,
    fields: list[tuple[str, str]] | None = None,
  ) -> Any:
    target, data = self.request(url, fields)

    with self.http.open(target, data) as response:
      require(
        response.status not in {301, 302, 303, 307, 308},
        "unexpected redirect; run kctl login",
        1,
      )
      require(
        response.status == 200,
        f"CTL returned HTTP {response.status}",
      )
      require(
        response.headers.get_content_type() in {"text/html", "text/plain"},
        "expected course HTML",
      )
      raw = self.http.read(response)
      encoding = response.headers.get_content_charset() or "utf-8"

    try:
      page = soup_html(raw.decode(encoding))
    except (UnicodeError, LookupError, ValueError):
      raise Failure("unsupported course encoding") from None

    context = _page_fields(page, form)
    self._check_context(page, context, target, form, fields)
    return page

  def _check_context(
    self,
    page: Any,
    context: list[tuple[str, str]],
    target: str,
    form: str,
    fields: list[tuple[str, str]] | None,
  ) -> None:
    if self.course:
      returned = field_value(context, "courseDTO.courseId")
      parts = urlsplit(target)
      outgoing = fields if fields is not None else query_fields(parts.query)

      if (
        returned is None
        and parts.path == "/Course.do"
        and field_value(outgoing, "cmd") == "viewCoursePlanChapterListNew"
        and form == "courseForm"
      ):
        returned = syllabus_course_id(page)

      require(
        returned is not None and returned.split(",")[0] == self.course,
        "returned course context mismatch",
      )

    if fields is None:
      return

    for name in (
      "boardContentsDTO.boardContentsId",
      "reportInfoDTO.reportInfoId",
      "teamactInfoDTO.teamactId",
      "teamDTO.teamInfoId",
    ):
      expected = field_value(fields, name)
      actual = field_value(context, name)
      if expected and actual:
        require(expected == actual, "returned item context mismatch")

  def home(self, page: Any = None) -> Home:
    require(not self.course, "homepage requested within a course context")
    if page is None:
      page = self.page(HOME_URL, "mainForm")

    require(
      field_value(_page_fields(page, "mainForm"), "userType") == "learner",
      "authenticated homepage role could not be verified",
      1,
    )
    courses = {}

    for tag, course_id in literal_calls(page, "viewMyCourse", 1):
      require(valid_id(course_id), "invalid enrolled course identity")
      title = text_of(tag)
      if course_id not in courses or title:
        courses[course_id] = title or course_id

    require(courses, "no enrolled course navigation recognized")
    return Home(page, homepage_account(page), courses)

  def post(
    self,
    page: Any,
    form: str,
    route: str,
    changes: dict[str, str],
  ) -> tuple[Any, str]:
    fields = changed_fields(form_fields(page, form), changes)
    url = CTL + route
    return self.page(url, form, fields), source_url(url, fields)

  def menu(
    self, url: str, recipe: tuple[str, ...]
  ) -> tuple[Any, str]:
    target, data = self.request(url)
    require(data is None, "unexpected menu request body")

    with self.http.open(target) as response:
      require(
        response.status == 302,
        "unexpected course-menu response",
      )
      locations = response.headers.get_all("Location", [])
      require(len(locations) == 1, "ambiguous course-menu redirect")
      destination = reader_url(urljoin(target, locations[0]))

    parts = urlsplit(destination)
    fields = query_fields(parts.query)
    require(
      parts.path == recipe[1]
      and field_value(fields, "cmd") == recipe[2],
      "course-menu destination changed",
    )

    return self.page(destination, recipe[3]), source_url(destination)


def next_page(
  page: Any,
  reader: Reader,
  form: str,
  current: int,
  collaboration: dict[str, str] | None = None,
) -> Any:
  source = str(page)
  matches = re.findall(
    r"new\s+Paginator\(\s*'[^']+',\s*(\d+),\s*\d+,\s*(\d+),"
    r"\s*'([^']+)'\s*\)",
    source,
  )

  if not matches:
    require(
      re.search(r"\bnew\s+Paginator\s*\(", source) is None,
      "unsupported paginator layout",
    )
    return None

  require(len(set(matches)) == 1, "ambiguous pagination")
  total, returned, base = matches[0]
  total = int(total)
  require(
    int(returned) == current
    and 0 <= total <= 100
    and 1 <= current <= max(total, 1)
    and (collaboration is None or total >= 1),
    "paginator repeated a page or exceeded limit",
  )

  if current >= total:
    return None

  require(base.endswith("curPage="), "unsupported pagination URL")
  require(form in PAGINATORS, "unsupported pagination form")
  target = reader_url(base + str(current + 1))
  parts = urlsplit(target)
  fields = query_fields(parts.query)
  context = form_fields(page, form)
  scope = {
    name: field_value(context, name)
    for name in ("courseDTO.courseId", "boardInfoDTO.boardInfoId")
    if field_value(context, name)
  }
  route, command = PAGINATORS[form]

  if collaboration is not None:
    route, command = "/Teamact.do", "viewTeamactInfo"
    scope.update(collaboration)
    require(
      field_value(fields, "type") == "coWork",
      "paginator changed collaboration mode",
    )

  require(
    parts.path == route
    and field_value(fields, "cmd") == command
    and field_value(fields, "curPage") == str(current + 1),
    "paginator changed read",
  )

  for name, expected in scope.items():
    require(
      field_value(fields, name) == expected,
      "paginator changed scope",
    )

  result = reader.page(target, form)
  returned_fields = form_fields(result, form)

  for name, expected in scope.items():
    require(
      field_value(returned_fields, name) == expected,
      "returned paginator context mismatch",
    )

  return result
