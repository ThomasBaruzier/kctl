from __future__ import annotations

import codecs
import getpass
import os
import re
import sys
import warnings

from typing import Any
from urllib.parse import urlencode

from .auth_html import (
  AuthDocument,
  approved_url,
  auth_document,
  declared_literal,
  function_body,
  navigation_script,
  text_of,
)
from .core import (
  CTL,
  CTL_LOGOUT_URL,
  HOME_URL,
  HTML_LIMIT,
  INDEX_URL,
  LOGIN_FORM_URL,
  LOGIN_POST_URL,
  LOGOUT_BRIDGE_URL,
  SSO_LOGOUT_URL,
  Failure,
  require,
  valid_id,
)
from .network import HTTP, has_cookie


AUTH_ROUTES = frozenset({
  ("GET", HOME_URL),
  ("GET", LOGIN_FORM_URL),
  ("POST", LOGIN_POST_URL),
  ("GET", INDEX_URL),
  ("POST", INDEX_URL),
  ("GET", CTL_LOGOUT_URL),
  ("GET", LOGOUT_BRIDGE_URL),
  ("GET", SSO_LOGOUT_URL),
})


def interactive_terminal() -> None:
  import termios

  require(
    sys.stdin.isatty() and sys.stderr.isatty(),
    "interactive terminal required",
  )
  descriptor = os.open(
    "/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC
  )

  try:
    termios.tcgetattr(descriptor)
  finally:
    os.close(descriptor)


def _enabled(control: Any) -> bool:
  return (
    not control.has_attr("disabled")
    and not control.has_attr("form")
    and control.find_parent("fieldset", attrs={"disabled": True}) is None
  )


def _credential(form: Any, name: str, kind: str) -> None:
  fields = form.find_all(attrs={"name": name})
  require(
    len(fields) == 1
    and fields[0].name == "input"
    and str(fields[0].get("type", "text")).lower() == kind
    and _enabled(fields[0]),
    "missing or conflicting credential fields",
  )


def _encoding(document: AuthDocument, form: Any) -> str:
  charset = form.get("accept-charset")
  parts = (
    charset if isinstance(charset, list)
    else str(charset).replace(",", " ").split() if charset
    else [document.encoding]
  )
  require(len(parts) == 1, "ambiguous password form charset")

  try:
    encoding = codecs.lookup(parts[0]).name
    require(
      "Return_Url".encode(encoding) == b"Return_Url",
      "unsupported password charset",
    )
  except (LookupError, UnicodeError, ValueError, TypeError):
    raise Failure("unsupported password charset") from None

  return encoding


def _password_form(
  document: AuthDocument,
) -> tuple[str, list[tuple[str, str]]]:
  form = document.form("idLoginForm")
  require(
    str(form.get("method", "")).lower() == "post",
    "unexpected password form method",
  )
  approved_url(form.get("action", ""), LOGIN_FORM_URL, LOGIN_POST_URL)
  require(
    str(form.get("enctype", "application/x-www-form-urlencoded")).lower()
    == "application/x-www-form-urlencoded",
    "unsupported password form encoding",
  )
  _credential(form, "loginName", "text")
  _credential(form, "password", "password")

  returns = form.find_all(attrs={"name": "Return_Url"})
  require(returns, "password form lacks return target")

  for control in returns:
    require(
      control.name == "input"
      and str(control.get("type", "")).lower() == "hidden"
      and control.get("value") == INDEX_URL
      and _enabled(control),
      "unexpected password return field",
    )

  hidden = []
  for control in form.find_all(["input", "select", "textarea", "button"]):
    name = control.get("name")
    if name in {"Return_Url", "loginName", "password"}:
      continue
    if control.has_attr("disabled"):
      continue
    if control.find_parent("fieldset", attrs={"disabled": True}) is not None:
      continue

    default = "submit" if control.name == "button" else "text"
    kind = str(control.get("type", default)).lower()

    if control.name != "input" or kind != "hidden":
      require(
        control.name in {"input", "button"}
        and kind in {"submit", "button", "reset"},
        "unexpected password form control",
      )
      continue

    value = control.get("value", "")
    require(
      isinstance(name, str)
      and 0 < len(name) <= 256
      and name.isprintable()
      and name not in {"plain", "signedMsg", "vidMsg"}
      and _enabled(control)
      and isinstance(value, str)
      and len(value) <= 8192
      and "\x00" not in value,
      "unsupported password form context",
    )
    hidden.append((name, value))

  require(len(hidden) <= 64, "too many password form fields")
  return _encoding(document, form), hidden


