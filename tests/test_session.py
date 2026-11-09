import argparse
import copy
import http.cookiejar
import os
import sys
import time

from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import kctl
import kctl.session as session

from kctl.core import CTL_HOST, SSO_HOST, Failure, ProtectionFailure, require
from kctl.session import (
  CONFIG_NAMES,
  State,
  auth_command,
  authenticate,
  configured_credentials,
  read_config,
  state_directory,
)
from kctl.storage import read_private, write_new


COOKIE_FIELDS = {
  "version", "name", "value", "port", "port_specified", "domain",
  "domain_specified", "domain_initial_dot", "path", "path_specified",
  "secure", "expires", "discard", "comment", "comment_url", "rest", "rfc2109",
}


def cookie(name="JSESSIONID", domain=CTL_HOST, expires=None):
  return http.cookiejar.Cookie(
    version=0,
    name=name,
    value="session-value",
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
    rest={},
    rfc2109=False,
  )


def options(command, directory):
  return argparse.Namespace(command=command, state_dir=directory)


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch, private_dir):
  monkeypatch.chdir(private_dir)
  for name in CONFIG_NAMES:
    monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env_file(private_dir):
  def create(text, mode=0o600):
    path = private_dir / ".env"
    write_new(path, text.encode())
    path.chmod(mode)
    return path

  return create


@pytest.fixture
def dependencies(monkeypatch):
  network = ModuleType("kctl.network")
  auth = ModuleType("kctl.auth")
  scenario = SimpleNamespace(
    homes=[("guest", None)],
    pages=[],
    login_page=object(),
    login_error=None,
    logout_error=None,
    calls=[],
    jars=[],
  )

  def new_jar(ctl_only=False):
    return http.cookiejar.CookieJar()

  def ctl_jar(saved):
    jar = new_jar()
    for item in saved:
      if item.domain == CTL_HOST and not item.domain_specified:
        jar.set_cookie(copy.deepcopy(item))
    return jar

  def merge_ctl(saved, updated):
    jar = new_jar()
    for item in saved:
      if item.domain != CTL_HOST or item.domain_specified:
        jar.set_cookie(copy.deepcopy(item))
    for item in updated:
      jar.set_cookie(copy.deepcopy(item))
    return jar

  def cookie_record(item):
    return {
      name: dict(item._rest) if name == "rest" else getattr(item, name)
      for name in COOKIE_FIELDS
    }

  def validate_cookie(value):
    require(
      isinstance(value, dict) and set(value) == COOKIE_FIELDS,
      "malformed test cookie",
    )
    require(
      value["domain"] in {CTL_HOST, SSO_HOST, ".kmu.ac.kr"},
      "unsupported test cookie domain",
    )

  def has_cookie(jar, name, ctl_only=False, usable=True):
    return any(
      item.name == name
      and (not ctl_only or item.domain == CTL_HOST)
      and (not usable or item.value and not item.is_expired())
      for item in jar
    )

  class Auth:
    def __init__(self, jar):
      self.jar = jar
      scenario.jars.append(jar)

    def home(self, probe=False):
      scenario.calls.append(("home", probe))
      value = scenario.homes.pop(0)
      if isinstance(value, BaseException):
        raise value
      page = object()
      scenario.pages.append(page)
      return (*value, page)

    def password_login(self, student_id, password=None):
      scenario.calls.append(("login", student_id, password))
      if scenario.login_error is not None:
        raise scenario.login_error
      self.jar.set_cookie(cookie())
      return scenario.login_page

    def logout(self):
      scenario.calls.append(("logout",))
      if scenario.logout_error is not None:
        raise scenario.logout_error
      self.jar.clear()

  def interactive_terminal():
    scenario.calls.append(("terminal",))

  network.new_jar = new_jar
  network.ctl_jar = ctl_jar
  network.merge_ctl = merge_ctl
  network.cookie_record = cookie_record
  network.validate_cookie = validate_cookie
  network.has_cookie = has_cookie
  auth.Auth = Auth
  auth.interactive_terminal = interactive_terminal

  monkeypatch.setitem(sys.modules, "kctl.network", network)
  monkeypatch.setitem(sys.modules, "kctl.auth", auth)
  monkeypatch.setattr(kctl, "network", network, raising=False)
  monkeypatch.setattr(kctl, "auth", auth, raising=False)

  return scenario


@pytest.fixture
def state(private_dir, dependencies):
  with State(private_dir / "state") as value:
    yield value


