import copy
import io
import queue
import sys
import threading

from collections import deque
from contextlib import contextmanager
from email.message import Message
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import kctl
import kctl.retrieval as retrieval

from kctl.archive import Archive
from kctl.auth_html import soup_html
from kctl.conversion import Engines
from kctl.core import (
  CTL,
  INDEX_URL,
  LOGIN_POST_URL,
  Cancelled,
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  digest,
  identity,
  require,
)
from kctl.exports import export_courses
from kctl.processing import DocumentJob
from kctl.reader import Home, clean_content
from kctl.retrieval import (
  Producer,
  page_markdown,
  response_length,
  response_validators,
  validate_html_attachment,
  validate_prefix,
)
from kctl.storage import Tree, signature, write_new


OWNER = "course/one/page"
FOLDER = "documents/Course"
PAGE = FOLDER + "/page.md"
URL = CTL + "/fileDownServlet?id=1"
PDF = b"%PDF-1.7\nexample"


class Response:
  def __init__(
    self,
    raw=PDF,
    *,
    status=200,
    mime="application/octet-stream",
    headers=(),
  ):
    self.status = status
    self.headers = Message()
    self.headers["Content-Type"] = mime
    for name, value in headers:
      self.headers[name] = value
    self.body = io.BytesIO(raw)
    self.closed = False


@pytest.fixture
def setup(private_dir):
  tree = Tree(private_dir / "archive")
  tree.mkdir(".ctl/work")
  state = SimpleNamespace(
    tree=tree,
    responses=deque(),
    requests=[],
    events=queue.Queue(),
    stop=threading.Event(),
    snapshot={
      "items": {
        OWNER: {
          "title": "Page",
          "folder": FOLDER,
          "path": PAGE,
          "status": "archived",
          "documents": ["previous-document"],
        },
      },
      "documents": {},
    },
    options=SimpleNamespace(
      course=None,
      refresh=False,
      include_submissions=True,
    ),
    page_calls=[],
    page=None,
  )

  class HTTP:
    requests = 0
    bytes = 0
    seconds = 0.0

    @contextmanager
    def open(self, url, headers=None):
      if state.stop.is_set():
        raise Cancelled()

      self.requests += 1
      state.requests.append((url, dict(headers or {})))
      assert state.responses, "unexpected attachment request"
      response = state.responses.popleft()

      if isinstance(response, BaseException):
        raise response

      try:
        yield response
      finally:
        response.closed = True

    def chunks(self, response, limit, seconds):
      total = 0
      while chunk := response.body.read(8):
        total += len(chunk)
        self.bytes += len(chunk)
        require(total <= limit, "response exceeds byte limit")
        yield chunk

  def read_page(url, form):
    state.page_calls.append((url, form))
    if isinstance(state.page, BaseException):
      raise state.page
    assert state.page is not None, "unexpected page request"
    return state.page

  state.reader = SimpleNamespace(
    http=HTTP(),
    course="",
    page=read_page,
  )
  state.home = Home(
    soup_html("<p>Verified homepage</p>"),
    "student-1",
    {"one": "Course"},
  )

  def producer():
    return Producer(
      reader=state.reader,
      root=tree.root,
      snapshot=copy.deepcopy(state.snapshot),
      options=state.options,
      events=state.events,
      stop=state.stop,
      home=state.home,
    )

  state.producer = producer

  try:
    yield state
  finally:
    tree.close()


def drain(events):
  result = []
  while not events.empty():
    result.append(events.get_nowait())
  return result


def install_download(setup, event):
  record = copy.deepcopy(event.data["record"])
  work = event.data["work"]
  assert work is not None
  setup.tree.move(work + "/new", record["folder"])
  original = setup.tree.path(record["folder"] + "/" + record["original"])
  record["signature"] = signature(original)
  setup.snapshot["documents"][event.data["key"]] = record
  return record


def cached_document(setup, *, headers=()):
  setup.responses.append(Response(headers=headers))
  producer = setup.producer()
  key, _ = producer.download(OWNER, FOLDER, URL, "original.pdf")
  completion = drain(setup.events)[0]
  record = install_download(setup, completion)
  setup.requests.clear()
  return key, record


def content_node(body):
  return soup_html('<div class="cont">' + body + "</div>").div


def attachment(name="notes.pdf"):
  return (
    '<a href="javascript:fileDownload('
    f"'{name}','stored','folder')\">{name}</a>"
  )


def test_download_hands_off_a_private_original_and_identity(setup):
  response = Response(headers=[("Content-Length", str(len(PDF)))])
  setup.responses.append(response)
  producer = setup.producer()

  key, record = producer.download(OWNER, FOLDER, URL, "original.pdf")
  events = drain(setup.events)

  assert len(events) == 1
  assert events[0].kind == "document"
  assert events[0].data["key"] == key
  work = events[0].data["work"]
  assert setup.tree.read(work + "/new/original.pdf", 100) == PDF
  assert record["sha256"] == digest(PDF)
  assert record["bytes"] == len(PDF)
  assert record["role"] == "attachment"
  assert record["result"] is None
  assert record["cache"] == "downloaded"
  assert response.closed
  assert producer.downloaded == 1
  assert not setup.tree.exists(record["folder"])