def _guest_login(document: AuthDocument) -> None:
  matches = []

  for index, form in enumerate(document.soup.find_all("form")):
    try:
      approved_url(form.get("action", ""), HOME_URL, LOGIN_FORM_URL)
    except Failure:
      continue
    matches.append((index, form))

  require(len(matches) == 1, "missing or ambiguous guest login form")
  index, form = matches[0]
  document.inventory.validate(index)
  returns = form.find_all(attrs={"name": "Return_Url"})
  require(
    len(returns) == 1
    and returns[0].name == "input"
    and returns[0].get("value") == INDEX_URL
    and _enabled(returns[0]),
    "unexpected guest return target",
  )


def _account(document: AuthDocument) -> str:
  accounts = []

  for span in document.soup.select("#header .login fieldset p span"):
    match = re.search(r"\(([^()\s]+)\)\s*$", text_of(span))
    if match:
      accounts.append(match[1])

  require(
    len(set(accounts)) == 1 and valid_id(accounts[0]),
    "authenticated account could not be verified",
  )
  return accounts[0]


def _login_handoff(document: AuthDocument, jar: Any) -> None:
  code, literals = navigation_script(
    document.soup, ("returnUrl", "jf_loginCheck")
  )
  require(
    declared_literal(code, literals, "returnUrl") == INDEX_URL,
    "unexpected login response",
  )

  form = document.form("form1")
  require(
    str(form.get("method", "")).lower() == "post"
    and form.get("action", "") in {"", "#"}
    and form.find(
      ["input", "select", "textarea", "button", "object", "form"]
    ) is None
    and not form.get_text(strip=True),
    "unexpected login handoff form",
  )

  body, _ = function_body(code, "jf_loginCheck")
  require(
    re.fullmatch(
      r"\s*document\s*\.\s*form1\s*\.\s*action\s*=\s*returnUrl\s*;"
      r"\s*document\s*\.\s*form1\s*\.\s*submit\s*\(\s*\)\s*;\s*",
      body,
    ) is not None
    and has_cookie(jar, "SSOTOKEN"),
    "login not verified",
    1,
  )


def _logout_bridge(document: AuthDocument) -> None:
  require(
    not document.soup.find_all("form"),
    "unexpected logout bridge form",
  )
  code, literals = navigation_script(document.soup, ("top", "location"))
  writes = list(re.finditer(
    r"(?<![\w$.])top\s*\.\s*location\s*\.\s*href\s*=(?!=)",
    code,
  ))
  matches = list(re.finditer(
    r"(?:^|[;{}])\s*top\s*\.\s*location\s*\.\s*href"
    r"\s*=\s*(?P<literal>@)\s*;",
    code,
  ))
  require(
    len(writes) == len(matches) == 1,
    "ambiguous logout navigation",
  )
  approved_url(
    literals.get(matches[0].start("literal")),
    LOGOUT_BRIDGE_URL,
    SSO_LOGOUT_URL,
  )


def _logout_completion(document: AuthDocument) -> None:
  code, literals = navigation_script(
    document.soup, ("returnUrl", "message", "jf_logoutCheck")
  )
  require(
    declared_literal(code, literals, "returnUrl") == INDEX_URL
    and declared_literal(code, literals, "message") == "",
    "unexpected central logout result",
  )
  body, start = function_body(code, "jf_logoutCheck")
  branch = re.match(
    r"\s*if\s*\(\s*returnUrl\s*==\s*@\s*\)\s*\{"
    r"\s*if\s*\(\s*navigator\.userAgent\.indexOf\(@\s*\) > 0"
    r"\s*\|\|\s*navigator\.userAgent\.indexOf\(@\s*\) > 0\s*\)"
    r"\s*\{\s*top\.open\(@\s*,\s*@\s*\)\.close\(\);"
    r"\s*\}\s*else\s*\{\s*window\.opener = top;"
    r"\s*top\.close\(\);\s*\}\s*\}",
    body,
  )

  if branch:
    values = [
      literals.get(start + match.start())
      for match in re.finditer("@", branch.group())
    ]
    require(
      values == ["close", "MSIE", "Trident", "about:blank", "_self"],
      "unexpected logout close branch",
    )
    body = body[branch.end():]
    start += branch.end()

  guard = (
    r"(?:if\s*\(\s*message\s*!=\s*@\s*\)\s*\{"
    r"\s*alert\s*\(\s*message\s*\)\s*;\s*\}\s*)?"
  )
  require(
    re.fullmatch(
      r"\s*" + guard + r"logout\s*\(\s*\)\s*;"
      r"\s*document\s*\.\s*location\s*\.\s*href\s*=\s*returnUrl\s*;\s*",
      body,
    ) is not None,
    "unrecognized central logout completion",
  )

  if "@" in body:
    require(
      literals.get(start + body.index("@")) == "",
      "unexpected logout comparison",
    )