def seed(state, student_id="student-1", sso=False):
  jar = http.cookiejar.CookieJar()
  jar.set_cookie(cookie())
  if sso:
    jar.set_cookie(cookie("SSOTOKEN", ".kmu.ac.kr"))
  state.save_login(jar, student_id)
  jar.clear()


def test_missing_configuration_is_empty():
  assert read_config("KCTL_STUDENT_ID", "KCTL_PASSWORD") == {
    "KCTL_STUDENT_ID": "",
    "KCTL_PASSWORD": "",
  }


def test_unknown_configuration_name_is_rejected():
  with pytest.raises(Failure, match="unknown configuration"):
    read_config("UNRELATED_SECRET")


def test_only_requested_values_are_returned(env_file):
  env_file("KCTL_STUDENT_ID=student-1\nUNRELATED_SECRET=hidden\n")

  assert read_config("KCTL_STUDENT_ID") == {
    "KCTL_STUDENT_ID": "student-1",
  }


def test_environment_wins_including_explicit_empty_values(env_file, monkeypatch):
  env_file(
    "KCTL_STUDENT_ID=file-id\nKCTL_PASSWORD=file-password\n",
    mode=0o644,
  )
  monkeypatch.setenv("KCTL_PASSWORD", "")

  assert read_config("KCTL_STUDENT_ID", "KCTL_PASSWORD") == {
    "KCTL_STUDENT_ID": "file-id",
    "KCTL_PASSWORD": "",
  }


def test_complete_environment_configuration_does_not_read_dotenv(
  private_dir, monkeypatch
):
  (private_dir / ".env").mkdir(mode=0o700)
  monkeypatch.setenv("KCTL_STUDENT_ID", "environment-id")
  monkeypatch.setenv("KCTL_PASSWORD", "environment-password")

  assert configured_credentials() == (
    "environment-id", "environment-password"
  )


def test_dotenv_values_are_not_interpolated_or_exported(env_file, monkeypatch):
  monkeypatch.setenv("UNRELATED_SECRET", "expanded")
  env_file(
    'KCTL_STUDENT_ID=student-1\n'
    'KCTL_PASSWORD="literal-${UNRELATED_SECRET}"\n'
  )

  assert configured_credentials() == (
    "student-1", "literal-${UNRELATED_SECRET}"
  )
  assert "KCTL_PASSWORD" not in os.environ
  assert "KCTL_STUDENT_ID" not in os.environ


def test_multiline_certificate_configuration(env_file):
  env_file(
    'KCTL_INTERMEDIATE_PEM="BEGIN\ncertificate\nEND"\n'
    "KCTL_INTERMEDIATE_SHA256=abc\n",
    mode=0o644,
  )

  assert read_config("KCTL_INTERMEDIATE_PEM") == {
    "KCTL_INTERMEDIATE_PEM": "BEGIN\ncertificate\nEND",
  }


def test_dotenv_is_not_searched_in_parent_directories(
  env_file, private_dir, monkeypatch
):
  env_file("KCTL_STUDENT_ID=parent-id\n")
  child = private_dir / "child"
  child.mkdir(mode=0o700)
  monkeypatch.chdir(child)

  assert read_config("KCTL_STUDENT_ID")["KCTL_STUDENT_ID"] == ""


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o400])
def test_password_file_requires_exact_private_permissions(env_file, mode):
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n", mode)

  with pytest.raises(ProtectionFailure, match="0600"):
    configured_credentials()


def test_hard_linked_password_file_is_rejected(env_file, private_dir):
  path = env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n")
  os.link(path, private_dir / "copy")

  with pytest.raises(ProtectionFailure, match="single-linked"):
    configured_credentials()


def test_symlink_dotenv_is_rejected(private_dir):
  target = private_dir / "configuration"
  write_new(target, b"KCTL_STUDENT_ID=student-1\n")
  (private_dir / ".env").symlink_to(target)

  with pytest.raises(ProtectionFailure, match="symlink"):
    read_config("KCTL_STUDENT_ID")


def test_non_regular_dotenv_is_rejected(private_dir):
  os.mkfifo(private_dir / ".env", 0o600)

  with pytest.raises(ProtectionFailure, match="regular file"):
    read_config("KCTL_STUDENT_ID")


def test_dotenv_size_is_bounded(env_file):
  env_file("x" * (session.ENV_LIMIT + 1))

  with pytest.raises(Failure, match="size limit"):
    read_config("KCTL_STUDENT_ID")


