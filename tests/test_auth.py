import getpass
import http.cookiejar
import warnings

from collections import deque
from contextlib import contextmanager
from email.message import Message
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

import kctl.auth as auth

from kctl.core import (
  CTL,
  CTL_HOST,
  CTL_LOGOUT_URL,
  HOME_URL,
  INDEX_URL,
  LOGIN_FORM_URL,
  LOGIN_POST_URL,
  LOGOUT_BRIDGE_URL,
  SSO_LOGOUT_URL,
  Failure,
)
from kctl.network import has_cookie, new_jar


def page(body):
  return "<!doctype html><html><body>" + body + "</body></html>"


def home(role="learner", account="student-1"):
  body = (
    '<form name="mainForm" method="post" action="/Main.do">'
    f'<input type="hidden" name="userType" value="{role}">'
    "</form>"
  )

  if role == "guest":
    body += (
      f'<form name="login" action="{LOGIN_FORM_URL}">'
      f'<input type="hidden" name="Return_Url" value="{INDEX_URL}">'
      "</form>"
    )
  elif account is not None:
    body += (
      '<div id="header"><div class="login"><fieldset><p>'
      f"<span>Student ({account})</span>"
      "</p></fieldset></div></div>"
    )

  return page(body)


def password_form(extra="", charset="utf-8"):
  return page(
    f'<form name="idLoginForm" method="post" accept-charset="{charset}" '
    f'action="{LOGIN_POST_URL}">'
    '<input type="text" name="loginName">'
    '<input type="password" name="password">'
    f'<input type="hidden" name="Return_Url" value="{INDEX_URL}">'
    '<input type="hidden" name="csrf" value="context">'
    + extra
    + '<button type="submit">Sign in</button></form>'
  )


def handoff(target=INDEX_URL, controls="", body=None):
  if body is None:
    body = (
      "document.form1.action = returnUrl;"
      "document.form1.submit();"
    )

  return page(
    f"<script>var returnUrl = '{target}';"
    f"function jf_loginCheck() {{{body}}}</script>"
    f'<form name="form1" method="post">{controls}</form>'
  )


def logout_bridge(target=SSO_LOGOUT_URL):
  return page(f"<script>top.location.href = '{target}';</script>")


def logout_completion(prefix="", guard=True, message=""):
  alert = "if (message != '') { alert(message); }" if guard else ""

  return page(
    f"<script>var returnUrl = '{INDEX_URL}';"
    f"var message = '{message}';"
    "function jf_logoutCheck() {"
    + prefix
    + alert
    + "logout(); document.location.href = returnUrl;"
    "}</script>"
  )


def cookie(name="JSESSIONID", domain=CTL_HOST):
  return http.cookiejar.Cookie(
    version=0,
    name=name,
    value="test-token",
    port=None,
    port_specified=False,
    domain=domain,
    domain_specified=domain.startswith("."),
    domain_initial_dot=domain.startswith("."),
    path="/",
    path_specified=True,
    secure=True,
    expires=None,
    discard=True,
    comment=None,
    comment_url=None,
    rest={},
    rfc2109=False,
  )


def issue_sso(jar):
  jar.set_cookie(cookie("SSOTOKEN", ".kmu.ac.kr"))


def remove_sso(jar):
  jar.clear(".kmu.ac.kr", "/", "SSOTOKEN")


@pytest.fixture
def upstream(monkeypatch):
  state = SimpleNamespace(
    responses=deque(),
    requests=[],
    closed=0,
    terminal_calls=0,
  )

  def enqueue(
    body,
    status=200,
    headers=(),
    change=None,
    charset="utf-8",
  ):
    message = Message()
    message["Content-Type"] = "text/html; charset=" + charset
    for name, value in headers:
      message[name] = value

    raw = body.encode(charset) if isinstance(body, str) else body
    state.responses.append(SimpleNamespace(
      status=status,
      headers=message,
      raw=raw,
      change=change,
    ))

  class HTTP:
    def __init__(self, jar, limit, stop=None):
      assert limit == 12
      self.jar = jar

    @contextmanager
    def open(self, url, data=None, headers=None, probe=False):
      state.requests.append((url, data, probe))
      assert state.responses, "unexpected authentication request"
      response = state.responses.popleft()

      try:
        if response.change is not None:
          response.change(self.jar)
        yield response
      finally:
        state.closed += 1

    def read(self, response):
      return response.raw

  def terminal():
    state.terminal_calls += 1

  state.enqueue = enqueue
  monkeypatch.setattr(auth, "HTTP", HTTP)
  monkeypatch.setattr(auth, "interactive_terminal", terminal)
  return state


