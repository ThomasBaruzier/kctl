import codecs
import errno
import hashlib
import json

from datetime import datetime
from urllib.parse import unquote

import pytest

from kctl.core import (
  ASSET_LIMIT,
  CONTENT_LIMIT,
  CONVERT_KINDS,
  CTL,
  DOCUMENT_RESERVED,
  HASH,
  REPORT_LIMIT,
  TEXT_KINDS,
  TOKEN,
  BudgetReached,
  Cancelled,
  Failure,
  LimitReached,
  bounded_text,
  bundle_name,
  cancelled_exception,
  decode_text,
  digest,
  display,
  explanation,
  fenced,
  identity,
  join_path,
  json_bytes,
  label,
  link_to,
  named_kind,
  now,
  numbered,
  original_name,
  parent_path,
  parse_json,
  public_path,
  publication_path,
  reader_url,
  relative_parts,
  require,
  retryable,
  safe_filename,
  safe_name,
  terminal,
  text_language,
  token,
  valid_header,
  valid_id,
)


def test_require_preserves_message_and_exit_code():
  require(True, "unused")

  with pytest.raises(Failure) as captured:
    require(False, "authentication required", 1)

  assert captured.value.message == "authentication required"
  assert captured.value.code == 1
  assert str(captured.value) == "authentication required"


def test_hashes_and_identity_are_stable():
  raw = b"example"
  assert digest(raw) == hashlib.sha256(raw).hexdigest()
  assert HASH.fullmatch(digest(raw))

  values = ("course", "자료")
  expected = hashlib.sha256(
    json.dumps(values, ensure_ascii=False).encode()
  ).hexdigest()

  assert identity(*values) == expected
  assert identity("a", "bc") != identity("ab", "c")


def test_tokens_and_timestamps_have_expected_shape():
  assert TOKEN.fullmatch(token())
  timestamp = datetime.fromisoformat(now())
  assert timestamp.utcoffset().total_seconds() == 0
  assert timestamp.microsecond == 0


def test_budgets_leave_room_for_reports():
  assert CONTENT_LIMIT > 0
  assert CONTENT_LIMIT + REPORT_LIMIT == ASSET_LIMIT


def test_terminal_output_removes_control_characters():
  value = "\x1b[31mhello\nworld\t\x7f"
  rendered = terminal(value)

  assert "\x1b" not in rendered
  assert "\n" not in rendered
  assert "\t" not in rendered
  assert "\x7f" not in rendered
  assert "hello" in rendered
  assert "world" in rendered


def test_display_replaces_surrogates_and_bounds_output():
  assert display("a\ud800b") == "a?b"
  assert display("abcdef", 3) == "abc"


def test_markdown_label_escapes_without_preserving_line_breaks():
  assert label(" a\n[b]\t*c* ") == r"a \[b\] \*c\*"


@pytest.mark.parametrize(
  "error",
  [KeyboardInterrupt(), EOFError(), Cancelled()],
)
def test_cancellation_explanations(error):
  assert cancelled_exception(error)
  assert explanation(error) == "interrupted"


def test_explanations_do_not_echo_unexpected_exception_messages():
  assert explanation(ValueError("private contents")) == "unexpected ValueError"
  assert explanation(Failure("known failure")) == "known failure"


def test_os_error_explanation_does_not_include_full_path():
  error = OSError(
    errno.ENOENT, "private error message", "/private/account/cookies.json"
  )
  message = explanation(error)

  assert message.startswith("cookies.json:")
  assert "/private" not in message
  assert "private error message" not in message


def test_retry_policy_distinguishes_fixed_and_remaining_limits():
  assert not retryable(LimitReached("fixed limit"))
  assert retryable(BudgetReached("remaining budget"))
  assert retryable(ValueError())


@pytest.mark.parametrize("limit", [1, 2, 3, 8, 17, 120])
def test_safe_names_obey_utf8_byte_limits(limit):
  result = safe_name("한글" * 100, limit)

  assert result
  assert len(result.encode()) <= limit
  assert not result.startswith(".")
  assert result == result.strip(" .-")


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_filename_limits_fail(limit):
  with pytest.raises(Failure):
    safe_name("name", limit)