def test_invalid_dotenv_encoding_does_not_echo_contents(private_dir):
  write_new(private_dir / ".env", b"KCTL_PASSWORD=secret\xff")

  with pytest.raises(Failure) as captured:
    read_config("KCTL_PASSWORD")

  assert captured.value.message == ".env must be UTF-8"
  assert "secret" not in captured.value.message


def test_password_requires_explicit_id_not_saved_preference(env_file, state):
  seed(state)
  env_file("KCTL_PASSWORD=secret\n")

  with pytest.raises(Failure, match="explicit KCTL_STUDENT_ID"):
    authenticate(state, interactive=False)


def test_invalid_configured_id_is_not_silently_trimmed(env_file):
  env_file('KCTL_STUDENT_ID=" student-1"\n')

  with pytest.raises(Failure, match="STUDENT_ID is invalid"):
    configured_credentials()


def test_state_stores_cookies_and_id_but_not_password(state):
  seed(state)

  assert state.account() == "student-1"
  jar = state.cookies()
  try:
    assert [item.name for item in jar] == ["JSESSIONID"]
  finally:
    jar.clear()

  assert set(path.name for path in state.root.iterdir()) == {
    ".lock", "cookies.json", "account.json",
  }
  assert b"password" not in read_private(state.root / "account.json", 4096)


def test_expired_cookies_are_not_saved(state):
  jar = http.cookiejar.CookieJar()
  jar.set_cookie(cookie(expires=int(time.time()) - 100))
  state.save_cookies(jar)

  assert len(state.cookies()) == 0


def test_duplicate_saved_cookies_are_rejected(state):
  seed(state)
  record = session.parse_json(state.read("cookies.json", session.COOKIE_LIMIT))
  record["cookies"].append(copy.deepcopy(record["cookies"][0]))
  state.write("cookies.json", session.json_bytes(record))

  with pytest.raises(Failure, match="duplicate saved cookie"):
    state.cookies()


def test_cookie_validation_is_applied_on_load(state):
  seed(state)
  record = session.parse_json(state.read("cookies.json", session.COOKIE_LIMIT))
  record["cookies"][0]["domain"] = "example.com"
  state.write("cookies.json", session.json_bytes(record))

  with pytest.raises(Failure, match="unsupported"):
    state.cookies()


def test_saved_account_validation_rejects_boolean_versions(state):
  state.write(
    "account.json",
    session.json_bytes({"version": True, "student_id": "student-1"}),
  )

  with pytest.raises(Failure, match="saved account"):
    state.account()


def test_login_save_removes_old_account_if_cookie_save_fails(state, monkeypatch):
  seed(state, "old-id")

  def fail(jar):
    raise Failure("cookie save failed")

  monkeypatch.setattr(state, "save_cookies", fail)

  with pytest.raises(Failure, match="cookie save failed"):
    state.save_login(http.cookiejar.CookieJar(), "new-id")

  assert state.account() is None


def test_valid_session_avoids_password_submission(state, dependencies, env_file):
  seed(state)
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n")
  dependencies.homes = [("learner", "student-1")]

  jar, page = authenticate(state, interactive=False, quiet=True)
  try:
    assert len(jar) == 1
    assert page is dependencies.pages[0]
    assert dependencies.calls == [("home", True)]
    assert state.account() == "student-1"
    assert all(len(probe) == 0 for probe in dependencies.jars)
  finally:
    jar.clear()


def test_probe_only_receives_ctl_cookies(state, dependencies):
  seed(state, sso=True)
  dependencies.homes = [("learner", "student-1")]

  jar, page = authenticate(state, interactive=False, quiet=True)
  try:
    assert {item.name for item in jar} == {"JSESSIONID", "SSOTOKEN"}
    assert page is dependencies.pages[0]
    assert dependencies.calls == [("home", True)]
  finally:
    jar.clear()


def test_saved_sso_reuse_returns_the_learner_page_not_the_guest_page(
  state, dependencies
):
  seed(state, sso=True)
  dependencies.homes = [("guest", None), ("learner", "student-1")]

  jar, page = authenticate(state, interactive=False, quiet=True)
  try:
    assert dependencies.calls == [("home", True), ("home", False)]
    assert page is dependencies.pages[1]
    assert page is not dependencies.pages[0]
    assert state.account() == "student-1"
    assert {item.name for item in jar} == {"JSESSIONID", "SSOTOKEN"}
  finally:
    jar.clear()


