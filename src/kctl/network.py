from __future__ import annotations

import copy
import http.client
import http.cookiejar
import ssl
import threading
import time
import urllib.error
import urllib.request
import warnings

from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit

from .core import (
  COOKIE_NAME,
  CTL_HOST,
  DOWNLOAD_LIMIT,
  HASH,
  HTML_LIMIT,
  NETWORK_TIMEOUT,
  SSO_HOST,
  Cancelled,
  Failure,
  bounded_text,
  digest,
  require,
  valid_header,
)


COOKIE_FIELDS = frozenset({
  "version", "name", "value", "port", "port_specified", "domain",
  "domain_specified", "domain_initial_dot", "path", "path_specified",
  "secure", "expires", "discard", "comment", "comment_url", "rest",
  "rfc2109",
})


def cookie_record(cookie: http.cookiejar.Cookie) -> dict[str, Any]:
  return {
    name: dict(cookie._rest) if name == "rest" else getattr(cookie, name)
    for name in COOKIE_FIELDS
  }


def validate_cookie(record: Any) -> None:
  require(
    isinstance(record, dict) and set(record) == COOKIE_FIELDS,
    "malformed cookie state",
  )

  for name in (
    "port_specified", "domain_specified", "domain_initial_dot",
    "path_specified", "secure", "discard", "rfc2109",
  ):
    require(type(record[name]) is bool, "malformed cookie flags")

  require(
    type(record["version"]) is int
    and record["version"] in {0, 1}
    and isinstance(record["name"], str)
    and len(record["name"]) <= 256
    and COOKIE_NAME.fullmatch(record["name"]),
    "malformed cookie identity",
  )
  require(
    bounded_text(record["value"], 16384)
    and record["value"].isascii()
    and ";" not in record["value"],
    "malformed cookie value",
  )
  require(
    record["path"] == "/"
    and record["port"] is None
    and not record["port_specified"],
    "unsupported cookie scope",
  )

  domain = record["domain"]
  require(isinstance(domain, str), "malformed cookie domain")

  if record["domain_specified"]:
    require(
      record["name"] == "SSOTOKEN"
      and domain in {".kmu.ac.kr", "kmu.ac.kr"}
      and (not record["domain_initial_dot"] or domain.startswith(".")),
      "unsupported domain cookie",
    )
  else:
    require(
      domain in {CTL_HOST, SSO_HOST}
      and not record["domain_initial_dot"]
      and record["name"] != "SSOTOKEN",
      "unsupported host cookie",
    )

  expiry = record["expires"]
  require(
    expiry is None
    or type(expiry) is int and 0 <= expiry < 2**63,
    "malformed cookie expiry",
  )

  for name in ("comment", "comment_url"):
    value = record[name]
    require(
      value is None
      or bounded_text(value, 4096)
      and ";jsessionid=" not in value.lower(),
      "malformed cookie metadata",
    )

  require(
    isinstance(record["rest"], dict) and len(record["rest"]) <= 32,
    "malformed cookie attributes",
  )

  for name, value in record["rest"].items():
    require(
      isinstance(name, str)
      and len(name) <= 128
      and COOKIE_NAME.fullmatch(name)
      and (
        value is None
        or bounded_text(value, 4096)
        and ";jsessionid=" not in value.lower()
      ),
      "malformed cookie attribute",
    )


def is_ctl_cookie(cookie: http.cookiejar.Cookie) -> bool:
  return cookie.domain == CTL_HOST and not cookie.domain_specified


