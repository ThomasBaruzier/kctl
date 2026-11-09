from email.message import Message
from urllib.parse import urlencode

import pytest

from kctl.auth_html import (
  auth_document,
  approved_url,
  declared_literal,
  function_body,
  mask_script,
  navigation_script,
  soup_html,
  text_of,
)
from kctl.core import (
  CTL,
  HOME_URL,
  HTML_LIMIT,
  INDEX_URL,
  LOGIN_FORM_URL,
  LOGIN_POST_URL,
  SSO,
  Failure,
)


def headers(*values: str) -> Message:
  message = Message()
  for value in values:
    message["Content-Type"] = value
  return message


def document(body: str):
  raw = ("<!doctype html><html><body>" + body + "</body></html>").encode()
  return auth_document(headers("text/html; charset=utf-8"), raw)


def test_approved_navigation_returns_the_canonical_target():
  candidate = "/Main.do;jsessionid=abc-123?cmd=viewHome"

  assert approved_url(candidate, HOME_URL, HOME_URL) == HOME_URL
  assert approved_url(
    "login_process.do", LOGIN_FORM_URL, LOGIN_POST_URL
  ) == LOGIN_POST_URL


def test_approved_navigation_accepts_explicit_https_port():
  candidate = "https://ctl.kmu.ac.kr:443/Main.do?cmd=viewHome"
  assert approved_url(candidate, HOME_URL, HOME_URL) == HOME_URL


def test_authentication_query_values_are_compared_after_decoding():
  candidate = SSO + "/kmusso/ext/ctl/login_form.do?" + urlencode({
    "Return_Url": INDEX_URL,
  })
  assert approved_url(
    candidate, HOME_URL, LOGIN_FORM_URL
  ) == LOGIN_FORM_URL


def test_authentication_parameter_order_is_not_significant():
  expected = CTL + "/Main.do?cmd=viewHome&mode=read"
  candidate = CTL + "/Main.do?mode=read&cmd=viewHome"

  assert approved_url(candidate, HOME_URL, expected) == expected


@pytest.mark.parametrize(
  "candidate",
  [
    "",
    " " + HOME_URL,
    HOME_URL + " ",
    HOME_URL + "#section",
    HOME_URL + "&extra=1",
    HOME_URL + "&cmd=viewHome",
    CTL + "/Main.do?cmd=other",
    CTL + "/Main.do?cmd",
    CTL + "/Other.do?cmd=viewHome",
    CTL + "/%4dain.do?cmd=viewHome",
    "http://ctl.kmu.ac.kr/Main.do?cmd=viewHome",
    "https://example.com/Main.do?cmd=viewHome",
    "https://user@ctl.kmu.ac.kr/Main.do?cmd=viewHome",
    "https://ctl.kmu.ac.kr:444/Main.do?cmd=viewHome",
    "https://ctl.kmu.ac.kr:invalid/Main.do?cmd=viewHome",
    r"https://ctl.kmu.ac.kr\@example.com/Main.do?cmd=viewHome",
    "/Main.do?cmd=viewHome\n",
    "/Main.do?cmd=한글",
    "https://[invalid",
  ],
)
def test_unapproved_authentication_navigation_is_rejected(candidate):
  with pytest.raises(Failure):
    approved_url(candidate, HOME_URL, HOME_URL)


def test_authentication_session_path_has_a_length_limit():
  candidate = "/Main.do;jsessionid=" + "x" * 257 + "?cmd=viewHome"

  with pytest.raises(Failure):
    approved_url(candidate, HOME_URL, HOME_URL)


def test_authentication_document_preserves_declared_encoding():
  raw = '<form name="mainForm"><p>자료</p></form>'.encode("cp949")
  parsed = auth_document(headers("text/html; charset=cp949"), raw)

  assert parsed.encoding == "cp949"
  assert text_of(parsed.form("mainForm")) == "자료"


