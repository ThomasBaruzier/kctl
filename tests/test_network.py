import http.cookiejar
import io
import ssl
import threading
import time
import urllib.error
import urllib.request
import warnings

from email.message import Message
from types import SimpleNamespace

import pytest

import kctl.network as network
import kctl.session as session

from kctl.core import (
  CTL,
  CTL_HOST,
  HOME_URL,
  SSO,
  SSO_HOST,
  Cancelled,
  Failure,
  digest,
)
from kctl.network import (
  CookiePolicy,
  HTTP,
  ReturnStatuses,
  cookie_record,
  ctl_jar,
  has_cookie,
  merge_ctl,
  new_jar,
  tls_context,
  validate_cookie,
)


def cookie(name="JSESSIONID", domain=CTL_HOST, expires=None):
  return http.cookiejar.Cookie(
    version=0,
    name=name,
    value="test-value",
    port=None,
    port_specified=False,
    domain=domain,
    domain_specified=domain.startswith("."),
    domain_initial_dot=domain.startswith("."),
    path="/",
    path_specified=True,
    secure=True,
    expires=expires,
    discard=expires is None,
    comment=None,
    comment_url=None,
    rest={"HttpOnly": None},
    rfc2109=False,
  )


class Response:
  def __init__(self, raw=b"body", status=200, headers=()):
    self.body = io.BytesIO(raw)
    self.status = status
    self.headers = Message()
    self.closed = False

    for name, value in headers:
      self.headers[name] = value

  def __enter__(self):
    return self

  def __exit__(self, *args):
    self.closed = True

  def info(self):
    return self.headers

  def read1(self, size):
    return self.body.read(size)


@pytest.fixture
def transport(monkeypatch):
  monkeypatch.setattr(network, "tls_context", lambda: None)

  def create(response=None, jar=None, limit=10, stop=None, error=None):
    response = response if response is not None else Response()
    jar = jar if jar is not None else new_jar()
    state = SimpleNamespace(
      response=response,
      jar=jar,
      error=error,
      calls=[],
      handlers=[],
      request=None,
    )

    class Opener:
      def open(self, request, timeout):
        state.request = request
        state.calls.append({
          "url": request.full_url,
          "data": request.data,
          "cookie": request.get_header("Cookie"),
          "timeout": timeout,
        })
        if state.error is not None:
          raise state.error
        return state.response

    def build(*handlers):
      state.handlers = handlers
      return Opener()

    monkeypatch.setattr(network.urllib.request, "build_opener", build)
    state.http = HTTP(jar, limit, stop)
    return state

  return create


@pytest.fixture
def certificate(monkeypatch):
  raw = b"synthetic DER for configuration tests"
  pem = ssl.DER_cert_to_PEM_cert(raw)
  values = {
    "KCTL_INTERMEDIATE_PEM": pem,
    "KCTL_INTERMEDIATE_SHA256": digest(raw),
  }
  contexts = []

  class Context:
    def __init__(self):
      self.check_hostname = True
      self.verify_mode = ssl.CERT_REQUIRED
      self.verify_flags = (
        ssl.VERIFY_X509_STRICT
        | getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
      )
      self.loaded = None

    def load_verify_locations(self, *, cadata):
      self.loaded = cadata

  def read_config(*names):
    return {name: values[name] for name in names}

  def create_context():
    context = Context()
    contexts.append(context)
    return context

  monkeypatch.setattr(session, "read_config", read_config)
  monkeypatch.setattr(network.ssl, "create_default_context", create_context)

  return SimpleNamespace(
    values=values,
    contexts=contexts,
    pem=pem,
    raw=raw,
  )


def test_tls_uses_configured_certificate_and_default_verification(certificate):
  context = tls_context()

  assert context is certificate.contexts[0]
  assert context.loaded == certificate.pem.strip()
  assert context.check_hostname
  assert context.verify_mode == ssl.CERT_REQUIRED
  assert not context.verify_flags & getattr(
    ssl, "VERIFY_X509_PARTIAL_CHAIN", 0
  )
  assert context.verify_flags & ssl.VERIFY_X509_STRICT