class Auth:
  def __init__(self, jar: Any):
    self.jar = jar
    self.http = HTTP(jar, 12)

  def fetch(
    self,
    url: str,
    data: bytes | None = None,
    status: int = 200,
    probe: bool = False,
  ) -> tuple[Any, bytes]:
    require(
      ("POST" if data is not None else "GET", url) in AUTH_ROUTES,
      "unapproved authentication request",
    )
    approved_url(url, url, url)

    with self.http.open(url, data, probe=probe) as response:
      require(
        response.status == status,
        f"authentication returned HTTP {response.status}",
      )
      return response.headers, self.http.read(response)

  def home(self, probe: bool = False) -> tuple[str, str | None, Any]:
    document = auth_document(*self.fetch(HOME_URL, probe=probe))
    form = document.form("mainForm")
    require(
      str(form.get("method", "")).lower() == "post",
      "unexpected homepage form method",
    )
    approved_url(form.get("action", ""), HOME_URL, f"{CTL}/Main.do")

    fields = form.find_all(attrs={"name": "userType"})
    require(
      len(fields) == 1
      and fields[0].name == "input"
      and str(fields[0].get("type", "")).lower() == "hidden"
      and _enabled(fields[0]),
      "unrecognized homepage role marker",
    )
    role = fields[0].get("value")
    require(role in {"learner", "guest"}, "unexpected homepage role")

    if role == "guest":
      _guest_login(document)
      return role, None, document.soup

    return role, _account(document), document.soup

  def password_login(
    self, student_id: str, password: str | None = None
  ) -> Any:
    require(valid_id(student_id), "invalid student id")
    document = auth_document(*self.fetch(LOGIN_FORM_URL))
    encoding, hidden = _password_form(document)

    if password is None:
      interactive_terminal()
      with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("password: ", stream=sys.stderr)

    fields = []
    payload = None

    try:
      require(isinstance(password, str) and password, "empty password")
      fields = hidden + [
        ("Return_Url", INDEX_URL),
        ("loginName", student_id),
        ("password", password),
      ]
      try:
        payload = urlencode(
          fields, encoding=encoding, errors="strict"
        ).encode("ascii")
      except (UnicodeError, ValueError):
        raise Failure(
          "credentials cannot use approved charset", 1
        ) from None

      require(len(payload) <= HTML_LIMIT, "password submission too large")

    finally:
      fields.clear()
      password = None

    try:
      document = auth_document(*self.fetch(LOGIN_POST_URL, payload))
    finally:
      payload = None

    _login_handoff(document, self.jar)
    auth_document(*self.fetch(INDEX_URL, b""))
    role, account, page = self.home()
    require(role == "learner", "login not verified", 1)
    require(
      account == student_id,
      "authenticated account does not match requested student id",
      1,
    )
    return page

  def logout(self) -> None:
    if has_cookie(self.jar, "JSESSIONID", ctl_only=True):
      headers, _ = self.fetch(CTL_LOGOUT_URL, status=302)
      locations = headers.get_all("Location", [])
      require(len(locations) == 1, "unexpected logout redirect")
      approved_url(locations[0], CTL_LOGOUT_URL, LOGOUT_BRIDGE_URL)

    _logout_bridge(auth_document(*self.fetch(LOGOUT_BRIDGE_URL)))
    _logout_completion(auth_document(*self.fetch(SSO_LOGOUT_URL)))
    require(
      not has_cookie(self.jar, "SSOTOKEN", usable=False),
      "SSO token not removed",
    )

    auth_document(*self.fetch(INDEX_URL))
    role, _, _ = self.home()
    require(
      role == "guest"
      and not has_cookie(self.jar, "SSOTOKEN", usable=False),
      "guest status not confirmed",
    )