@pytest.mark.parametrize(
  ("source", "expected"),
  [
    ("../../notes.PDF", "notes.pdf"),
    (r"C:\temp\notes.PDF", "notes.pdf"),
    ("   ", "untitled"),
    ("a/b/c.txt", "c.txt"),
  ],
)
def test_safe_filenames_discard_source_directories(source, expected):
  assert safe_filename(source) == expected


@pytest.mark.parametrize("name", sorted(DOCUMENT_RESERVED))
def test_original_names_cannot_replace_generated_outputs(name):
  result = original_name(name)

  assert result.casefold() not in DOCUMENT_RESERVED
  assert "/" not in result
  assert "\\" not in result


def test_reserved_filename_check_is_case_insensitive():
  assert original_name("TEXT.MD") == "TEXT (2).md"


@pytest.mark.parametrize(
  ("source", "expected"),
  [
    ("a/archive.tar.gz", "archive"),
    (r"a\archive.tar.zstd", "archive"),
    ("archive.tar.lz4", "archive"),
    ("notes.PDF", "notes"),
  ],
)
def test_bundle_names_strip_complete_archive_suffixes(source, expected):
  assert bundle_name(source) == expected


def test_numbered_names_preserve_file_extensions():
  assert numbered("notes.pdf", 1, file=True) == "notes.pdf"
  assert numbered("notes.pdf", 2, file=True) == "notes (2).pdf"
  assert numbered("Course", 2) == "Course (2)"


def test_relative_path_helpers():
  assert join_path("", "index.md") == "index.md"
  assert join_path("documents", "index.md") == "documents/index.md"
  assert parent_path("index.md") == ""
  assert parent_path("documents/Course/index.md") == "documents/Course"
  assert relative_parts(".ctl/work/job") == (".ctl", "work", "job")

  link = link_to(
    "documents/Course/자료/a b.pdf",
    "documents/Course/index.md",
  )
  assert unquote(link) == "자료/a b.pdf"
  assert " " not in link


@pytest.mark.parametrize(
  "value",
  [
    "", "/absolute", "a//b", "a/", "./a", "a/../b",
    r"a\b", "a/\x00b", "a/\nb", "a/\x7fb",
  ],
)
def test_unsafe_managed_paths_are_rejected(value):
  with pytest.raises(Failure):
    relative_parts(value)


def test_public_and_publication_paths_have_different_scopes():
  public_path("contents/Example/source.py")
  publication_path("documents/Course/index.md")

  with pytest.raises(Failure):
    public_path(".ctl/manifest.json")

  with pytest.raises(Failure):
    publication_path("contents/Example/source.py")

  with pytest.raises(Failure):
    publication_path("extracted/Course.txt")

  with pytest.raises(Failure):
    publication_path("documents")


def test_text_validators():
  assert bounded_text("", 0)
  assert bounded_text("한글", 2)
  assert not bounded_text("a\nb", 3)
  assert not bounded_text("long", 3)

  assert valid_id("course-123")
  assert not valid_id("")
  assert not valid_id(" course-123")
  assert not valid_id("course\n123")
  assert not valid_id("x" * 129)

  assert valid_header('"etag-value"')
  assert valid_header("café")
  assert not valid_header("한글")
  assert not valid_header("value\r\nInjected: yes")


def test_json_roundtrip_preserves_unicode():
  value = {"course": "자료", "members": [1, True, None]}
  raw = json_bytes(value)

  assert "자료".encode() in raw
  assert parse_json(raw) == value


@pytest.mark.parametrize(
  "raw",
  [
    b'{"x": 1, "x": 2}',
    b'{"outer": {"x": 1, "x": 2}}',
    b'{"x": NaN}',
    b'{"x": Infinity}',
    b'{"x": -Infinity}',
    b"{",
    b"\xff",
  ],
)
def test_invalid_json_is_rejected(raw):
  with pytest.raises(Failure):
    parse_json(raw)