def test_duplicate_download_in_one_run_uses_the_same_event(setup):
  setup.responses.append(Response())
  producer = setup.producer()

  first = producer.download(OWNER, FOLDER, URL, "original.pdf")
  second = producer.download(OWNER, FOLDER, URL, "original.pdf")

  assert first == second
  assert len(setup.requests) == 1
  assert len(drain(setup.events)) == 1


def test_conflicting_roles_for_the_same_document_are_rejected(setup):
  setup.responses.append(Response())
  producer = setup.producer()
  producer.download(OWNER, FOLDER, URL, "original.pdf")

  with pytest.raises(Failure, match="conflicting attachment roles"):
    producer.download(
      OWNER, FOLDER, URL, "original.pdf", role="submission"
    )


def test_download_requires_assigned_owner_and_matching_folder(setup):
  producer = setup.producer()

  with pytest.raises(Failure, match="owner"):
    producer.download("missing", FOLDER, URL, "original.pdf")

  with pytest.raises(Failure, match="folder"):
    producer.download(OWNER, "documents/Other", URL, "original.pdf")

  assert setup.requests == []


@pytest.mark.parametrize(
  "url",
  [
    "https://example.com/file.pdf",
    CTL + "/Course.do?cmd=viewStudyHome",
    CTL + "/private/original.pdf",
  ],
)
def test_download_location_is_restricted(setup, url):
  with pytest.raises(Failure):
    setup.producer().download(OWNER, FOLDER, url, "original.pdf")

  assert setup.requests == []


def test_submission_download_is_blocked_at_the_download_boundary(setup):
  setup.options.include_submissions = False

  with pytest.raises(Failure, match="excluded"):
    setup.producer().download(
      OWNER, FOLDER, URL, "submission.pdf", role="submission"
    )

  assert setup.requests == []


def test_unvalidated_cached_original_is_reused_without_network(setup):
  key, previous = cached_document(setup)
  producer = setup.producer()

  returned_key, record = producer.download(
    OWNER, FOLDER, URL, "original.pdf"
  )
  events = drain(setup.events)

  assert returned_key == key
  assert record["sha256"] == previous["sha256"]
  assert record["cache"] == "local"
  assert events[0].data["work"] is None
  assert setup.requests == []
  assert producer.reused == 1


def test_etag_revalidation_preserves_original(setup):
  _, previous = cached_document(setup, headers=[("ETag", '"first"')])
  setup.responses.append(Response(
    b"", status=304, headers=[("ETag", '"second"')]
  ))
  producer = setup.producer()

  _, record = producer.download(OWNER, FOLDER, URL, "original.pdf")

  assert setup.requests == [(URL, {"If-None-Match": '"first"'})]
  assert record["etag"] == '"second"'
  assert record["signature"] == previous["signature"]
  assert record["cache"] == "revalidated"
  assert producer.revalidated == 1
  assert drain(setup.events)[0].data["work"] is None
  assert not list(setup.tree.path(".ctl/work").glob("*/new"))


def test_last_modified_is_used_when_etag_is_absent(setup):
  modified = "Wed, 04 Nov 2026 00:00:00 GMT"
  cached_document(setup, headers=[("Last-Modified", modified)])
  setup.responses.append(Response(b"", status=304))

  setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert setup.requests == [(URL, {"If-Modified-Since": modified})]


def test_refresh_fetches_an_original_without_validators(setup):
  cached_document(setup)
  setup.options.refresh = True
  setup.responses.append(Response())

  _, record = setup.producer().download(
    OWNER, FOLDER, URL, "original.pdf"
  )

  assert setup.requests == [(URL, {})]
  assert record["cache"] == "downloaded"
  assert drain(setup.events)[0].data["work"] is None


def test_refresh_still_uses_existing_validators(setup):
  cached_document(setup, headers=[("ETag", '"first"')])
  setup.options.refresh = True
  setup.responses.append(Response(b"", status=304))

  setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert setup.requests == [(URL, {"If-None-Match": '"first"'})]


def test_changed_remote_original_gets_a_new_staged_bundle(setup):
  _, old = cached_document(setup, headers=[("ETag", '"old"')])
  newer = b"%PDF-1.7\nupdated"
  setup.responses.append(Response(newer))

  _, record = setup.producer().download(
    OWNER, FOLDER, URL, "new-display-name.pdf"
  )
  completion = drain(setup.events)[0]

  assert completion.data["work"] is not None
  assert record["folder"] == old["folder"]
  assert record["original"] == old["original"]
  assert record["name"] == "new-display-name.pdf"
  assert record["sha256"] == digest(newer)
  assert "etag" not in record