def test_configured_account_mismatch_stops_without_login(
  state, dependencies, env_file
):
  seed(state, "other-id")
  env_file("KCTL_STUDENT_ID=requested-id\nKCTL_PASSWORD=secret\n")
  dependencies.homes = [("learner", "other-id")]

  with pytest.raises(Failure, match="another account"):
    authenticate(state, interactive=False)

  assert dependencies.calls == [("home", True)]
  assert state.account() == "other-id"
  assert all(len(jar) == 0 for jar in dependencies.jars)


def test_sso_fallback_still_checks_configured_identity(
  state, dependencies, env_file
):
  seed(state, "student-1", sso=True)
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n")
  dependencies.homes = [("guest", None), ("learner", "other-student")]

  with pytest.raises(Failure, match="another account"):
    authenticate(state, interactive=False)

  assert dependencies.calls == [("home", True), ("home", False)]
  assert state.account() == "student-1"
  assert all(len(jar) == 0 for jar in dependencies.jars)


def test_unverifiable_learner_identity_is_not_saved(state, dependencies):
  dependencies.homes = [("learner", None)]

  with pytest.raises(Failure, match="could not be verified"):
    authenticate(state, interactive=False)

  assert state.account() is None


def test_configured_credentials_allow_noninteractive_login(
  state, dependencies, env_file, capsys
):
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=private-password\n")

  jar, page = authenticate(state, interactive=False)
  try:
    assert dependencies.calls == [
      ("home", True),
      ("login", "student-1", "private-password"),
    ]
    assert page is dependencies.login_page
    assert page is not dependencies.pages[0]
    assert state.account() == "student-1"
    output = capsys.readouterr()
    assert "private-password" not in output.out + output.err
    assert "signed in" in output.out
    assert "other university sessions" in output.err
  finally:
    jar.clear()


def test_rejected_credentials_are_submitted_once(
  state, dependencies, env_file
):
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n")
  dependencies.login_error = Failure("login not verified", 1)

  with pytest.raises(Failure, match="login not verified"):
    authenticate(state, interactive=True)

  assert sum(call[0] == "login" for call in dependencies.calls) == 1
  assert state.account() is None
  assert all(len(jar) == 0 for jar in dependencies.jars)


def test_transport_failure_never_triggers_password_login(
  state, dependencies, env_file
):
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n")
  dependencies.homes = [Failure("TLS certificate verification failed")]

  with pytest.raises(Failure, match="TLS"):
    authenticate(state, interactive=True)

  assert dependencies.calls == [("home", True)]


def test_noninteractive_missing_credentials_fail_without_prompt(
  state, dependencies, monkeypatch
):
  monkeypatch.setattr(session.sys.stdin, "isatty", lambda: False)
  monkeypatch.setattr(session.sys.stderr, "isatty", lambda: False)

  def unexpected():
    raise AssertionError("must not prompt")

  monkeypatch.setattr("builtins.input", unexpected)

  with pytest.raises(Failure) as captured:
    authenticate(state, interactive=True)

  assert captured.value.code == 1
  assert dependencies.calls == [("home", True)]


def test_configured_id_only_defers_password_prompt_to_auth(
  state, dependencies, env_file, monkeypatch
):
  env_file("KCTL_STUDENT_ID=student-1\n")
  monkeypatch.setattr(session.sys.stdin, "isatty", lambda: True)
  monkeypatch.setattr(session.sys.stderr, "isatty", lambda: True)

  def unexpected():
    raise AssertionError("configured ID must not be prompted again")

  monkeypatch.setattr("builtins.input", unexpected)

  jar, page = authenticate(state, interactive=True, quiet=True)
  try:
    assert dependencies.calls == [
      ("home", True),
      ("login", "student-1", None),
    ]
    assert page is dependencies.login_page
  finally:
    jar.clear()


def test_interactive_login_can_reuse_remembered_id(
  state, dependencies, monkeypatch
):
  seed(state)
  monkeypatch.setattr(session.sys.stdin, "isatty", lambda: True)
  monkeypatch.setattr(session.sys.stderr, "isatty", lambda: True)
  monkeypatch.setattr("builtins.input", lambda: "")

  jar, page = authenticate(state, interactive=True, quiet=True)
  try:
    assert ("terminal",) in dependencies.calls
    assert ("login", "student-1", None) in dependencies.calls
    assert page is dependencies.login_page
  finally:
    jar.clear()