@pytest.mark.parametrize(
  ("name", "value"),
  [
    ("KCTL_INTERMEDIATE_PEM", ""),
    ("KCTL_INTERMEDIATE_SHA256", ""),
    ("KCTL_INTERMEDIATE_SHA256", "not-a-fingerprint"),
    ("KCTL_INTERMEDIATE_SHA256", "A" * 64),
  ],
)
def test_missing_or_invalid_tls_configuration_fails(certificate, name, value):
  certificate.values[name] = value

  with pytest.raises(Failure, match="configure"):
    tls_context()

  assert certificate.contexts == []


def test_fingerprint_is_checked_before_loading_certificate(certificate):
  certificate.values["KCTL_INTERMEDIATE_SHA256"] = "0" * 64

  with pytest.raises(Failure, match="fingerprint mismatch"):
    tls_context()

  assert certificate.contexts == []


def test_multiple_certificates_are_not_accepted(certificate):
  certificate.values["KCTL_INTERMEDIATE_PEM"] = certificate.pem * 2

  with pytest.raises(Failure, match="one PEM certificate"):
    tls_context()


def test_certificate_errors_do_not_echo_configuration(certificate, monkeypatch):
  def fail():
    raise ssl.SSLError("private certificate contents")

  monkeypatch.setattr(network.ssl, "create_default_context", fail)

  with pytest.raises(Failure) as captured:
    tls_context()

  assert captured.value.message == "TLS initialization failed"
  assert "private" not in captured.value.message


def test_tls_does_not_accept_disabled_verification(certificate, monkeypatch):
  context = SimpleNamespace(
    check_hostname=False,
    verify_mode=ssl.CERT_NONE,
    verify_flags=0,
    load_verify_locations=lambda **kwargs: None,
  )
  monkeypatch.setattr(
    network.ssl, "create_default_context", lambda: context
  )

  with pytest.raises(Failure, match="verification unavailable"):
    tls_context()


@pytest.mark.parametrize(
  ("name", "domain"),
  [
    ("JSESSIONID", CTL_HOST),
    ("JSESSIONID", SSO_HOST),
    ("SSOTOKEN", ".kmu.ac.kr"),
  ],
)
def test_supported_cookie_records_roundtrip(name, domain):
  original = cookie(name, domain)
  record = cookie_record(original)
  validate_cookie(record)
  restored = http.cookiejar.Cookie(**record)

  assert cookie_record(restored) == record


@pytest.mark.parametrize(
  ("field", "value"),
  [
    ("version", True),
    ("version", 2),
    ("name", ""),
    ("name", "bad name"),
    ("name", "x" * 257),
    ("value", None),
    ("value", "a;b"),
    ("value", "한글"),
    ("value", "line\nbreak"),
    ("path", "/private"),
    ("port", "443"),
    ("port_specified", True),
    ("domain", ["ctl.kmu.ac.kr"]),
    ("domain", "example.com"),
    ("domain_initial_dot", True),
    ("expires", True),
    ("expires", -1),
    ("expires", 2**63),
    ("secure", 1),
    ("comment", "url;jsessionid=secret"),
    ("rest", {"name": "url;jsessionid=secret"}),
    ("rest", {"bad name": None}),
  ],
)
def test_malformed_cookie_fields_are_rejected(field, value):
  record = cookie_record(cookie())
  record[field] = value

  with pytest.raises(Failure):
    validate_cookie(record)


def test_domain_cookies_are_limited_to_ssotoken():
  record = cookie_record(cookie("OTHER", ".kmu.ac.kr"))

  with pytest.raises(Failure, match="domain cookie"):
    validate_cookie(record)

  record = cookie_record(cookie("SSOTOKEN", CTL_HOST))
  with pytest.raises(Failure, match="host cookie"):
    validate_cookie(record)


def test_cookie_metadata_is_copied():
  original = cookie()
  record = cookie_record(original)
  record["rest"]["HttpOnly"] = "changed"

  assert original._rest["HttpOnly"] is None