def test_damaged_cached_original_requires_refresh(setup):
  _, old = cached_document(setup)
  setup.tree.write(
    old["folder"] + "/" + old["original"],
    b"%PDF-1.7\ndamaged",
  )

  with pytest.raises(SourceProblem):
    setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert setup.requests == []


def test_refresh_repairs_damaged_original_without_conditional_headers(setup):
  _, old = cached_document(setup, headers=[("ETag", '"old"')])
  setup.tree.write(
    old["folder"] + "/" + old["original"], b"damaged"
  )
  setup.options.refresh = True
  setup.responses.append(Response())

  _, record = setup.producer().download(
    OWNER, FOLDER, URL, "original.pdf"
  )

  assert setup.requests == [(URL, {})]
  assert record["sha256"] == digest(PDF)
  assert drain(setup.events)[0].data["work"] is not None


def test_unexpected_not_modified_response_is_rejected(setup):
  setup.responses.append(Response(b"", status=304))

  with pytest.raises(Failure, match="unexpected not-modified"):
    setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert list(setup.tree.path(".ctl/work").iterdir()) == []


@pytest.mark.parametrize(
  ("response", "message"),
  [
    (Response(b"", status=302), "redirected"),
    (Response(b"", status=500), "HTTP 500"),
    (Response(b""), "empty or incomplete"),
    (Response(b"not a PDF"), "PDF signature"),
    (
      Response(PDF, headers=[("Content-Length", str(len(PDF) + 1))]),
      "incomplete",
    ),
    (
      Response(b"<html>Login</html>", mime="text/html"),
      "returned HTML",
    ),
  ],
)
def test_failed_downloads_are_not_handed_off(setup, response, message):
  setup.responses.append(response)

  with pytest.raises(Failure, match=message):
    setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert drain(setup.events) == []
  assert list(setup.tree.path(".ctl/work").iterdir()) == []
  assert response.closed


def test_transfer_limit_is_enforced_and_workspace_cleaned(setup, monkeypatch):
  monkeypatch.setattr(retrieval, "FILE_LIMIT", 8)
  setup.responses.append(Response(PDF))

  with pytest.raises(Failure, match="byte limit"):
    setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert list(setup.tree.path(".ctl/work").iterdir()) == []


def test_connection_failure_cleans_workspace(setup):
  setup.responses.append(Failure("connection failed"))

  with pytest.raises(Failure, match="connection failed"):
    setup.producer().download(OWNER, FOLDER, URL, "original.pdf")

  assert list(setup.tree.path(".ctl/work").iterdir()) == []


def test_direct_html_attachment_is_preserved_for_later_conversion(setup):
  raw = (
    b"<!doctype html><html><head><title>Lesson</title></head>"
    b"<body><h1>Lesson</h1><pre>print('hello')</pre></body></html>"
  )
  setup.responses.append(Response(raw, mime="text/html"))

  _, record = setup.producer().download(
    OWNER, FOLDER, URL, "lesson.html"
  )
  completion = drain(setup.events)[0]

  assert record["original"] == "lesson.html"
  assert record["sha256"] == digest(raw)
  assert setup.tree.read(
    completion.data["work"] + "/new/lesson.html", len(raw)
  ) == raw


def test_html_source_with_an_ordinary_password_form_is_not_rejected(setup):
  raw = (
    '<form action="/example">'
    '<input name="username"><input type="password" name="password">'
    "</form>"
  ).encode()
  setup.responses.append(Response(raw, mime="text/html"))

  _, record = setup.producer().download(
    OWNER, FOLDER, URL, "example.htm"
  )

  assert record["bytes"] == len(raw)


@pytest.mark.parametrize(
  "body",
  [
    (
      f'<form name="idLoginForm" action="{LOGIN_POST_URL}">'
      '<input name="loginName"><input name="password" type="password">'
      "</form>"
    ),
    (
      '<form name="mainForm" action="/Main.do">'
      '<input type="hidden" name="userType" value="guest"></form>'
    ),
    "<p>An error has occurred while processing</p>",
  ],
)
def test_known_upstream_pages_are_not_saved_as_html_attachments(setup, body):
  setup.responses.append(Response(body.encode(), mime="text/html"))

  with pytest.raises(Failure):
    setup.producer().download(OWNER, FOLDER, URL, "lesson.html")

  assert drain(setup.events) == []
  assert list(setup.tree.path(".ctl/work").iterdir()) == []


def test_html_validation_reads_a_bounded_prefix(private_dir, monkeypatch):
  path = private_dir / "lesson.html"
  write_new(path, b"<p>Lesson</p>" + b"x" * 100)
  monkeypatch.setattr(retrieval, "HTML_LIMIT", 20)

  validate_html_attachment(path)