def test_status_never_submits_configured_password(
  state, dependencies, env_file
):
  env_file("KCTL_STUDENT_ID=student-1\nKCTL_PASSWORD=secret\n")

  with pytest.raises(Failure) as captured:
    authenticate(state, interactive=False, login=False)

  assert captured.value.code == 1
  assert dependencies.calls == [("home", True)]


def test_status_command_returns_one_when_signed_out(
  private_dir, dependencies, capsys
):
  code = auth_command(options("status", private_dir / "state"))

  assert code == 1
  assert capsys.readouterr().out == "not signed in\n"
  assert dependencies.calls == [("home", True)]


@pytest.mark.parametrize("command", ["status", "login"])
def test_auth_commands_reuse_session_without_persisting_the_homepage(
  private_dir, dependencies, command
):
  root = private_dir / "state"
  with State(root) as state:
    seed(state)
  dependencies.homes = [("learner", "student-1")]

  assert auth_command(options(command, root)) == 0

  assert dependencies.calls == [("home", True)]
  assert all(len(jar) == 0 for jar in dependencies.jars)
  assert set(path.name for path in root.iterdir()) == {
    ".lock", "account.json", "cookies.json",
  }


def test_forget_does_not_read_configuration_or_import_auth(
  private_dir, monkeypatch
):
  root = private_dir / "state"
  with State(root) as state:
    state.write("cookies.json", b"not JSON")
    state.write("account.json", b"not JSON")

  def unexpected(*args):
    raise AssertionError("configuration must not be read")

  monkeypatch.setattr(session, "read_config", unexpected)
  monkeypatch.setitem(sys.modules, "kctl.auth", None)
  monkeypatch.setitem(sys.modules, "kctl.network", None)

  assert auth_command(options("forget", root)) == 0
  assert not (root / "cookies.json").exists()
  assert not (root / "account.json").exists()
  assert (root / ".lock").exists()


@pytest.mark.parametrize(
  ("error", "expected"),
  [
    (Failure("remote failure"), 2),
    (KeyboardInterrupt(), 130),
  ],
)
def test_logout_clears_local_state_after_remote_failure(
  private_dir, dependencies, error, expected
):
  root = private_dir / "state"
  with State(root) as state:
    seed(state)

  dependencies.logout_error = error

  assert auth_command(options("logout", root)) == expected
  assert not (root / "cookies.json").exists()
  assert not (root / "account.json").exists()
  assert dependencies.calls == [("logout",)]
  assert all(len(jar) == 0 for jar in dependencies.jars)


def test_logout_forgets_malformed_saved_session(private_dir, dependencies):
  root = private_dir / "state"
  with State(root) as state:
    state.write("cookies.json", b"not JSON")

  assert auth_command(options("logout", root)) == 2
  assert not (root / "cookies.json").exists()
  assert dependencies.calls == []


def test_state_context_cleans_abandoned_atomic_writes(private_dir):
  root = private_dir / "state"
  with State(root) as state:
    state.write(".write-" + "a" * 32, b"unfinished")
    state.write("keep", b"keep")

  with State(root) as state:
    assert not state.exists(".write-" + "a" * 32)
    assert state.read("keep", 100) == b"keep"


def test_forget_attempts_both_files_if_one_removal_fails(
  state, monkeypatch
):
  calls = []

  def remove(name):
    calls.append(name)
    if name == "cookies.json":
      raise ProtectionFailure("unsafe")

  monkeypatch.setattr(state, "remove", remove)

  with pytest.raises(Failure, match="cookies.json"):
    state.forget()

  assert calls == ["cookies.json", "account.json"]


def test_state_directory_uses_kctl_namespace(monkeypatch, private_dir):
  monkeypatch.setenv("XDG_STATE_HOME", str(private_dir))
  assert state_directory() == private_dir / "kctl"


def test_state_directory_rejects_relative_xdg_path(monkeypatch):
  monkeypatch.setenv("XDG_STATE_HOME", "relative")

  with pytest.raises(Failure, match="absolute"):
    state_directory()


def test_state_directory_falls_back_to_home(monkeypatch, private_dir):
  monkeypatch.delenv("XDG_STATE_HOME", raising=False)
  monkeypatch.setattr(Path, "home", classmethod(lambda cls: private_dir))

  assert state_directory() == private_dir / ".local" / "state" / "kctl"
