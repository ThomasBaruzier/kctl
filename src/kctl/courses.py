from __future__ import annotations

import copy
import re

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from .auth_html import text_of
from .core import (
  CTL,
  TEXT_LIMIT,
  Failure,
  ProtectionFailure,
  StorageFailure,
  digest,
  reader_url,
  require,
  valid_id,
)
from .reader import (
  EMPTY_TEXT,
  clean_content,
  field_value,
  form_fields,
  literal_calls,
  next_page,
  query_fields,
  source_url,
)

if TYPE_CHECKING:
  from .retrieval import Producer


MENUS = {
  "menu_00053": ("course", "/Course.do", "viewCourseInfo", "courseForm"),
  "menu_00052": (
    "syllabus", "/Course.do", "viewCoursePlanChapterListNew", "courseForm",
  ),
  "menu_00099": (
    "outline", "/Lesson.do", "viewLessonContentsList", "lessonForm",
  ),
  "menu_00082": (
    "attendance", "/Attend.do", "viewAttendLearnerList", "attendForm",
  ),
  "menu_00106": ("grades", "/Score.do", "viewScorePerson", "scoreForm"),
  "menu_00063": (
    "assignments", "/Report.do", "viewReportInfoPageList", "reportForm",
  ),
  "menu_00071": (
    "team-activities", "/Teamact.do", "viewTeamactList", "teamactForm",
  ),
  "menu_00065": (
    "discussion", "/Forum.do", "viewForumInfoList", "forumForm",
  ),
  "menu_00067": ("exams", "/Etest.do", "viewEtestInfoList", "etestForm"),
}

TEAM_FIELDS = (
  "courseDTO.courseId",
  "teamactInfoDTO.teamactId",
  "teamactInfoDTO.teamCategoryId",
  "teamDTO.teamInfoId",
)

EMPTY_MESSAGES = frozenset(
  value.rstrip(".!").casefold() for value in EMPTY_TEXT
)
LISTING_CONTENT = (
  "dl.element, a, img, picture, iframe, object, embed, canvas, svg, "
  "audio, video, input, textarea, select, button, [onclick]"
)


def _identifier(value: Any) -> str:
  require(
    valid_id(value) and "/" not in value and "\\" not in value,
    "invalid course item identity",
  )
  return value


def _arguments(calls: list[tuple[Any, ...]]) -> tuple[str, ...]:
  values = list(dict.fromkeys(call[1:] for call in calls))
  require(
    len(values) == 1,
    "missing or ambiguous activity identity",
  )
  _identifier(values[0][0])
  return values[0]


def _check_context(
  page: Any,
  form: str,
  expected: dict[str, str],
  *,
  required: bool = False,
) -> None:
  context = form_fields(page, form)

  for name, value in expected.items():
    actual = field_value(context, name)
    require(
      actual == value if required or actual else True,
      "returned activity context mismatch",
    )


def _team_scope(page: Any) -> dict[str, str]:
  context = form_fields(page, "teamactForm")
  return {
    name: _identifier(field_value(context, name))
    for name in TEAM_FIELDS
  }


def _content_region(page: Any) -> Any:
  body = page.select_one("#bodyContent")
  return clean_content(body) if body is not None else None


def _explicitly_empty(node: Any) -> bool:
  return (
    node is not None
    and text_of(node).rstrip(".!").casefold() in EMPTY_MESSAGES
    and not node.select(LISTING_CONTENT)
  )


def _empty_listing(page: Any) -> bool:
  scope = _content_region(page)
  if scope is None or scope.select("dl.element"):
    return False

  regions = [
    node
    for node in scope.select("#listBox")
    if node.find_parent(id="listBox") is None
  ]
  if not regions:
    return _explicitly_empty(scope)

  if not all(_explicitly_empty(region) for region in regions):
    return False

  for region in regions:
    region.decompose()

  return not text_of(scope) and not scope.select(LISTING_CONTENT)


def _collaboration_regions(page: Any) -> list[Any]:
  boxes = page.select("#bodyContent #listBox")

  if len(boxes) == 1:
    return boxes[0].select(
      ":scope > .listContent > dl.teamList > dd"
    )

  return boxes[-1:]