@pytest.mark.parametrize(
  "raw",
  [
    b"<!doctype html><html>Login",
    b"\xef\xbb\xbf<html>Login",
    b" \n<script>redirect()</script>",
  ],
)
def test_html_prefix_is_rejected_for_non_html_attachments(raw):
  with pytest.raises(Failure, match="returned HTML"):
    validate_prefix(raw, ".txt", False)


@pytest.mark.parametrize(
  "raw",
  [
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
    b"RIFF1234WEBP",
  ],
)
def test_supported_inline_raster_signatures(raw):
  validate_prefix(raw, ".img", True)


def test_response_validators_ignore_ambiguous_or_unsafe_values():
  headers = Message()
  headers["ETag"] = '"one"'
  headers["ETag"] = '"two"'
  headers["Last-Modified"] = "safe"

  assert response_validators(headers) == {"last_modified": "safe"}


@pytest.mark.parametrize("value", ["", "-1", "abc", "0", "9" * 30])
def test_invalid_content_lengths_are_rejected(value):
  headers = Message()
  headers["Content-Length"] = value

  with pytest.raises(Failure):
    response_length(headers)


def test_multiple_content_lengths_are_rejected():
  headers = Message()
  headers["Content-Length"] = "1"
  headers["Content-Length"] = "1"

  with pytest.raises(Failure, match="ambiguous"):
    response_length(headers)


def test_absent_content_length_is_allowed():
  assert response_length(Message()) is None


def test_filename_allocation_avoids_reserved_case_variants(setup):
  producer = setup.producer()
  producer.reserve(FOLDER, "Report")

  assert producer.allocate(FOLDER, "report") == FOLDER + "/report (2)"


def test_events_are_snapshots_not_shared_mutable_records(setup):
  producer = setup.producer()
  changes = {"title": "Original", "children": ["one"]}
  producer.update("example", changes)
  changes["children"].append("two")

  emitted = drain(setup.events)[0]
  assert emitted.data["changes"]["children"] == ["one"]


def test_stopped_producer_does_not_block_on_event_queue(setup):
  setup.stop.set()

  with pytest.raises(Cancelled):
    setup.producer().emit("message", text="unused")


def test_page_content_keeps_source_text_without_generated_filler(setup):
  setup.options.include_submissions = False
  producer = setup.producer()
  node = content_node(
    "<h2>Submission</h2>"
    "<p>Fetched: this is actual source text.</p>"
    "<h3>Documents</h3>"
    + attachment("submission.pdf")
    + '<img src="/lmsdata/submission.png" alt="Diagram">'
  )

  producer.content(
    OWNER,
    "Submission",
    PAGE,
    [node],
    INDEX_URL,
    role="submission",
    parent="course/one",
  )
  emitted = drain(setup.events)
  final = emitted[-1]

  assert setup.requests == []
  assert final.data["changes"]["documents"] == []
  assert "Submission" in final.data["body"]
  assert "Fetched: this is actual source text." in final.data["body"]
  assert "Documents" in final.data["body"]
  assert "submission.pdf" in final.data["body"]
  assert "Diagram" in final.data["body"]
  assert "not downloaded" not in final.data["body"]
  assert "Image unavailable" not in final.data["body"]
  assert final.data["changes"]["status"] == "archived"


def test_submission_inclusion_applies_to_files_and_inline_images(setup):
  producer = setup.producer()
  image = b"\x89PNG\r\n\x1a\nimage"
  image_url = CTL + "/lmsdata/submission.png"
  attachment_url = CTL + "/fileDownServlet?" + urlencode({
    "rFileName": "submission.pdf",
    "sFileName": "stored",
    "filePath": "folder",
  })
  expected = {
    image_url: ("submission.png", image),
    attachment_url: ("submission.pdf", PDF),
  }
  responses = [Response(image, mime="image/png"), Response()]
  setup.responses.extend(responses)
  node = content_node(
    "<p>Submission status: submitted</p>"
    + attachment("submission.pdf")
    + '<img src="/lmsdata/submission.png" alt="Diagram">'
  )

  producer.content(
    OWNER, "Submission", PAGE, [node], INDEX_URL, role="submission"
  )
  emitted = drain(setup.events)
  documents = [item for item in emitted if item.kind == "document"]
  final = emitted[-1]

  assert setup.requests == [(image_url, {}), (attachment_url, {})]
  assert len(documents) == 2
  assert {item.data["record"]["url"] for item in documents} == set(expected)

  for event in documents:
    record = event.data["record"]
    name, raw = expected[record["url"]]
    assert record["role"] == "submission"
    assert record["original"] == name
    assert record["sha256"] == digest(raw)
    assert setup.tree.read(event.data["work"] + "/new/" + name, 100) == raw

  assert final.data["changes"]["documents"] == [
    item.data["key"] for item in documents
  ]
  assert final.data["changes"]["status"] == "archived"
  assert final.data["changes"]["problems"] == []
  assert final.data["changes"]["empty_listing"] is False
  assert "Submission status: submitted" in final.data["body"]
  assert "Diagram" in final.data["body"]
  assert not setup.responses
  assert all(response.closed for response in responses)