def test_cookie_policy_never_sends_host_cookies_to_other_hosts():
  jar = new_jar()
  jar.set_cookie(cookie("CTLSESSION", CTL_HOST))
  jar.set_cookie(cookie("SSOSESSION", SSO_HOST))
  jar.set_cookie(cookie("SSOTOKEN", ".kmu.ac.kr"))

  ctl_request = urllib.request.Request(CTL + "/Main.do")
  jar.add_cookie_header(ctl_request)
  assert "CTLSESSION=" in ctl_request.get_header("Cookie")
  assert "SSOTOKEN=" in ctl_request.get_header("Cookie")
  assert "SSOSESSION=" not in ctl_request.get_header("Cookie")

  sso_request = urllib.request.Request(SSO + "/login")
  jar.add_cookie_header(sso_request)
  assert "SSOSESSION=" in sso_request.get_header("Cookie")
  assert "SSOTOKEN=" in sso_request.get_header("Cookie")
  assert "CTLSESSION=" not in sso_request.get_header("Cookie")


@pytest.mark.parametrize(
  "url",
  [
    "http://ctl.kmu.ac.kr/",
    "https://other.kmu.ac.kr/",
    "https://example.com/",
  ],
)
def test_cookie_policy_refuses_unapproved_destinations(url):
  jar = new_jar()
  jar.set_cookie(cookie("SSOTOKEN", ".kmu.ac.kr"))
  request = urllib.request.Request(url)
  jar.add_cookie_header(request)

  assert request.get_header("Cookie") is None


def test_policy_rejects_invalid_upstream_cookie_without_raising():
  policy = CookiePolicy()
  invalid = cookie("OTHER", ".kmu.ac.kr")
  request = urllib.request.Request(HOME_URL)

  assert not policy.set_ok(invalid, request)


def test_isolated_jar_has_only_unexpired_ctl_cookies():
  saved = new_jar()
  saved.set_cookie(cookie())
  saved.set_cookie(cookie("SSOTOKEN", ".kmu.ac.kr"))
  saved.set_cookie(cookie("EXPIRED", expires=int(time.time()) - 10))
  isolated = ctl_jar(saved)

  assert [item.name for item in isolated] == ["JSESSIONID"]

  request = urllib.request.Request(HOME_URL)
  isolated.add_cookie_header(request)
  assert request.get_header("Cookie") == "JSESSIONID=test-value"

  sso_request = urllib.request.Request(SSO + "/login")
  isolated.add_cookie_header(sso_request)
  assert sso_request.get_header("Cookie") is None

  next(iter(isolated)).value = "changed"
  assert next(item for item in saved if item.name == "JSESSIONID").value == (
    "test-value"
  )


def test_merging_isolated_jar_preserves_sso_and_ctl_deletions():
  saved = new_jar()
  saved.set_cookie(cookie())
  saved.set_cookie(cookie("SSOTOKEN", ".kmu.ac.kr"))
  isolated = ctl_jar(saved)
  isolated.clear()

  merged = merge_ctl(saved, isolated)
  assert [item.name for item in merged] == ["SSOTOKEN"]


def test_merging_rejects_sso_in_an_isolated_reader():
  with pytest.raises(Failure, match="unexpected SSO"):
    merge_ctl(new_jar(), _jar_with(cookie("SSOTOKEN", ".kmu.ac.kr")))


def _jar_with(item):
  jar = new_jar()
  jar.set_cookie(item)
  return jar


def test_has_cookie_distinguishes_present_from_usable():
  item = cookie(expires=int(time.time()) - 10)
  jar = _jar_with(item)

  assert not has_cookie(jar, "JSESSIONID")
  assert has_cookie(jar, "JSESSIONID", usable=False)

  item = cookie()
  item.value = ""
  jar = _jar_with(item)

  assert not has_cookie(jar, "JSESSIONID")
  assert has_cookie(jar, "JSESSIONID", usable=False)


def test_transport_uses_no_proxies_and_returns_statuses(transport):
  state = transport(Response(status=302))

  assert isinstance(state.handlers[0], urllib.request.ProxyHandler)
  assert state.handlers[0].proxies == {}
  assert isinstance(state.handlers[-1], ReturnStatuses)

  with state.http.open(HOME_URL) as response:
    assert response.status == 302

  assert len(state.calls) == 1
  assert state.response.closed


def test_status_processor_does_not_invoke_redirect_handling():
  response = Response(status=302)
  processor = ReturnStatuses()

  assert processor.http_response(None, response) is response
  assert processor.https_response(None, response) is response