def _empty_markers(regions: list[Any]) -> list[Any]:
  return [
    marker
    for region in regions
    for marker in region.select(".search_unfine")
    if marker.find_parent("dl", class_="element") is None
    and text_of(region) == text_of(marker)
    and _explicitly_empty(region)
  ]


def verify_receipt(card: Any, team: bool) -> bool:
  cells = card.select("td")
  empty_message = "제출 내역이 없습니다."

  if team and any(
    text_of(cell).startswith(empty_message) for cell in cells
  ):
    tables = card.find_all("table")
    headings = card.find_all("dt", recursive=False)
    downloads = any(
      "fileDownload" in (
        str(node.get("onclick", "")) + " " + str(node.get("href", ""))
      )
      for node in card.select("[onclick], [href]")
    )
    require(
      len(tables) == 1
      and "boardListWrite" in tables[0].get("class", [])
      and tables[0].get("summary") == "산출물 제출 정보"
      and len(headings) == 1
      and text_of(headings[0]) == "제출정보"
      and not card.select("th")
      and len(cells) == 1
      and cells[0].get("colspan") == "2"
      and cells[0].find_parent("table") is tables[0]
      and text_of(cells[0]) == empty_message
      and not tables[0].select(
        "a, img, input, textarea, select, button, iframe, object, embed"
      )
      and not downloads,
      "unrecognized or mixed empty team-output layout",
    )
    return True

  headings = {
    re.sub(r"\s+", "", text_of(node)).casefold()
    for node in card.select("th")
  }
  require(
    bool(headings & {"제출상태", "submissionstatus", "status"})
    and bool(headings & {
      "첨부파일", "attachments", "attachedfile", "attachedfiles",
    }),
    "unrecognized receipt layout",
  )

  if team:
    require(
      bool(headings & {"산출물내용", "outputcontent"}),
      "unrecognized team-output content heading",
    )

  return False