@pytest.mark.parametrize(
  "content_types",
  [
    (),
    ("text/plain; charset=utf-8",),
    ("text/html",),
    ("text/html; charset=",),
    ("text/html; charset=missing-codec",),
    ("text/html; charset=utf-8; charset=utf-8",),
    ("text/html; charset=utf-8", "text/html; charset=utf-8"),
  ],
)
def test_authentication_document_requires_unambiguous_html_encoding(
  content_types,
):
  with pytest.raises(Failure):
    auth_document(headers(*content_types), b"<html></html>")


@pytest.mark.parametrize(
  "raw",
  [
    b"<html>\x00</html>",
    b"<html>\xff</html>",
    b"<html>An error has occurred while processing</html>",
  ],
)
def test_invalid_authentication_document_bodies_are_rejected(raw):
  with pytest.raises(Failure):
    auth_document(headers("text/html; charset=utf-8"), raw)


def test_authentication_document_size_is_checked_before_parsing():
  with pytest.raises(Failure, match="size limit"):
    auth_document(
      headers("text/html; charset=utf-8"),
      b"x" * (HTML_LIMIT + 1),
    )


def test_named_form_is_selected_without_collecting_other_forms():
  parsed = document(
    '<form name="other"><input name="x"></form>'
    '<form name="mainForm" method="post">'
    '<input type="hidden" name="userType" value="learner">'
    "</form>"
  )
  form = parsed.form("mainForm")

  assert form["method"] == "post"
  assert form.find("input")["value"] == "learner"
  assert form.find("input", attrs={"name": "x"}) is None


@pytest.mark.parametrize(
  "body",
  [
    '<form name="mainForm" name="mainForm"></form>',
    '<form name="mainForm"><input name="x" name="x"></form>',
    '<form name="mainForm"><form name="nested"></form></form>',
    '<form name="mainForm">',
    '<form name="mainForm"/>',
    '<form id="mainForm"></form>',
    '<form name="mainForm"></form><form name="mainForm"></form>',
    '<form name="mainForm"></form><form name="other" id="mainForm"></form>',
    (
      '<form name="mainForm" id="main">'
      '<input name="x" form="main">'
      "</form>"
    ),
    (
      '<input name="x" form="main">'
      '<form name="mainForm" id="main"></form>'
    ),
    (
      '<form name="mainForm"></form>'
      '<button form="mainForm">Submit</button>'
    ),
  ],
)
def test_ambiguous_or_associated_forms_are_rejected(body):
  with pytest.raises(Failure):
    document(body).form("mainForm")


def test_missing_form_is_rejected():
  with pytest.raises(Failure, match="missing or ambiguous"):
    document("<p>No form</p>").form("mainForm")


def test_unrelated_form_association_does_not_invalidate_selected_form():
  parsed = document(
    '<form name="mainForm"></form>'
    '<form name="other" id="other"></form>'
    '<input form="other" name="value">'
  )

  assert parsed.form("mainForm")["name"] == "mainForm"


def test_text_helper_collapses_layout_whitespace():
  assert text_of(soup_html("<p>a <strong>b</strong>\n c</p>")) == "a b c"
  assert text_of(None) == ""


def test_script_masking_preserves_offsets_and_ignores_comments():
  source = (
    "var returnUrl = 'https://ctl.kmu.ac.kr/index.jsp';\n"
    "// returnUrl = 'wrong';\n"
    "/* var returnUrl = 'also wrong'; */\n"
  )
  code, literals = mask_script(source)

  assert len(code) == len(source)
  assert "wrong" not in code
  assert declared_literal(
    code, literals, "returnUrl"
  ) == INDEX_URL

  literal_offset = source.index("'")
  assert code[literal_offset] == "@"
  assert literals[literal_offset] == INDEX_URL


def test_navigation_words_inside_strings_are_not_declarations():
  source = (
    """var message = "var returnUrl = 'wrong';"; """
    "var returnUrl = 'correct';"
  )
  code, literals = mask_script(source)

  assert declared_literal(code, literals, "returnUrl") == "correct"


@pytest.mark.parametrize(
  "source",
  [
    "/* unfinished",
    "'unfinished",
    '"unfinished',
    "'trailing\\",
    "'line\nbreak'",
  ],
)
def test_unterminated_or_multiline_navigation_strings_are_rejected(source):
  with pytest.raises(Failure):
    mask_script(source)