def test_json_output_rejects_nonfinite_values():
  with pytest.raises(ValueError):
    json_bytes({"x": float("nan")})


def test_reader_url_strips_path_sessions_and_fragments():
  result = reader_url(
    "/Main.do;jsessionid=abc123?cmd=viewHome#section"
  )
  assert result == CTL + "/Main.do?cmd=viewHome"


def test_reader_url_preserves_encoded_attachment_names():
  assert reader_url("/lmsdata/a%20b.png") == CTL + "/lmsdata/a%20b.png"


@pytest.mark.parametrize(
  "url",
  [
    "http://ctl.kmu.ac.kr/Main.do",
    "https://example.com/Main.do",
    "//example.com/Main.do",
    "https://user@ctl.kmu.ac.kr/Main.do",
    "https://ctl.kmu.ac.kr:443/Main.do",
    "/lmsdata/%2e%2e/private",
    "/lmsdata/a%5cb",
    "/lmsdata/a%00b",
    "/lmsdata/a%0ab",
    "/Main.do\n",
    "https://[invalid",
  ],
)
def test_reader_url_rejects_unapproved_origins_and_paths(url):
  with pytest.raises(Failure):
    reader_url(url)


@pytest.mark.parametrize(
  ("name", "kind"),
  [
    ("notes.HTM", ".html"),
    ("notes.html", ".html"),
    ("slides.PPTX", ".pptx"),
    ("src/main.py", ".py"),
    (r"src\main.cpp", ".cpp"),
    ("project/Makefile", "text"),
    ("project/.gitignore", "text"),
    ("project/.env.example", "text"),
    ("README.md", ".md"),
    ("bundle.zip", "zip"),
    ("bundle.tar.gz", "tar"),
    ("bundle.tgz", "tar"),
    ("bundle.tar.zstd", "tar-zstd"),
    ("bundle.tzst", "tar-zstd"),
    ("bundle.tar.lz4", "tar-lz4"),
    ("unknown.bin", "unsupported"),
    ("notebook.ipynb", "unsupported"),
  ],
)
def test_named_format_classification(name, kind):
  assert named_kind(name) == kind


def test_text_and_conversion_categories_do_not_overlap():
  assert not TEXT_KINDS.intersection(CONVERT_KINDS)
  assert ".html" in CONVERT_KINDS
  assert ".md" in TEXT_KINDS


def test_text_languages_use_original_names_when_needed():
  assert text_language(".py") == "python"
  assert text_language(".md") is None
  assert text_language(".txt") == "text"
  assert text_language("text", "project/Makefile") == "makefile"
  assert text_language("text", r"project\Dockerfile") == "dockerfile"

  with pytest.raises(Failure):
    text_language(".html")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_text_decoding_and_newline_normalization(encoding):
  raw = "한글\r\nfirst\rsecond\n".encode(encoding)
  assert decode_text(raw) == "한글\nfirst\nsecond\n"


def test_big_endian_unicode_boms_are_supported():
  text = "자료"
  assert decode_text(
    codecs.BOM_UTF16_BE + text.encode("utf-16-be")
  ) == text
  assert decode_text(
    codecs.BOM_UTF32_BE + text.encode("utf-32-be")
  ) == text


@pytest.mark.parametrize("raw", [b"\xff", b"a\x00b"])
def test_invalid_passthrough_text_is_not_silently_replaced(raw):
  with pytest.raises(Failure):
    decode_text(raw)


def test_fences_preserve_source_and_outgrow_embedded_backticks():
  text = "  first\n```example\nlast"
  result = fenced(text, "python")

  assert result == "````python\n" + text + "\n````\n"


def test_fences_preserve_existing_trailing_newlines():
  assert fenced("value\n\n") == "```\nvalue\n\n```\n"


def test_code_fence_language_cannot_inject_lines():
  with pytest.raises(Failure):
    fenced("value", "python\ninjected")