class CourseTraversal:
  def __init__(self, producer: Producer):
    self.p = producer
    self.reader = producer.reader

  def run(self, course_id: str, title: str) -> None:
    key = "course/" + course_id
    require(
      self.reader.course == course_id
      and self.p.items.get(key, {}).get("course_id") == course_id,
      "course traversal was not prepared",
    )
    folder = self.p.items[key]["folder"]
    url = CTL + "/Course.do?" + urlencode({
      "cmd": "viewStudyHome",
      "courseDTO.courseId": course_id,
      "boardInfoDTO.boardInfoGubun": "study_home",
      "gubun": "study_course",
    })

    for recipe in MENUS.values():
      self.p.reserve(folder, recipe[0], recipe[0] + ".md")
    self.p.reserve(folder, "index.md", "attendance-offline.md")
    self.p.update(key, {"source": source_url(url)})

    home = self.reader.page(url, "courseForm")
    children = []
    seen = set()

    for anchor in home.select("a[href]"):
      href = anchor["href"]
      if not href.startswith(("/", CTL)):
        continue

      target = reader_url(href)
      parts = urlsplit(target)
      fields = query_fields(parts.query)
      if field_value(fields, "courseDTO.courseId") != course_id:
        continue

      command = field_value(fields, "cmd")

      if parts.path == "/Course.do" and command == "viewBoardContentsList":
        board_id = field_value(fields, "boardInfoDTO.boardInfoId")
        if not board_id or ("board", board_id) in seen:
          continue

        _identifier(board_id)
        seen.add(("board", board_id))
        child = key + "/boards/" + board_id
        name = text_of(anchor) or board_id
        destination = self.p.folder(child, folder, name)
        children.append(child)
        self.p.child(key, child)
        self.p.attempt(
          child, name, self.board,
          target, child, name, destination, key,
        )

      elif parts.path == "/Main.do" and command == "moveCourseMenu":
        menu = field_value(fields, "mainDTO.menuId")
        if menu not in MENUS or ("menu", menu) in seen:
          continue

        seen.add(("menu", menu))
        recipe = MENUS[menu]
        child = key + "/" + recipe[0]
        name = text_of(anchor) or recipe[0]
        children.append(child)
        self.p.child(key, child)
        self.p.attempt(
          child, name, self.section,
          target, recipe, child, name, folder, key,
        )

    require(seen, "no supported course navigation recognized")

    for menu, recipe in MENUS.items():
      if ("menu", menu) in seen:
        continue

      child = key + "/" + recipe[0]
      children.append(child)
      self.p.child(key, child)
      self.p.update(child, {
        "title": recipe[0],
        "status": "absent",
        "message": "menu not offered",
        "parent": key,
      })

    self.p.index(
      key, title, folder, children, source_url(url), parent="index"
    )

  def board(
    self,
    url: str,
    key: str,
    title: str,
    folder: str,
    parent: str,
  ) -> None:
    children = []
    seen = set()
    number = 1
    self.p.index(
      key, title, folder, [], source_url(url), False, parent
    )
    page = self.reader.page(url, "courseForm")
    fields = query_fields(urlsplit(url).query)
    board_id = field_value(fields, "boardInfoDTO.boardInfoId")

    if board_id:
      _check_context(
        page, "courseForm", {"boardInfoDTO.boardInfoId": board_id}
      )

    while page is not None:
      calls = literal_calls(
        page.select_one("#bodyContent"), "moveBoardView", 2
      )
      ids = {call[1] for call in calls}
      require(not ids or ids - seen, "board repeated a page")
      require(
        calls or _empty_listing(page),
        "unrecognized board layout",
      )

      for tag, post_id, row in calls:
        if post_id in seen:
          continue

        _identifier(post_id)
        seen.add(post_id)
        child = key + "/" + post_id
        children.append(child)
        self.p.child(key, child)
        self.p.attempt(
          child, text_of(tag) or post_id, self.post,
          page, tag, post_id, row, child, folder, key,
        )

      page = next_page(page, self.reader, "courseForm", number)
      number += 1

    self.p.index(
      key, title, folder, children, source_url(url), parent=parent
    )

  def post(
    self,
    page: Any,
    tag: Any,
    post_id: str,
    row: str,
    key: str,
    parent_folder: str,
    parent: str,
  ) -> None:
    title = text_of(tag) or post_id
    folder = self.p.folder(key, parent_folder, title)
    source = source_url(CTL + "/Course.do", [
      ("cmd", "viewBoardContents"),
      ("gubun", "V"),
      ("courseDTO.courseId", self.reader.course),
      ("boardContentsDTO.boardContentsId", post_id),
    ])
    self.p.prepare(key, title, folder + "/index.md", parent, source)
    card = tag.find_parent("dl", class_="element")
    problems = []

    if card is None or card.select_one(".cont") is None:
      detail, source = self.reader.post(page, "courseForm", "/Course.do", {
        "cmd": "viewBoardContents",
        "boardContentsDTO.boardContentsId": post_id,
        "boardContentsDTO.boardNo": row,
        "gubun": "V",
      })
      _check_context(detail, "courseForm", {
        "boardContentsDTO.boardContentsId": post_id,
      })
      cards = [
        node
        for node in detail.select("#bodyContent .listContent dl.element")
        if node.find_parent("dl", class_="element") is None
      ]
      require(
        len(cards) == 1 and cards[0].select_one(".cont") is not None,
        "unsupported board detail layout",
      )
      card = cards[0]
      comments = [
        node.get("value", "0")
        for node in detail.select("input[name]")
        if "commentcnt" in node["name"].lower()
      ]
      if any(value not in {"", "0"} for value in comments):
        problems.append("comment details not extracted")

    title = text_of(card.select_one("h4, h3")) or title
    self.p.content(
      key,
      title,
      folder + "/index.md",
      [card],
      source,
      limitations=problems,
      parent=parent,
    )

  def section(
    self,
    url: str,
    recipe: tuple[str, ...],
    key: str,
    title: str,
    folder: str,
    parent: str,
  ) -> None:
    kind = recipe[0]

    if kind in {"assignments", "team-activities"}:
      destination = folder + "/" + kind
      self.p.index(
        key, title, destination, [], complete=False, parent=parent
      )
      page, source = self.reader.menu(url, recipe)
      self.activities(
        page,
        key,
        title,
        destination,
        source,
        kind == "team-activities",
        parent,
      )
      return

    path = folder + "/" + kind + ".md"
    self.p.prepare(key, title, path, parent)
    page, source = self.reader.menu(url, recipe)
    boxes = page.select("#bodyContent #listBox")

    if kind == "course":
      require(len(boxes) >= 2, "course information region missing")
      nodes = boxes[:2]
    elif kind == "grades":
      table = page.select_one("#bodyContent table#table1")
      require(table is not None, "grade table missing")
      total = table.find_previous("table")
      headings = [text_of(node) for node in total.select("th")] if total else []
      nodes = [total, table] if headings in (["합 계"], ["Total"]) else [table]
    else:
      nodes = boxes

    status = "archived"
    limitations = []
    empty = False

    if kind in {"discussion", "exams"}:
      region = _content_region(page)
      nodes = [region] if region is not None else []
      empty = _empty_listing(page)
      status = "empty" if empty else "unsupported"
      if not empty:
        limitations.append(f"{kind} details are not supported")

    self.p.content(
      key,
      title,
      path,
      nodes,
      source,
      status=status,
      limitations=limitations,
      parent=parent,
      empty_listing=empty,
    )

    if kind == "attendance":
      child = key + "/offline"
      self.p.related(key, [child])
      self.p.attempt(
        child, title + " / offline", self.offline_attendance,
        page, child, folder, key,
      )

  def offline_attendance(
    self, page: Any, key: str, folder: str, parent: str
  ) -> None:
    path = folder + "/attendance-offline.md"
    self.p.prepare(key, "Offline attendance", path, parent)
    offline, source = self.reader.post(page, "attendForm", "/Attend.do", {
      "cmd": "viewAttendLearnerList",
      "boardInfoDTO.boardInfoGubun": "learner_attend",
      "boardInfoDTO.gubun": "offline",
    })
    region = _content_region(offline)
    nodes = [region] if region is not None else []
    empty = _empty_listing(offline)

    self.p.content(
      key,
      "Offline attendance",
      path,
      nodes,
      source,
      status="empty" if empty else "archived",
      parent=parent,
      empty_listing=empty,
    )

  def activities(
    self,
    page: Any,
    key: str,
    title: str,
    folder: str,
    source: str,
    team: bool,
    parent: str,
  ) -> None:
    children = []
    seen = set()
    unlinked = []
    number = 1
    form = "teamactForm" if team else "reportForm"
    self.p.index(key, title, folder, [], source, False, parent)

    while page is not None:
      cards = page.select("#bodyContent dl.element")
      require(
        cards or _empty_listing(page),
        "unrecognized activity list",
      )
      current = set()
      previous_unlinked = len(unlinked)

      for card in cards:
        calls = (
          literal_calls(card, "viewTeamactInfo", 2) if team
          else literal_calls(card, "submitReportView", 2)
          or literal_calls(card, "submitReport", 1)
        )
        if not calls and not team:
          unlinked.append(card)
          continue

        args = _arguments(calls)
        item_id = args[0]
        if item_id in current:
          continue
        current.add(item_id)
        if item_id in seen:
          continue

        child = key + "/" + item_id
        children.append(child)
        self.p.child(key, child)
        child_title = text_of(card.select_one("h4, h3")) or item_id
        destination = self.p.folder(child, folder, child_title)
        self.p.attempt(
          child, child_title, self.activity,
          page, card, args, child, child_title,
          destination, source, team, key,
        )

      require(
        not current or current - seen,
        "activity list repeated a page",
      )
      seen.update(current)

      if len(unlinked) > previous_unlinked:
        child = key + "/unlinked"
        require(
          "unlinked" not in seen,
          "activity identity conflicts with unlinked listing",
        )
        if child not in children:
          children.append(child)
          self.p.child(key, child)

        destination = self.p.folder(child, folder, "Unlinked assignments")
        self.p.content(
          child,
          "Assignments without detail links",
          destination + "/index.md",
          unlinked,
          source,
          parent=key,
        )

      page = next_page(page, self.reader, form, number)
      number += 1

    self.p.index(key, title, folder, children, source, parent=parent)

  def activity(
    self,
    page: Any,
    card: Any,
    args: tuple[str, ...],
    key: str,
    title: str,
    folder: str,
    source: str,
    team: bool,
    parent: str,
  ) -> None:
    require(
      len(args) in {1, 2} and (not team or len(args) == 2),
      "unsupported activity navigation",
    )
    self.p.reserve(folder, "own-team", "submission.md", "index.md")
    self.p.content(
      key, title, folder + "/index.md", [card], source, parent=parent
    )

    if team:
      child = key + "/own-team"
      self.p.related(key, [child])
      self.p.attempt(
        child, "Own team", self.own_team,
        page, args, child, folder + "/own-team", key,
      )
    elif len(args) == 2:
      child = key + "/submission"
      self.p.related(key, [child])
      self.p.attempt(
        child, "Submission", self.receipt,
        page, args, child, folder, key,
      )

  def receipt(
    self,
    page: Any,
    args: tuple[str, ...],
    key: str,
    folder: str,
    parent: str,
  ) -> None:
    path = folder + "/submission.md"
    self.p.prepare(key, "Submission", path, parent)
    receipt, source = self.reader.post(page, "reportForm", "/Report.do", {
      "cmd": "viewReportSubmit",
      "reportInfoDTO.reportInfoId": args[0],
      "reportInfoDTO.randomYn": args[1],
      "gubun": "A",
    })
    _check_context(receipt, "reportForm", {
      "reportInfoDTO.reportInfoId": args[0],
    })
    cards = receipt.select("#bodyContent dl.element")
    require(
      len(cards) == 2
      and all(
        card.find_parent("dl", class_="element") is None for card in cards
      ),
      "unsupported individual receipt layout",
    )
    verify_receipt(cards[1], team=False)

    self.p.content(
      key,
      "Submission",
      path,
      [cards[1]],
      source,
      parent=parent,
      role="submission",
    )

  def own_team(
    self,
    page: Any,
    args: tuple[str, ...],
    key: str,
    folder: str,
    parent: str,
  ) -> None:
    self.p.reserve(folder, "activity.md", "output.md", "index.md")
    self.p.index(
      key, "Own team", folder, [], complete=False, parent=parent
    )
    detail, source = self.reader.post(page, "teamactForm", "/Teamact.do", {
      "cmd": "viewTeamactInfo",
      "type": "coWork",
      "curPage": "1",
      "teamactInfoDTO.teamactId": _identifier(args[0]),
      "teamactInfoDTO.teamCategoryId": _identifier(args[1]),
    })
    _check_context(detail, "teamactForm", {
      "courseDTO.courseId": self.reader.course,
      "teamactInfoDTO.teamactId": args[0],
      "teamactInfoDTO.teamCategoryId": args[1],
    }, required=True)

    team_id = field_value(
      form_fields(detail, "teamactForm"), "teamDTO.teamInfoId"
    )
    if not team_id:
      self.p.update(key, {
        "status": "absent",
        "message": "no current-team context",
        "source": source,
      })
      return

    _identifier(team_id)
    cards = detail.select(
      "#bodyContent #listBox > .listContent > dl.element"
    )
    tables = detail.select(
      "#bodyContent #listBox > .listContent > dl.studyTarget > dd > "
      "table.boardListBasic"
    )
    require(
      len(cards) == len(tables) == 1
      and len(cards[0].select("table.boardListInfo")) == 1
      and cards[0].parent is tables[0].find_parent("dl").parent,
      "unsupported own-team layout",
    )
    children = [key + "/info"]
    self.p.child(key, children[0])
    self.p.content(
      children[0],
      "Team activity",
      folder + "/activity.md",
      cards + tables,
      source,
      parent=key,
    )

    seen = set()
    for option in detail.select(
      'select[name="teamactActivityDTO.activityId"] option[value]'
    ):
      item_id = option.get("value")
      if not item_id or item_id in seen:
        continue

      _identifier(item_id)
      seen.add(item_id)
      child = key + "/areas/" + item_id
      title = text_of(option) or item_id
      children.append(child)
      self.p.child(key, child)
      destination = self.p.folder(child, folder, title)
      self.p.attempt(
        child, title, self.team_area,
        detail, item_id, child, destination, key, title,
      )

    child = key + "/output"
    children.append(child)
    self.p.child(key, child)
    self.p.attempt(
      child, "Team output", self.team_output,
      detail, child, folder, key,
    )
    self.p.index(
      key, "Own team", folder, children, source, parent=parent
    )

  def team_area(
    self,
    detail: Any,
    item_id: str,
    key: str,
    folder: str,
    parent: str,
    title: str = "Collaboration",
  ) -> None:
    path = folder + "/index.md"
    self.p.prepare(key, title, path, parent)
    scope = _team_scope(detail)
    scope["teamactActivityDTO.activityId"] = _identifier(item_id)
    area, source = self.reader.post(detail, "teamactForm", "/Teamact.do", {
      "cmd": "viewTeamactInfo",
      "type": "coWork",
      "curPage": "1",
      "teamactActivityDTO.activityId": item_id,
    })
    _check_context(area, "teamactForm", scope, required=True)

    nodes = []
    limitations = []
    seen = set()
    empty = False
    total = 0

    for number in range(1, 101):
      regions = _collaboration_regions(area)
      populated = [
        region
        for region in regions
        if region.select(":scope > .listContent > dl.element")
      ]
      unknown = False

      if len(populated) == 1:
        added = [
          copy.deepcopy(card)
          for card in populated[0].select(
            ":scope > .listContent > dl.element"
          )
        ]
        for card in added:
          for editor in card.select('.partition[id^="commentRegForm_"]'):
            editor.decompose()

        page_hash = digest(
          "\n".join(str(card) for card in added).encode()
        )
        if page_hash in seen:
          limitations.append("collaboration repeated a page; traversal stopped")
          break

        seen.add(page_hash)
        limitations.append(
          "displayed posts and comments retained; comment completeness "
          "is not verified"
        )
      else:
        markers = _empty_markers(regions)
        if (
          len(markers) == 1
          and not nodes
          and all(_explicitly_empty(region) for region in regions)
        ):
          added = [copy.deepcopy(markers[0])]
          empty = True
        else:
          limitations.append("unrecognized collaboration content layout")
          fallback = regions or area.select("#bodyContent #listBox")
          added = [copy.deepcopy(region) for region in fallback]
          unknown = True

      size = sum(len(str(node).encode()) for node in added)
      if total + size > TEXT_LIMIT:
        limitations.append("collaboration content exceeds text limit")
        break

      total += size
      nodes.extend(added)
      if unknown:
        break

      try:
        following = next_page(
          area,
          self.reader,
          "teamactForm",
          number,
          collaboration=scope,
        )
        if following is None:
          break

        if empty:
          limitations.append("empty collaboration page advertises further pages")
          empty = False
        area = following

      except (ProtectionFailure, StorageFailure):
        raise
      except Failure as error:
        if error.code == 1:
          raise
        limitations.append(
          "collaboration pagination incomplete: " + error.message
        )
        break
    else:
      limitations.append("collaboration pagination exceeded limit")

    self.p.content(
      key,
      title,
      path,
      nodes,
      source,
      status="partial" if limitations else "empty" if empty else "archived",
      limitations=list(dict.fromkeys(limitations)),
      parent=parent,
      empty_listing=empty and not limitations,
    )

  def team_output(
    self, detail: Any, key: str, folder: str, parent: str
  ) -> None:
    path = folder + "/output.md"
    self.p.prepare(key, "Team output", path, parent)
    scope = _team_scope(detail)
    output, source = self.reader.post(detail, "teamactForm", "/Teamact.do", {
      "cmd": "teamactOutputInfo",
      "gubun": "V",
    })
    category = "teamactInfoDTO.teamCategoryId"
    if field_value(form_fields(output, "teamactForm"), category) is None:
      scope.pop(category)
    _check_context(output, "teamactForm", scope, required=True)

    cards = output.select("#bodyContent dl.element")
    require(
      len(cards) == 2
      and all(
        card.find_parent("dl", class_="element") is None for card in cards
      ),
      "unsupported team-output layout",
    )
    empty = verify_receipt(cards[1], team=True)
    receipt = copy.deepcopy(cards[1])

    if empty:
      table = receipt.find("table")
      flattened = output.new_tag("pre")
      flattened.string = table.get_text("\n", strip=True)
      table.replace_with(flattened)

    self.p.content(
      key,
      "Team output",
      path,
      [receipt],
      source,
      status="empty" if empty else "archived",
      parent=parent,
      role="submission",
    )