@pytest.mark.parametrize(
  "source",
  [
    r"var returnUrl = 'escaped\nvalue';",
    "var returnUrl = `template`;",
    "var returnUrl = `multiple\nlines`;",
    "var returnUrl = 'first' + 'second';",
    "var returnUrl = dynamicValue;",
    "var returnUrl = 'value'",
    "let returnUrl = 'value';",
    "returnUrl = 'value';",
    "var returnUrl = 'first'; var returnUrl = 'second';",
    "var returnUrl = 'first'; returnUrl = 'second';",
    "var returnUrl = 'first'; returnUrl += 'second';",
    "var returnUrl = 'first'; returnUrl ||= 'second';",
    "var returnUrl = 'first'; returnUrl++;",
    "var returnUrl = 'first'; ++returnUrl;",
    "var returnUrl = 'first'; --returnUrl;",
  ],
)
def test_navigation_literals_require_one_plain_var_assignment(source):
  code, literals = mask_script(source)

  with pytest.raises(Failure):
    declared_literal(code, literals, "returnUrl")


def test_comparisons_and_unrelated_properties_are_not_variable_writes():
  source = (
    "var returnUrl = 'value';"
    "if (returnUrl == 'value') { object.returnUrl = 'other'; }"
  )
  code, literals = mask_script(source)

  assert declared_literal(code, literals, "returnUrl") == "value"


def test_navigation_scripts_preserve_offsets_across_script_elements():
  page = soup_html(
    "<script>var unrelated = 'ignore';</script>"
    "<script>var message = '';</script>"
    f"<script>var returnUrl = '{INDEX_URL}';</script>"
  )
  code, literals = navigation_script(page, ("message", "returnUrl"))

  assert "unrelated" not in code
  assert declared_literal(code, literals, "message") == ""
  assert declared_literal(code, literals, "returnUrl") == INDEX_URL


@pytest.mark.parametrize(
  "html",
  [
    '<script src="/remote.js">var returnUrl = "value";</script>',
    '<script type="application/json">var returnUrl = "value";</script>',
    "<script>var unrelated = 'value';</script>",
    "<p>returnUrl</p>",
  ],
)
def test_unsupported_or_missing_navigation_scripts_are_rejected(html):
  with pytest.raises(Failure):
    navigation_script(soup_html(html), ("returnUrl",))


@pytest.mark.parametrize(
  "script_type",
  ["", "text/javascript", "application/javascript"],
)
def test_supported_inline_navigation_scripts(script_type):
  page = soup_html(
    f'<script type="{script_type}">var returnUrl = "value";</script>'
  )
  code, literals = navigation_script(page, ("returnUrl",))

  assert declared_literal(code, literals, "returnUrl") == "value"


def test_function_body_handles_nested_braces_after_string_masking():
  source = (
    "function jf_loginCheck() {"
    "if (enabled) { var value = '}'; }"
    "document.form1.submit();"
    "}"
  )
  code, _ = mask_script(source)
  body, offset = function_body(code, "jf_loginCheck")

  assert body.startswith("if (enabled)")
  assert body.endswith("document.form1.submit();")
  assert code[offset:offset + len(body)] == body


@pytest.mark.parametrize(
  "source",
  [
    "function other() {}",
    "function jf_loginCheck(value) {}",
    "function jf_loginCheck() {",
    "function jf_loginCheck() {} function jf_loginCheck() {}",
  ],
)
def test_missing_ambiguous_or_incomplete_functions_are_rejected(source):
  code, _ = mask_script(source)

  with pytest.raises(Failure):
    function_body(code, "jf_loginCheck")


def test_masked_function_body_can_be_matched_without_executing_script():
  source = (
    f"var returnUrl = '{INDEX_URL}';"
    "function jf_loginCheck() {"
    "document.form1.action = returnUrl;"
    "document.form1.submit();"
    "}"
  )
  code, literals = mask_script(source)
  body, _ = function_body(code, "jf_loginCheck")

  assert declared_literal(code, literals, "returnUrl") == INDEX_URL
  assert body == (
    "document.form1.action = returnUrl;"
    "document.form1.submit();"
  )