def queue_login(upstream, account="student-1", form=None, result=None):
  upstream.enqueue(form if form is not None else password_form())
  upstream.enqueue(
    result if result is not None else handoff(),
    change=issue_sso,
  )
  upstream.enqueue(page("Index"))
  upstream.enqueue(home(account=account))


def queue_logout(upstream, ctl_session=True, completion=None, clear=True):
  if ctl_session:
    upstream.enqueue(
      "",
      status=302,
      headers=[("Location", "/ssoLogout.jsp")],
    )

  upstream.enqueue(logout_bridge())
  upstream.enqueue(
    completion if completion is not None else logout_completion(),
    change=remove_sso if clear else None,
  )
  upstream.enqueue(page("Index"))
  upstream.enqueue(home("guest"))


def test_home_returns_verified_role_account_and_page(upstream):
  upstream.enqueue(home())
  client = auth.Auth(new_jar(ctl_only=True))

  role, account, verified = client.home(probe=True)

  assert (role, account) == ("learner", "student-1")
  assert verified.select_one('input[name="userType"]')["value"] == "learner"
  assert "Student (student-1)" in verified.get_text()
  assert upstream.requests == [(HOME_URL, None, True)]
  assert upstream.closed == 1


def test_guest_home_requires_known_login_navigation(upstream):
  upstream.enqueue(home("guest"))

  role, account, verified = auth.Auth(new_jar()).home()

  assert (role, account) == ("guest", None)
  assert verified.select_one('form[name="login"]') is not None
  assert len(upstream.requests) == 1


@pytest.mark.parametrize(
  "body",
  [
    home(account=None),
    home(role="administrator"),
    home().replace('name="userType"', 'name="other"'),
    home().replace('type="hidden"', 'type="text"', 1),
    home().replace('name="userType"', 'disabled name="userType"'),
    home().replace('action="/Main.do"', 'action="/Other.do"'),
    home().replace('method="post"', 'method="get"'),
    home().replace(
      "</p>",
      "<span>Other (student-2)</span></p>",
    ),
  ],
)
def test_unverified_homepages_fail(upstream, body):
  upstream.enqueue(body)

  with pytest.raises(Failure):
    auth.Auth(new_jar()).home()


def test_repeated_identical_account_labels_are_not_ambiguous(upstream):
  body = home().replace(
    "</p>", "<span>Student (student-1)</span></p>"
  )
  upstream.enqueue(body)

  role, account, verified = auth.Auth(new_jar()).home()

  assert (role, account) == ("learner", "student-1")
  assert len(verified.select("#header span")) == 2


@pytest.mark.parametrize(
  "body",
  [
    home("guest").replace(LOGIN_FORM_URL, "https://example.com/login"),
    home("guest").replace(
      f'value="{INDEX_URL}"', 'value="https://example.com/"'
    ),
    home("guest").replace(
      '<form name="login"', '<form name="login" id="external"'
    ).replace(
      "</body>", '<input form="external" name="x"></body>'
    ),
  ],
)
def test_guest_login_form_must_match_approved_structure(upstream, body):
  upstream.enqueue(body)

  with pytest.raises(Failure):
    auth.Auth(new_jar()).home()


@pytest.mark.parametrize(
  ("url", "data"),
  [
    (CTL + "/Other.do", None),
    (LOGIN_POST_URL, None),
    (LOGIN_FORM_URL, b"password=test"),
    (HOME_URL, b"unexpected"),
    ("https://example.com/", None),
  ],
)
def test_fetch_allowlist_rejects_unapproved_requests(upstream, url, data):
  with pytest.raises(Failure, match="unapproved authentication request"):
    auth.Auth(new_jar()).fetch(url, data)

  assert upstream.requests == []