def test_attachment_download_recipe_preserves_server_arguments(setup):
  setup.responses.append(Response())
  producer = setup.producer()

  producer.content(
    OWNER,
    "Page",
    PAGE,
    [content_node(attachment())],
    INDEX_URL,
  )

  fields = parse_qs(urlsplit(setup.requests[0][0]).query)
  assert fields == {
    "rFileName": ["notes.pdf"],
    "sFileName": ["stored"],
    "filePath": ["folder"],
  }


def test_disabled_file_capture_also_disables_inline_images(setup):
  producer = setup.producer()

  producer.content(
    OWNER,
    "Receipt",
    PAGE,
    [content_node(
      attachment() + '<img src="/lmsdata/image.png" alt="Image description">'
    )],
    INDEX_URL,
    include_files=False,
  )

  assert setup.requests == []
  assert drain(setup.events)[-1].data["changes"]["documents"] == []


def test_successful_page_refresh_replaces_old_attachment_references(setup):
  producer = setup.producer()
  assert producer.items[OWNER]["documents"] == ["previous-document"]

  producer.content(
    OWNER,
    "Page",
    PAGE,
    [content_node("<p>New content with no attachments.</p>")],
    INDEX_URL,
  )

  assert producer.items[OWNER]["documents"] == []
  assert drain(setup.events)[-1].data["changes"]["documents"] == []


def test_failed_page_refresh_does_not_clear_previous_references(setup):
  producer = setup.producer()

  producer.attempt(
    OWNER, "Page", producer.content,
    OWNER, "Page", PAGE, [], INDEX_URL,
  )

  assert producer.items[OWNER]["documents"] == ["previous-document"]
  assert producer.items[OWNER]["status"] == "failed"
  assert all(
    "documents" not in item.data["changes"]
    for item in drain(setup.events)
  )


def test_download_failure_is_a_page_limitation_not_generated_source_text(setup):
  producer = setup.producer()
  setup.responses.append(Response(b"", status=500))

  producer.content(
    OWNER,
    "Page",
    PAGE,
    [content_node("<p>Instructions</p>" + attachment())],
    INDEX_URL,
  )
  final = drain(setup.events)[-1]

  assert final.data["changes"]["status"] == "partial"
  assert "HTTP 500" in final.data["changes"]["problems"][0]
  assert "notes.pdf" in final.data["body"]
  assert "unavailable" not in final.data["body"]


def test_session_expiry_during_attachment_download_stops_the_page(setup):
  producer = setup.producer()
  setup.responses.append(Response(b"", status=302))

  with pytest.raises(Failure) as captured:
    producer.content(
      OWNER,
      "Page",
      PAGE,
      [content_node(attachment())],
      INDEX_URL,
    )

  assert captured.value.code == 1
  assert producer.items[OWNER]["documents"] == ["previous-document"]


def test_image_only_page_has_no_manufactured_transcript(setup):
  producer = setup.producer()
  setup.responses.append(Response(b"\x89PNG\r\n\x1a\nimage", mime="image/png"))

  producer.content(
    OWNER,
    "Image lesson",
    PAGE,
    [content_node('<img src="/lmsdata/image.png">')],
    INDEX_URL,
  )
  final = drain(setup.events)[-1]

  assert final.data["body"].strip() == ""
  assert len(final.data["changes"]["documents"]) == 1


def test_external_body_images_are_not_fetched(setup):
  producer = setup.producer()

  producer.content(
    OWNER,
    "Page",
    PAGE,
    [content_node('<img src="https://example.com/image.png" alt="Source caption">')],
    INDEX_URL,
  )
  final = drain(setup.events)[-1]

  assert setup.requests == []
  assert final.data["body"] == "Source caption"
  assert final.data["changes"]["status"] == "partial"


def test_page_markdown_removes_controls_without_deleting_source_prose():
  problems = []
  fragment = soup_html(
    "<h2>Documents</h2>"
    "<p>Fetched: source statement</p>"
    "<script>secret_script()</script>"
    "<style>.unused { color: red }</style>"
    '<div class="btnBox">Source panel text</div>'
    '<input value="hidden control">'
    '<a href="javascript:ignored()">Visible label</a>'
    '<a href="https://example.com/reference">Reference</a>'
  )
  body = page_markdown(fragment, problems)

  assert "Documents" in body
  assert "Fetched: source statement" in body
  assert "Source panel text" in body
  assert "Visible label" in body
  assert "https://example.com/reference" in body
  assert "secret_script" not in body
  assert "unused" not in body
  assert "hidden control" not in body
  assert "javascript:" not in body
  assert problems == []