@pytest.mark.parametrize(
  "url",
  [
    "http://ctl.kmu.ac.kr/Main.do",
    "https://example.com/",
    "https://ctl.kmu.ac.kr:443/Main.do",
    "https://user@ctl.kmu.ac.kr/Main.do",
    HOME_URL + "#fragment",
    HOME_URL + "\n",
    CTL + "/한글",
    "https://[invalid",
  ],
)
def test_transport_rejects_unapproved_urls_before_opening(transport, url):
  state = transport()

  with pytest.raises(Failure):
    with state.http.open(url):
      pass

  assert state.calls == []
  assert state.http.requests == 0


@pytest.mark.parametrize(
  "headers",
  [
    {"Cookie": "manual=value"},
    {"Host": "example.com"},
    {"Authorization": "secret"},
    {"Accept-Encoding": "gzip"},
    {"If-None-Match": "a\r\nInjected: yes"},
    {"Bad Name": "value"},
  ],
)
def test_transport_rejects_unsafe_header_overrides(transport, headers):
  state = transport()

  with pytest.raises(Failure, match="outgoing header"):
    with state.http.open(HOME_URL, headers=headers):
      pass

  assert state.calls == []


def test_transport_accepts_conditional_headers(transport):
  state = transport()

  with state.http.open(HOME_URL, headers={"If-None-Match": '"etag"'}):
    assert state.request.get_header("If-none-match") == '"etag"'
    assert state.request.get_header("Accept-encoding") == "identity"

  assert state.calls[0]["timeout"] == network.NETWORK_TIMEOUT


def test_request_secrets_are_removed_after_context(transport):
  jar = _jar_with(cookie())
  state = transport(jar=jar)
  payload = b"password=test-password"

  with state.http.open(HOME_URL, data=payload):
    assert state.request.data == payload
    assert state.request.get_header("Cookie") is not None

  assert state.request.data is None
  assert state.request.get_header("Cookie") is None


def test_cookie_extraction_uses_validated_policy(transport):
  state = transport(Response(headers=[
    ("Set-Cookie", "JSESSIONID=new; Path=/; Secure; HttpOnly"),
    ("Set-Cookie", "OTHER=bad; Domain=.kmu.ac.kr; Path=/"),
  ]))

  with state.http.open(HOME_URL):
    pass

  assert has_cookie(state.jar, "JSESSIONID", ctl_only=True)
  assert not has_cookie(state.jar, "OTHER")


def test_probe_requires_isolated_cookie_policy(transport):
  state = transport()

  with pytest.raises(Failure, match="isolated"):
    with state.http.open(HOME_URL, probe=True):
      pass

  assert state.calls == []
  assert state.http.requests == 0


def test_probe_rejects_plain_standard_library_cookie_jar(transport):
  state = transport(jar=http.cookiejar.CookieJar())

  with pytest.raises(Failure, match="isolated"):
    with state.http.open(HOME_URL, probe=True):
      pass

  assert state.calls == []


@pytest.mark.parametrize("replacement", ["standard", "normal"])
def test_probe_checks_the_current_cookie_policy(transport, replacement):
  jar = new_jar(ctl_only=True)
  state = transport(jar=jar)
  policy = (
    http.cookiejar.DefaultCookiePolicy()
    if replacement == "standard" else CookiePolicy()
  )
  jar.set_policy(policy)

  with pytest.raises(Failure, match="isolated"):
    with state.http.open(HOME_URL, probe=True):
      pass

  assert state.calls == []
  assert state.http.requests == 0


def test_probe_is_restricted_to_ctl_even_with_an_isolated_jar(transport):
  state = transport(jar=new_jar(ctl_only=True))

  with pytest.raises(Failure, match="isolated"):
    with state.http.open(SSO + "/login", probe=True):
      pass

  assert state.calls == []


@pytest.mark.parametrize(
  "header",
  [
    ("Set-Cookie", "SSOTOKEN=new; Path=/"),
    ("Set-Cookie", "JSESSIONID=new; Domain=ctl.kmu.ac.kr; Path=/"),
    ("Set-Cookie2", "JSESSIONID=new; Version=1; Path=/"),
  ],
)
def test_probe_cannot_change_domain_or_sso_cookies(transport, header):
  state = transport(
    Response(headers=[header]), jar=new_jar(ctl_only=True)
  )

  with pytest.raises(Failure, match="probe"):
    with state.http.open(HOME_URL, probe=True):
      pass

  assert len(state.jar) == 0
  assert state.response.closed