def test_unexpected_status_is_not_followed(upstream):
  upstream.enqueue("", status=302, headers=[("Location", LOGIN_FORM_URL)])

  with pytest.raises(Failure, match="HTTP 302"):
    auth.Auth(new_jar()).home()

  assert len(upstream.requests) == 1
  assert upstream.closed == 1


def test_configured_password_uses_verified_form_and_returns_final_home(upstream):
  queue_login(upstream)
  jar = new_jar()

  verified = auth.Auth(jar).password_login("student-1", password=" p&ss+word ")

  assert upstream.terminal_calls == 0
  assert [request[:1] for request in upstream.requests] == [
    (LOGIN_FORM_URL,),
    (LOGIN_POST_URL,),
    (INDEX_URL,),
    (HOME_URL,),
  ]
  fields = parse_qs(
    upstream.requests[1][1].decode(), keep_blank_values=True
  )
  assert fields == {
    "csrf": ["context"],
    "Return_Url": [INDEX_URL],
    "loginName": ["student-1"],
    "password": [" p&ss+word "],
  }
  assert upstream.requests[2][1] == b""
  assert has_cookie(jar, "SSOTOKEN")
  assert verified.select_one('form[name="mainForm"]') is not None
  assert verified.select_one('form[name="idLoginForm"], form[name="form1"]') is None
  assert "Student (student-1)" in verified.get_text()
  assert upstream.closed == 4


def test_returned_homepage_keeps_enrollment_navigation(upstream):
  markup = home().replace(
    "</body>",
    '<a onclick="viewMyCourse(\'course-1\')">Course One</a></body>',
  )
  upstream.enqueue(markup)

  _, _, verified = auth.Auth(new_jar()).home()

  anchor = verified.find("a", attrs={"onclick": "viewMyCourse('course-1')"})
  assert anchor.get_text() == "Course One"
  assert upstream.requests == [(HOME_URL, None, False)]


def test_duplicate_identical_return_controls_are_canonicalized(upstream):
  form = password_form(
    f'<input type="hidden" name="Return_Url" value="{INDEX_URL}">'
  )
  queue_login(upstream, form=form)

  auth.Auth(new_jar()).password_login("student-1", password="test")

  fields = parse_qs(upstream.requests[1][1].decode())
  assert fields["Return_Url"] == [INDEX_URL]


def test_disabled_noncredential_controls_are_not_submitted(upstream):
  form = password_form(
    '<input name="ignored" disabled value="x">'
    '<fieldset disabled><input name="alsoIgnored" value="x"></fieldset>'
  )
  queue_login(upstream, form=form)

  auth.Auth(new_jar()).password_login("student-1", password="test")

  fields = parse_qs(upstream.requests[1][1].decode())
  assert "ignored" not in fields
  assert "alsoIgnored" not in fields


def test_interactive_password_is_prompted_after_form_validation(
  upstream, monkeypatch
):
  queue_login(upstream)
  calls = []

  def password(prompt, stream):
    calls.append((prompt, len(upstream.requests), stream))
    return "test-password"

  monkeypatch.setattr(auth.getpass, "getpass", password)

  verified = auth.Auth(new_jar()).password_login("student-1")

  assert upstream.terminal_calls == 1
  assert calls[0][0] == "password: "
  assert calls[0][1] == 1
  assert calls[0][2] is auth.sys.stderr
  assert verified.select_one('input[name="userType"]')["value"] == "learner"


def test_getpass_warning_prevents_credential_submission(upstream, monkeypatch):
  upstream.enqueue(password_form())

  def password(*args, **kwargs):
    warnings.warn("echoing input", getpass.GetPassWarning)
    return "must-not-submit"

  monkeypatch.setattr(auth.getpass, "getpass", password)

  with pytest.raises(getpass.GetPassWarning):
    auth.Auth(new_jar()).password_login("student-1")

  assert len(upstream.requests) == 1