def test_cleaning_removes_known_navigation_before_markdown_conversion():
  fragment = soup_html(
    "<h2>Documents</h2>"
    '<div class="btnBox"><a href="/Course.do?cmd=viewBoardContentsList">'
    "Board navigation</a></div>"
    '<div class="btnBox">Required instructions</div>'
  )
  before = str(fragment)
  cleaned = clean_content(fragment)
  problems = []

  assert str(fragment) == before
  assert "Board navigation" not in cleaned.get_text()
  body = page_markdown(cleaned, problems)

  assert "Documents" in body
  assert "Required instructions" in body
  assert "Board navigation" not in body
  assert problems == []


def test_spanning_table_keeps_readable_source_rows():
  fragment = soup_html(
    "<table>"
    '<tr><th colspan="2">Submission information</th></tr>'
    "<tr><td>Status</td><td>No submission recorded</td></tr>"
    "</table>"
  )
  body = page_markdown(fragment, [])

  assert "Submission information" in body
  assert "Status | No submission recorded" in body


def test_embedded_content_description_survives_without_execution():
  problems = []
  fragment = soup_html(
    '<iframe src="https://example.com/" title="Interactive diagram"></iframe>'
  )
  body = page_markdown(fragment, problems)

  assert body == "Interactive diagram"
  assert problems == ["embedded CTL content is not archived"]


@pytest.fixture
def traversal(setup, monkeypatch):
  module = ModuleType("kctl.courses")
  state = SimpleNamespace(calls=[], error=None, policies=[])

  class CourseTraversal:
    def __init__(self, producer):
      self.producer = producer

    def run(self, course_id, title):
      producer = self.producer
      key = "course/" + course_id
      state.calls.append((course_id, title, producer.reader.course))
      state.policies.append(copy.deepcopy(producer.items[key]))

      if state.error is not None:
        raise state.error

      producer.index(
        key,
        title,
        producer.items[key]["folder"],
        [],
        parent="index",
      )

  module.CourseTraversal = CourseTraversal
  monkeypatch.setitem(sys.modules, "kctl.courses", module)
  monkeypatch.setattr(kctl, "courses", module, raising=False)
  setup.page = soup_html(
    '<div id="PORTLET_0002">Dashboard one</div>'
    '<div id="PORTLET_0003">Dashboard two</div>'
    '<div id="PORTLET_0005">Dashboard three</div>'
  )
  return state


def test_run_reuses_home_and_records_policy_before_course_traversal(
  setup, traversal
):
  setup.options.include_submissions = False
  producer = setup.producer()
  producer.run()
  emitted = drain(setup.events)

  assert traversal.calls == [("one", "Course", "one")]
  assert traversal.policies[0]["course_id"] == "one"
  assert traversal.policies[0]["include_submissions"] is False
  assert traversal.policies[0]["status"] == "partial"
  assert len(setup.page_calls) == 1
  assert "Study.do" in setup.page_calls[0][0]
  assert producer.reader.course == ""
  assert emitted[-1].kind == "done"
  assert emitted[-1].data["status"] == "complete"
  assert emitted[-1].data["code"] == 0


def test_selected_course_run_preserves_unselected_course_metadata(setup, traversal):
  setup.home.courses["two"] = "Other"
  setup.options.course = ["one"]
  setup.options.include_submissions = False
  original = {
    "title": "Other",
    "path": "documents/Other/index.md",
    "folder": "documents/Other",
    "status": "archived",
    "course_id": "two",
    "include_submissions": True,
    "children": [],
  }
  setup.snapshot["items"]["course/two"] = copy.deepcopy(original)
  producer = setup.producer()

  producer.run()

  assert producer.items["course/two"] == original
  assert traversal.calls == [("one", "Course", "one")]
  assert "course/two" in producer.items["index"]["children"]


def test_ordinary_course_failure_is_recorded_without_stopping_other_work(
  setup, traversal
):
  traversal.error = Failure("unsupported course layout")
  producer = setup.producer()
  producer.run()
  emitted = drain(setup.events)

  assert producer.items["course/one"]["status"] == "failed"
  assert producer.items["course/one"]["include_submissions"] is True
  assert emitted[-1].data["status"] == "complete"


@pytest.mark.parametrize(
  "error",
  [StorageFailure("disk"), ProtectionFailure("unsafe")],
)
def test_local_course_failure_is_fatal(setup, traversal, error):
  traversal.error = error
  setup.producer().run()
  completion = drain(setup.events)[-1]

  assert completion.kind == "done"
  assert completion.data["status"] == "failed"
  assert completion.data["fatal_local"] is True
  assert completion.data["code"] == 2


def test_session_expiry_during_course_traversal_reports_auth_failure(
  setup, traversal
):
  traversal.error = Failure("session expired", 1)
  setup.producer().run()
  completion = drain(setup.events)[-1]

  assert completion.data["status"] == "failed"
  assert completion.data["code"] == 1
  assert completion.data["fatal_local"] is False