def test_probe_accepts_host_only_ctl_cookie(transport):
  state = transport(
    Response(headers=[("Set-Cookie", "JSESSIONID=new; Path=/; Secure")]),
    jar=new_jar(ctl_only=True),
  )

  with state.http.open(HOME_URL, probe=True):
    pass

  assert has_cookie(state.jar, "JSESSIONID", ctl_only=True)


def test_cookie_processing_warnings_are_not_echoed(
  transport, monkeypatch, capsys
):
  state = transport()

  def warn(response, request):
    warnings.warn("private cookie value", RuntimeWarning)

  monkeypatch.setattr(state.jar, "extract_cookies", warn)

  with pytest.raises(Failure, match="cookie processing warning"):
    with state.http.open(HOME_URL):
      pass

  assert "private cookie value" not in capsys.readouterr().err


@pytest.mark.parametrize(
  "headers",
  [
    [("Content-Encoding", "gzip")],
    [("Content-Encoding", "identity"), ("Content-Encoding", "gzip")],
  ],
)
def test_encoded_or_ambiguous_responses_are_rejected(transport, headers):
  state = transport(Response(headers=headers))

  with pytest.raises(Failure, match="response encoding"):
    with state.http.open(HOME_URL):
      pass


def test_request_limit_is_enforced_before_network_access(transport):
  state = transport(limit=0)

  with pytest.raises(Failure, match="request limit"):
    with state.http.open(HOME_URL):
      pass

  assert state.calls == []


def test_stop_event_prevents_requests(transport):
  stop = threading.Event()
  state = transport(stop=stop)
  stop.set()

  with pytest.raises(Cancelled):
    with state.http.open(HOME_URL):
      pass

  assert state.calls == []


def test_html_body_limit_is_enforced(transport, monkeypatch):
  monkeypatch.setattr(network, "HTML_LIMIT", 4)
  state = transport(Response(b"12345"))

  with pytest.raises(Failure, match="byte limit"):
    with state.http.open(HOME_URL) as response:
      state.http.read(response)

  assert state.response.closed


def test_body_limit_accepts_exact_size_and_detects_eof(transport):
  state = transport(Response(b"1234"))

  with state.http.open(HOME_URL) as response:
    assert b"".join(state.http.chunks(response, 4, 120)) == b"1234"

  assert state.http.bytes == 4


def test_total_download_budget_is_enforced(transport, monkeypatch):
  monkeypatch.setattr(network, "DOWNLOAD_LIMIT", 3)
  state = transport(Response(b"1234"))

  with pytest.raises(Failure, match="byte limit"):
    with state.http.open(HOME_URL) as response:
      state.http.read(response)


def test_body_transfer_failure_has_no_upstream_details(transport):
  state = transport()

  def fail(size):
    raise OSError("private server detail")

  state.response.read1 = fail

  with pytest.raises(Failure) as captured:
    with state.http.open(HOME_URL) as response:
      state.http.read(response)

  assert captured.value.message == "body transfer failed or timed out"


def test_body_timeout_is_checked_after_a_read(transport, monkeypatch):
  state = transport(Response(b"data"))
  clock = [0.0]

  def read(size):
    clock[0] = 121.0
    return b"data"

  state.response.read1 = read

  with state.http.open(HOME_URL) as response:
    monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])
    with pytest.raises(Failure, match="body timeout"):
      state.http.read(response)


@pytest.mark.parametrize(
  ("error", "message"),
  [
    (
      urllib.error.URLError("private connection detail"),
      "connection failed or timed out",
    ),
    (
      urllib.error.URLError(ssl.SSLCertVerificationError("private detail")),
      "TLS certificate verification failed",
    ),
    (
      TimeoutError("private detail"),
      "upstream transport failed or timed out",
    ),
  ],
)
def test_transport_failures_have_safe_messages(transport, error, message):
  state = transport(error=error)

  with pytest.raises(Failure) as captured:
    with state.http.open(HOME_URL, data=b"password=secret"):
      pass

  assert captured.value.message == message
  assert state.request.data is None