@pytest.mark.parametrize(
  "form",
  [
    password_form().replace('method="post"', 'method="get"'),
    password_form().replace(LOGIN_POST_URL, "https://example.com/login"),
    password_form().replace(
      'method="post"', 'method="post" enctype="multipart/form-data"'
    ),
    password_form().replace('type="password"', 'type="text"'),
    password_form().replace(
      'name="password"', 'disabled name="password"'
    ),
    password_form().replace(
      'name="loginName"', 'name="missingLoginName"'
    ),
    password_form('<input name="password" type="password">'),
    password_form('<textarea name="unexpected">value</textarea>'),
    password_form('<input name="plain" type="hidden" value="x">'),
    password_form('<input name="signedMsg" type="hidden" value="x">'),
    password_form('<input name="vidMsg" type="hidden" value="x">'),
    password_form(
      f'<input name="Return_Url" type="hidden" value="{CTL}/other">'
    ),
    password_form(charset="utf-8 ascii"),
    password_form(charset="utf-16"),
  ],
)
def test_invalid_password_forms_fail_before_prompt_or_post(upstream, form):
  upstream.enqueue(form)

  with pytest.raises(Failure):
    auth.Auth(new_jar()).password_login("student-1")

  assert upstream.terminal_calls == 0
  assert len(upstream.requests) == 1


def test_disabled_credential_fieldset_is_rejected(upstream):
  form = password_form().replace(
    '<input type="password" name="password">',
    '<fieldset disabled><input type="password" name="password"></fieldset>',
  )
  upstream.enqueue(form)

  with pytest.raises(Failure, match="credential"):
    auth.Auth(new_jar()).password_login("student-1", password="test")

  assert len(upstream.requests) == 1


def test_excessive_hidden_fields_are_rejected(upstream):
  extra = "".join(
    f'<input type="hidden" name="field{index}" value="x">'
    for index in range(65)
  )
  upstream.enqueue(password_form(extra))

  with pytest.raises(Failure, match="too many password form fields"):
    auth.Auth(new_jar()).password_login("student-1", password="test")


def test_unencodable_credentials_fail_without_echoing_password(upstream):
  upstream.enqueue(password_form(charset="ascii"))

  with pytest.raises(Failure) as captured:
    auth.Auth(new_jar()).password_login("student-1", password="비밀번호")

  assert captured.value.code == 1
  assert "비밀번호" not in captured.value.message
  assert len(upstream.requests) == 1


def test_non_utf8_form_charset_is_used_for_credentials(upstream):
  queue_login(upstream, form=password_form(charset="cp949"))

  auth.Auth(new_jar()).password_login("student-1", password="암호")

  fields = parse_qs(
    upstream.requests[1][1].decode(), encoding="cp949", errors="strict"
  )
  assert fields["password"] == ["암호"]


def test_empty_password_is_not_submitted(upstream):
  upstream.enqueue(password_form())

  with pytest.raises(Failure, match="empty password"):
    auth.Auth(new_jar()).password_login("student-1", password="")

  assert len(upstream.requests) == 1


def test_invalid_student_id_does_not_start_a_request(upstream):
  with pytest.raises(Failure, match="invalid student id"):
    auth.Auth(new_jar()).password_login(" student-1", password="test")

  assert upstream.requests == []


@pytest.mark.parametrize(
  "result",
  [
    handoff(target="https://example.com/"),
    handoff(controls='<input name="extra" value="x">'),
    handoff(body="document.location.href = returnUrl;"),
    handoff().replace('method="post"', 'method="get"'),
    handoff().replace(
      '<form name="form1"', '<form name="form1" action="/Other.do"'
    ),
  ],
)
def test_unrecognized_login_handoff_stops_before_ctl_submission(
  upstream, result
):
  queue_login(upstream, result=result)

  with pytest.raises(Failure):
    auth.Auth(new_jar()).password_login("student-1", password="test")

  assert len(upstream.requests) == 2


def test_login_requires_usable_sso_token(upstream):
  upstream.enqueue(password_form())
  upstream.enqueue(handoff())

  with pytest.raises(Failure, match="login not verified"):
    auth.Auth(new_jar()).password_login("student-1", password="test")

  assert len(upstream.requests) == 2


def test_login_verifies_requested_account_before_returning_homepage(upstream):
  queue_login(upstream, account="another-student")

  with pytest.raises(Failure) as captured:
    auth.Auth(new_jar()).password_login("student-1", password="test")

  assert captured.value.code == 1
  assert "does not match" in captured.value.message
  assert len(upstream.requests) == 4