def test_dashboard_failure_keeps_course_traversal_running(setup, traversal):
  setup.page = Failure("dashboard layout changed")
  producer = setup.producer()
  producer.run()

  assert producer.items["dashboard"]["status"] == "failed"
  assert traversal.calls == [("one", "Course", "one")]


def test_cancelled_producer_does_not_fabricate_success(setup, traversal):
  setup.stop.set()
  producer = setup.producer()
  producer.run()

  assert drain(setup.events) == []
  assert traversal.calls == []


@pytest.mark.parametrize(
  ("name", "raw", "mime"),
  [
    ("reading.txt", b"Required reading body\n", "text/plain"),
    ("lesson.html", b"<p>Required reading body</p>", "text/html"),
  ],
)
def test_generic_download_label_does_not_replace_source_filename(
  setup, name, raw, mime
):
  setup.responses.append(Response(raw, mime=mime))
  producer = setup.producer()
  markup = (
    f'<button onclick="fileDownload(\'{name}\', \'stored\', \'folder\')">'
    "Download reading</button>"
  )

  producer.content(
    OWNER, "Page", PAGE, [content_node(markup)], INDEX_URL
  )
  events = drain(setup.events)
  document, = [event for event in events if event.kind == "document"]
  record = document.data["record"]

  assert record["name"] == name
  assert record["original"] == name
  assert setup.tree.read(document.data["work"] + "/new/" + name, 4096) == raw
  assert "Download reading" in events[-1].data["body"]
  assert events[-1].data["changes"]["status"] == "archived"
  assert events[-1].data["changes"]["problems"] == []


def test_download_filename_decoding_does_not_change_server_arguments(setup):
  raw = b"Source\n"
  setup.responses.append(Response(raw, mime="text/plain"))
  markup = (
    '<a onclick="fileDownload(\'reading*20notes.txt\', \'stored\', \'folder\')">'
    "Download</a>"
  )

  setup.producer().content(
    OWNER, "Page", PAGE, [content_node(markup)], INDEX_URL
  )
  events = drain(setup.events)
  document, = [event for event in events if event.kind == "document"]

  assert document.data["record"]["name"] == "reading notes.txt"
  assert document.data["record"]["original"] == "reading notes.txt"
  assert parse_qs(urlsplit(setup.requests[0][0]).query)["rFileName"] == [
    "reading*20notes.txt",
  ]


def test_ragged_table_limit_is_checked_before_cell_conversion(monkeypatch):
  import markdownify

  fragment = soup_html(
    "<table><tr>" + "<td>x</td>" * 8 + "</tr>"
    + "<tr><td>y</td></tr>" * 8 + "</table>"
  )
  monkeypatch.setattr(retrieval, "TEXT_LIMIT", 256)

  def unexpected(*args, **kwargs):
    pytest.fail("oversized table reached Markdown conversion")

  monkeypatch.setattr(markdownify, "markdownify", unexpected)

  with pytest.raises(Failure, match="text limit"):
    page_markdown(fragment, [])


def test_spanning_table_does_not_pay_for_unused_rectangular_padding(monkeypatch):
  fragment = soup_html(
    '<table><tr><td colspan="2">x</td>' + "<td>x</td>" * 7 + "</tr>"
    + "<tr><td>y</td></tr>" * 8 + "</table>"
  )
  monkeypatch.setattr(retrieval, "TEXT_LIMIT", 256)

  body = page_markdown(fragment, [])

  assert body.startswith("```")
  assert body.count("x") == 8
  assert body.count("y") == 8
  assert len(body.encode()) <= 256


def test_table_expansion_uses_remaining_page_allowance(monkeypatch):
  import markdownify

  table = "<table><tr>" + "<td>x</td>" * 4 + "</tr></table>"
  fragment = soup_html(table * 2)
  original = markdownify.markdownify
  calls = []

  def convert(raw, **kwargs):
    calls.append(raw)
    return original(raw, **kwargs)

  monkeypatch.setattr(retrieval, "TEXT_LIMIT", 100)
  monkeypatch.setattr(markdownify, "markdownify", convert)

  with pytest.raises(Failure, match="text limit"):
    page_markdown(fragment, [])

  assert calls == ["x"] * 4


def process_download(archive, key):
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
  return archive.data["documents"][key]["result"]