class CookiePolicy(http.cookiejar.DefaultCookiePolicy):
  def __init__(self, ctl_only: bool = False):
    super().__init__(
      strict_ns_domain=self.DomainStrictNonDomain,
      secure_protocols=("https",),
    )
    self.ctl_only = ctl_only

  def allowed(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
    parts = urlsplit(request.full_url)
    return (
      parts.scheme == "https"
      and parts.hostname in {CTL_HOST, SSO_HOST}
      and (not self.ctl_only or is_ctl_cookie(cookie))
      and (cookie.domain_specified or cookie.domain == parts.hostname)
    )

  def set_ok(self, cookie: Any, request: Any) -> bool:
    try:
      validate_cookie(cookie_record(cookie))
    except (Failure, TypeError, ValueError):
      return False

    return self.allowed(cookie, request) and super().set_ok(cookie, request)

  def return_ok(self, cookie: Any, request: Any) -> bool:
    return self.allowed(cookie, request) and super().return_ok(cookie, request)


def new_jar(ctl_only: bool = False) -> http.cookiejar.CookieJar:
  return http.cookiejar.CookieJar(CookiePolicy(ctl_only))


def ctl_jar(saved: http.cookiejar.CookieJar) -> http.cookiejar.CookieJar:
  jar = new_jar(ctl_only=True)

  for cookie in saved:
    if is_ctl_cookie(cookie) and not cookie.is_expired():
      jar.set_cookie(copy.deepcopy(cookie))

  return jar


def merge_ctl(
  saved: http.cookiejar.CookieJar,
  updated: http.cookiejar.CookieJar,
) -> http.cookiejar.CookieJar:
  jar = new_jar()

  for cookie in saved:
    if not is_ctl_cookie(cookie) and not cookie.is_expired():
      jar.set_cookie(copy.deepcopy(cookie))

  for cookie in updated:
    require(
      is_ctl_cookie(cookie),
      "unexpected SSO cookie in isolated reader",
    )
    if not cookie.is_expired():
      jar.set_cookie(copy.deepcopy(cookie))

  return jar


def has_cookie(
  jar: http.cookiejar.CookieJar,
  name: str,
  ctl_only: bool = False,
  usable: bool = True,
) -> bool:
  return any(
    cookie.name == name
    and (not ctl_only or is_ctl_cookie(cookie))
    and (not usable or bool(cookie.value) and not cookie.is_expired())
    for cookie in jar
  )


def tls_context() -> ssl.SSLContext:
  from .session import read_config

  values = read_config(
    "KCTL_INTERMEDIATE_PEM",
    "KCTL_INTERMEDIATE_SHA256",
  )
  pem = values["KCTL_INTERMEDIATE_PEM"].strip()
  fingerprint = values["KCTL_INTERMEDIATE_SHA256"].strip()
  values.clear()

  require(
    pem and HASH.fullmatch(fingerprint),
    "configure KCTL_INTERMEDIATE_PEM and KCTL_INTERMEDIATE_SHA256",
  )
  require(
    pem.startswith("-----BEGIN CERTIFICATE-----")
    and pem.endswith("-----END CERTIFICATE-----")
    and pem.count("-----BEGIN CERTIFICATE-----") == 1
    and pem.count("-----END CERTIFICATE-----") == 1,
    "KCTL_INTERMEDIATE_PEM must contain one PEM certificate",
  )

  try:
    der = ssl.PEM_cert_to_DER_cert(pem)
    require(
      digest(der) == fingerprint,
      "intermediate certificate fingerprint mismatch",
    )
    context = ssl.create_default_context()
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    context.load_verify_locations(cadata=pem)

    require(
      context.check_hostname
      and context.verify_mode == ssl.CERT_REQUIRED,
      "TLS verification unavailable",
    )
    return context

  except (ssl.SSLError, ValueError, OSError):
    raise Failure("TLS initialization failed") from None


class ReturnStatuses(urllib.request.HTTPErrorProcessor):
  def http_response(self, request: Any, response: Any) -> Any:
    return response

  https_response = http_response


@contextmanager
def cookie_warnings() -> Iterator[None]:
  with warnings.catch_warnings(record=True) as captured:
    warnings.simplefilter("always")
    yield
    require(not captured, "cookie processing warning")


def _target(url: str) -> str:
  require(
    isinstance(url, str)
    and 0 < len(url) <= 16384
    and "\\" not in url
    and all(32 < ord(character) < 127 for character in url),
    "invalid network URL",
  )

  try:
    parts = urlsplit(url)
  except ValueError:
    raise Failure("malformed network URL") from None

  require(
    parts.scheme == "https"
    and parts.netloc in {CTL_HOST, SSO_HOST}
    and not parts.fragment,
    "unapproved network origin",
  )
  return parts.netloc


def _headers(values: dict[str, str] | None) -> dict[str, str]:
  protected = {
    "host", "cookie", "cookie2", "authorization", "proxy-authorization",
    "content-length", "transfer-encoding", "accept-encoding",
  }
  result = {
    "User-Agent": "kctl/1",
    "Accept-Encoding": "identity",
  }

  for name, value in (values or {}).items():
    require(
      isinstance(name, str)
      and len(name) <= 128
      and COOKIE_NAME.fullmatch(name)
      and name.lower() not in protected
      and valid_header(value),
      "invalid outgoing header",
    )
    result[name] = value

  return result


def _probe_cookies(headers: Any) -> None:
  require(
    not headers.get_all("Set-Cookie2", []),
    "unsupported probe cookie headers",
  )
  values = headers.get_all("Set-Cookie", [])

  with cookie_warnings():
    parsed = http.cookiejar.parse_ns_headers(values)

  require(len(parsed) == len(values), "malformed probe cookies")

  for fields in parsed:
    require(
      fields
      and fields[0][0] != "SSOTOKEN"
      and "domain" not in {name.lower() for name, _ in fields[1:]},
      "CTL-only probe attempted to change SSO cookies",
    )


class HTTP:
  def __init__(
    self,
    jar: http.cookiejar.CookieJar,
    limit: int,
    stop: threading.Event | None = None,
  ):
    self.jar = jar
    self.limit = limit
    self.stop = stop
    self.requests = 0
    self.bytes = 0
    self.seconds = 0.0
    self.opener = urllib.request.build_opener(
      urllib.request.ProxyHandler({}),
      urllib.request.HTTPSHandler(context=tls_context()),
      ReturnStatuses(),
    )

  def check(self) -> None:
    if self.stop is not None and self.stop.is_set():
      raise Cancelled()

  @contextmanager
  def open(
    self,
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    probe: bool = False,
  ) -> Iterator[Any]:
    self.check()
    host = _target(url)
    require(self.requests < self.limit, "request limit reached")
    require(self.bytes < DOWNLOAD_LIMIT, "download limit reached")

    if probe:
      policy = getattr(self.jar, "_policy", None)
      require(
        host == CTL_HOST
        and isinstance(policy, CookiePolicy)
        and policy.ctl_only,
        "probe requires an isolated CTL cookie jar",
      )

    outgoing = _headers(headers)
    if data is not None:
      outgoing["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, data=data, headers=outgoing)
    self.requests += 1
    started = time.monotonic()

    try:
      with cookie_warnings():
        self.jar.add_cookie_header(request)

      with self.opener.open(request, timeout=NETWORK_TIMEOUT) as response:
        if probe:
          _probe_cookies(response.headers)

        with cookie_warnings():
          self.jar.extract_cookies(response, request)

        require(len(self.jar) <= 128, "too many upstream cookies")
        encodings = response.headers.get_all("Content-Encoding", [])
        require(
          len(encodings) <= 1
          and (not encodings or encodings[0].strip().lower() == "identity"),
          "unsupported response encoding",
        )

        self.check()
        yield response

    except urllib.error.URLError as error:
      if isinstance(error.reason, ssl.SSLCertVerificationError):
        raise Failure("TLS certificate verification failed") from None
      raise Failure("connection failed or timed out") from None

    except (ssl.SSLError, TimeoutError, http.client.HTTPException):
      raise Failure("upstream transport failed or timed out") from None

    finally:
      self.seconds += time.monotonic() - started
      request.data = None
      request.remove_header("Cookie")
      request.remove_header("Cookie2")

  def chunks(
    self, response: Any, limit: int, seconds: int
  ) -> Iterator[bytes]:
    require(type(limit) is int and limit >= 0, "invalid response limit")
    require(seconds > 0, "invalid response timeout")
    total = 0
    deadline = time.monotonic() + seconds

    while True:
      self.check()
      require(time.monotonic() < deadline, "response body timeout")

      try:
        chunk = response.read1(min(128 * 1024, limit + 1 - total))
      except (OSError, http.client.HTTPException):
        raise Failure("body transfer failed or timed out") from None

      self.check()
      require(time.monotonic() < deadline, "response body timeout")

      if not chunk:
        return

      total += len(chunk)
      self.bytes += len(chunk)
      require(
        total <= limit and self.bytes <= DOWNLOAD_LIMIT,
        "response exceeds byte limit",
      )
      yield chunk

  def read(self, response: Any) -> bytes:
    return b"".join(self.chunks(response, HTML_LIMIT, 120))