def test_guest_after_login_is_not_success(upstream):
  upstream.enqueue(password_form())
  upstream.enqueue(handoff(), change=issue_sso)
  upstream.enqueue(page("Index"))
  upstream.enqueue(home("guest"))

  with pytest.raises(Failure, match="login not verified"):
    auth.Auth(new_jar()).password_login("student-1", password="test")


def test_logout_validates_every_navigation_and_final_guest_state(upstream):
  jar = new_jar()
  jar.set_cookie(cookie())
  issue_sso(jar)
  queue_logout(upstream)

  auth.Auth(jar).logout()

  assert [request[0] for request in upstream.requests] == [
    CTL_LOGOUT_URL,
    LOGOUT_BRIDGE_URL,
    SSO_LOGOUT_URL,
    INDEX_URL,
    HOME_URL,
  ]
  assert not has_cookie(jar, "SSOTOKEN", usable=False)


def test_logout_without_ctl_session_uses_bridge_directly(upstream):
  jar = new_jar()
  issue_sso(jar)
  queue_logout(upstream, ctl_session=False)

  auth.Auth(jar).logout()

  assert upstream.requests[0][0] == LOGOUT_BRIDGE_URL


def test_logout_accepts_the_verified_browser_close_branch(upstream):
  branch = (
    "if (returnUrl == 'close') {"
    "if (navigator.userAgent.indexOf('MSIE') > 0"
    " || navigator.userAgent.indexOf('Trident') > 0) {"
    "top.open('about:blank', '_self').close();"
    "} else { window.opener = top; top.close(); }"
    "}"
  )
  jar = new_jar()
  issue_sso(jar)
  queue_logout(
    upstream,
    ctl_session=False,
    completion=logout_completion(prefix=branch),
  )

  auth.Auth(jar).logout()
  assert not has_cookie(jar, "SSOTOKEN", usable=False)


def test_logout_can_omit_the_empty_message_guard(upstream):
  jar = new_jar()
  issue_sso(jar)
  queue_logout(
    upstream,
    ctl_session=False,
    completion=logout_completion(guard=False),
  )

  auth.Auth(jar).logout()


@pytest.mark.parametrize(
  "headers",
  [
    [],
    [("Location", "https://example.com/")],
    [("Location", "/ssoLogout.jsp"), ("Location", "/ssoLogout.jsp")],
  ],
)
def test_logout_redirect_must_be_unambiguous_and_approved(upstream, headers):
  jar = new_jar()
  jar.set_cookie(cookie())
  upstream.enqueue("", status=302, headers=headers)

  with pytest.raises(Failure):
    auth.Auth(jar).logout()

  assert len(upstream.requests) == 1


def test_logout_bridge_cannot_redirect_elsewhere(upstream):
  upstream.enqueue(logout_bridge("https://example.com/"))

  with pytest.raises(Failure, match="authentication target"):
    auth.Auth(new_jar()).logout()

  assert len(upstream.requests) == 1


def test_logout_bridge_cannot_have_competing_navigation(upstream):
  body = logout_bridge().replace(
    "</script>", "top.location.href = 'https://example.com/';</script>"
  )
  upstream.enqueue(body)

  with pytest.raises(Failure, match="ambiguous logout navigation"):
    auth.Auth(new_jar()).logout()


def test_logout_rejects_nonempty_central_error_message(upstream):
  jar = new_jar()
  issue_sso(jar)
  queue_logout(
    upstream,
    ctl_session=False,
    completion=logout_completion(message="failed"),
  )

  with pytest.raises(Failure, match="central logout result"):
    auth.Auth(jar).logout()

  assert len(upstream.requests) == 2


def test_logout_requires_actual_sso_token_removal(upstream):
  jar = new_jar()
  issue_sso(jar)
  queue_logout(upstream, ctl_session=False, clear=False)

  with pytest.raises(Failure, match="SSO token not removed"):
    auth.Auth(jar).logout()

  assert len(upstream.requests) == 2


def test_logout_requires_guest_state_at_end(upstream):
  jar = new_jar()
  issue_sso(jar)
  upstream.enqueue(logout_bridge())
  upstream.enqueue(logout_completion(), change=remove_sso)
  upstream.enqueue(page("Index"))
  upstream.enqueue(home())

  with pytest.raises(Failure, match="guest status not confirmed"):
    auth.Auth(jar).logout()