@pytest.mark.parametrize("cache", ["local", "revalidated", "downloaded"])
@pytest.mark.parametrize(
  ("name", "legacy", "raw", "mime", "kind"),
  [
    ("reading.txt", "Download", b"Required reading body\n", "text/plain", ".txt"),
    (
      "lesson.html",
      "Download",
      b"<p>Required reading body</p>",
      "text/html",
      ".html",
    ),
    (
      "lesson.html",
      "old.html",
      b"<p>Required reading body</p>",
      "text/html",
      ".html",
    ),
  ],
)
def test_corrected_source_name_updates_discovery_without_renaming_original(
  private_dir, monkeypatch, cache, name, legacy, raw, mime, kind
):
  archive = Archive(private_dir / "published", create=True)
  course = "course/one"
  url = CTL + "/fileDownServlet?" + urlencode({
    "rFileName": name,
    "sFileName": "stored",
    "filePath": "folder",
  })
  key = identity(OWNER, url)
  requests = []

  @contextmanager
  def open_response(target, headers=None):
    requests.append((target, headers))
    assert cache != "local"
    assert target == url
    response = Response(
      b"" if cache == "revalidated" else raw,
      status=304 if cache == "revalidated" else 200,
      mime=mime,
    )
    yield response

  def unexpected(*args, **kwargs):
    pytest.fail("name-only correction regenerated a verified product")

  try:
    archive.prepare()
    archive.item("index", {
      "title": "Archive",
      "path": "documents/index.md",
      "folder": "documents",
      "status": "archived",
      "children": [course],
      "parent": None,
    }, None)
    archive.item(course, {
      "title": "Course",
      "path": FOLDER + "/index.md",
      "folder": FOLDER,
      "status": "archived",
      "course_id": "one",
      "include_submissions": True,
      "children": [OWNER],
      "parent": "index",
    }, None)
    archive.item(OWNER, {
      "title": "Page",
      "path": PAGE,
      "folder": FOLDER,
      "status": "archived",
      "parent": course,
      "documents": [],
    }, "Instructions\n")
    work = archive.new_work()
    archive.mkdir(work + "/new")
    archive.write(work + "/new/" + legacy, raw)
    archive.document(key, {
      "owner": OWNER,
      "url": url,
      "name": legacy,
      "folder": FOLDER + "/Document",
      "original": legacy,
      "role": "attachment",
      "bytes": len(raw),
      "sha256": digest(raw),
      "signature": signature(archive.path(work + "/new/" + legacy)),
      "result": None,
      "attempts": {},
    }, work)
    archive.item(OWNER, {"documents": [key]}, None)
    previous = copy.deepcopy(process_download(archive, key))
    document = archive.data["documents"][key]
    assert archive.reusable(document, ["convert", "ocr"])

    if legacy == "Download":
      assert previous["nodes"][""]["kind"] == "unsupported"
    else:
      assert previous["nodes"][""]["products"]["convert"]["status"] == "complete"

    if cache != "local":
      document["etag"] = '"previous"'
    before = copy.deepcopy(document)
    events = queue.Queue()
    producer = Producer(
      reader=SimpleNamespace(http=SimpleNamespace(
        open=open_response,
        chunks=lambda response, limit, seconds: [response.body.read()],
      )),
      root=archive.root,
      snapshot=copy.deepcopy(archive.data),
      options=SimpleNamespace(
        refresh=False,
        include_submissions=True,
        course=None,
      ),
      events=events,
      stop=threading.Event(),
      home=None,
    )
    markup = (
      f'<button onclick="fileDownload(\'{name}\', \'stored\', \'folder\')">'
      "Download reading</button>"
    )
    producer.content(
      OWNER, "Page", PAGE, [content_node(markup)], INDEX_URL, parent=course
    )

    for event in drain(events):
      data = event.data
      if event.kind == "document":
        assert data["work"] is None
        assert data["key"] == key
        archive.document(key, data["record"], data["work"])
      else:
        assert event.kind == "item"
        archive.item(data["key"], data["changes"], data["body"])

    document = archive.data["documents"][key]
    assert document["name"] == name
    assert document["cache"] == cache
    assert document["result"] == previous
    assert all(
      document[field] == before[field]
      for field in ("folder", "original", "sha256", "bytes", "role", "url")
    )
    assert requests == (
      [] if cache == "local" else [(url, {"If-None-Match": '"previous"'})]
    )
    assert not archive.reusable(document, ["convert", "ocr"])

    with monkeypatch.context() as patch:
      if legacy == "old.html":
        patch.setattr(Engines, "markdown", unexpected)
        patch.setattr(Engines, "recognize", unexpected)
      result = process_download(archive, key)

    node = result["nodes"][""]
    assert node["name"] == name
    assert node["kind"] == kind
    if legacy == "old.html":
      old_product = previous["nodes"][""]["products"]["convert"]
      product = node["products"]["convert"]
      assert product["content_sha256"] == old_product["content_sha256"]
      assert product["finished"] == old_product["finished"]

    document = archive.data["documents"][key]
    assert archive.read(document["folder"] + "/" + legacy, 4096) == raw
    assert archive.data["items"][OWNER]["documents"] == [key]
    assert archive.reusable(document, ["convert", "ocr"])
    assert export_courses(archive, [course]) == 0
    text = archive.read("extracted/Course.txt", 16384).decode()
    assert "Required reading body" in text
    assert "Download reading" in text
    assert "unsupported file type" not in text
    archive.validate()
  finally:
    archive.close()
