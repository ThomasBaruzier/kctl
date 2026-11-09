from __future__ import annotations

import argparse
import errno
import http.cookiejar
import io
import os
import stat
import sys

from pathlib import Path
from typing import Any

from .core import (
  Cancelled,
  Failure,
  ProtectionFailure,
  cancelled_exception,
  explanation,
  json_bytes,
  parse_json,
  require,
  terminal,
  valid_id,
)
from .storage import Tree, checked_file


ENV_LIMIT = 128 * 1024
COOKIE_LIMIT = 512 * 1024
CONFIG_NAMES = frozenset({
  "KCTL_STUDENT_ID",
  "KCTL_PASSWORD",
  "KCTL_INTERMEDIATE_PEM",
  "KCTL_INTERMEDIATE_SHA256",
})


def _file_config(names: set[str]) -> dict[str, str]:
  from dotenv import dotenv_values

  path = Path.cwd() / ".env"
  flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

  try:
    descriptor = os.open(path, flags)
  except FileNotFoundError:
    return {}
  except OSError as error:
    if error.errno == errno.ELOOP:
      raise ProtectionFailure(".env must not be a symlink") from None
    raise Failure("cannot open .env: " + explanation(error)) from None

  try:
    with os.fdopen(descriptor, "rb") as stream:
      before = os.fstat(stream.fileno())
      if not stat.S_ISREG(before.st_mode):
        raise ProtectionFailure(".env must be a regular file")

      require(before.st_size <= ENV_LIMIT, ".env exceeds size limit")
      raw = stream.read(ENV_LIMIT + 1)
      after = os.fstat(stream.fileno())

    require(len(raw) <= ENV_LIMIT, ".env exceeds size limit")
    require(
      all(
        getattr(before, field) == getattr(after, field)
        for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
      ),
      ".env changed while being read",
    )
    text = raw.decode("utf-8-sig")
    require("\x00" not in text, ".env contains NUL bytes")
    values = dotenv_values(stream=io.StringIO(text), interpolate=False)

    try:
      if "KCTL_PASSWORD" in names and values.get("KCTL_PASSWORD"):
        try:
          checked_file(before)
          checked_file(after)
        except ProtectionFailure:
          raise ProtectionFailure(
            ".env supplying a password must be owned, mode 0600, and single-linked"
          ) from None

      return {name: values.get(name) or "" for name in names}
    finally:
      values.clear()

  except UnicodeError:
    raise Failure(".env must be UTF-8") from None
  except OSError as error:
    raise Failure("cannot read .env: " + explanation(error)) from None


def read_config(*names: str) -> dict[str, str]:
  require(
    all(name in CONFIG_NAMES for name in names),
    "unknown configuration field",
  )
  values = {
    name: os.environ[name] for name in names if name in os.environ
  }
  missing = set(names) - values.keys()

  if missing:
    configured = _file_config(missing)
    values.update({name: configured.get(name, "") for name in missing})
    configured.clear()

  return values


def configured_credentials() -> tuple[str | None, str | None]:
  values = read_config("KCTL_STUDENT_ID", "KCTL_PASSWORD")

  try:
    student_id = values["KCTL_STUDENT_ID"] or None
    password = values["KCTL_PASSWORD"] or None
    require(
      student_id is None or valid_id(student_id),
      "KCTL_STUDENT_ID is invalid",
    )
    require(
      password is None or student_id is not None,
      "KCTL_PASSWORD requires an explicit KCTL_STUDENT_ID",
    )
    return student_id, password
  finally:
    values.clear()


class State(Tree):
  def __enter__(self) -> State:
    try:
      self.lock()
      self.clean_atomic()
      return self
    except BaseException:
      self.close()
      raise

  def __exit__(self, *args: Any) -> None:
    self.close()

  def account(self) -> str | None:
    if not self.exists("account.json"):
      return None

    record = parse_json(self.read("account.json", 4096))
    require(
      isinstance(record, dict)
      and set(record) == {"version", "student_id"}
      and type(record["version"]) is int
      and record["version"] == 1
      and valid_id(record["student_id"]),
      "malformed saved account",
    )
    return record["student_id"]

  def cookies(self) -> http.cookiejar.CookieJar:
    from .network import new_jar, validate_cookie

    jar = new_jar()

    try:
      if not self.exists("cookies.json"):
        return jar

      record = parse_json(self.read("cookies.json", COOKIE_LIMIT))
      require(
        isinstance(record, dict)
        and set(record) == {"version", "cookies"}
        and type(record["version"]) is int
        and record["version"] == 1
        and isinstance(record["cookies"], list)
        and len(record["cookies"]) <= 128,
        "malformed saved session",
      )
      seen = set()

      for fields in record["cookies"]:
        validate_cookie(fields)
        key = fields["domain"], fields["path"], fields["name"]
        require(key not in seen, "duplicate saved cookie")
        seen.add(key)
        cookie = http.cookiejar.Cookie(**fields)

        if not cookie.is_expired():
          jar.set_cookie(cookie)

      return jar

    except BaseException:
      jar.clear()
      raise

  def save_cookies(self, jar: http.cookiejar.CookieJar) -> None:
    from .network import cookie_record, validate_cookie

    records = []

    for cookie in jar:
      if cookie.is_expired():
        continue
      record = cookie_record(cookie)
      validate_cookie(record)
      records.append(record)

    require(len(records) <= 128, "too many cookies")
    raw = json_bytes({"version": 1, "cookies": records})
    require(len(raw) <= COOKIE_LIMIT, "session state too large")
    self.write("cookies.json", raw)

  def save_login(
    self, jar: http.cookiejar.CookieJar, student_id: str
  ) -> None:
    require(valid_id(student_id), "invalid verified account")
    self.remove("account.json")
    self.save_cookies(jar)
    self.write(
      "account.json",
      json_bytes({"version": 1, "student_id": student_id}),
    )

  def forget(self) -> None:
    failed = []

    for name in ("cookies.json", "account.json"):
      try:
        self.remove(name)
      except (Failure, OSError):
        failed.append(name)

    require(not failed, "could not remove: " + ", ".join(failed))


def state_directory() -> Path:
  configured = os.environ.get("XDG_STATE_HOME")

  if configured:
    require(Path(configured).is_absolute(), "XDG_STATE_HOME must be absolute")
    return Path(configured) / "kctl"

  return Path.home().resolve(strict=True) / ".local" / "state" / "kctl"


def _can_prompt(interactive: bool) -> bool:
  return interactive and sys.stdin.isatty() and sys.stderr.isatty()


def _student_id(remembered: str | None) -> str:
  from .auth import interactive_terminal

  interactive_terminal()
  prompt = f"student id [{remembered}]: " if remembered else "student id: "
  print(prompt, end="", file=sys.stderr, flush=True)
  student_id = input() or remembered
  require(valid_id(student_id), "invalid student id")
  return student_id


def authenticate(
  state: State,
  interactive: bool,
  quiet: bool = False,
  *,
  login: bool = True,
) -> tuple[http.cookiejar.CookieJar, Any]:
  from .auth import Auth
  from .network import ctl_jar, has_cookie, merge_ctl, new_jar

  configured_id = read_config("KCTL_STUDENT_ID")["KCTL_STUDENT_ID"] or None
  require(
    configured_id is None or valid_id(configured_id),
    "KCTL_STUDENT_ID is invalid",
  )
  password = None
  saved = probe = merged = fresh = None

  try:
    saved = state.cookies()
    probe = ctl_jar(saved)
    role, account, page = Auth(probe).home(probe=True)
    merged = merge_ctl(saved, probe)

    if role == "guest" and has_cookie(merged, "SSOTOKEN"):
      role, account, page = Auth(merged).home()

    require(role in {"learner", "guest"}, "unexpected authentication role")

    if role == "learner":
      require(valid_id(account), "authenticated account could not be verified")
      require(
        configured_id is None or configured_id == account,
        "saved session belongs to another account; use kctl forget before switching",
      )
      state.save_login(merged, account)

      if not quiet:
        print("signed in")

      result = merged
      merged = None
      return result, page

    require(login, "not signed in", 1)
    student_id, password = configured_credentials()
    require(
      student_id == configured_id,
      "authentication configuration changed; rerun the command",
    )
    require(
      password is not None or _can_prompt(interactive),
      "not signed in; configure credentials or run kctl login in a terminal",
      1,
    )

    print(
      "warning: login may end other university sessions",
      file=sys.stderr,
      flush=True,
    )
    remembered = state.account()
    student_id = student_id or _student_id(remembered)

    if student_id != remembered:
      state.remove("cookies.json")

    fresh = new_jar()
    page = Auth(fresh).password_login(student_id, password=password)
    state.save_login(fresh, student_id)

    if not quiet:
      print("signed in")

    result = fresh
    fresh = None
    return result, page

  finally:
    password = None
    for jar in (saved, probe, merged, fresh):
      if jar is not None:
        jar.clear()


def _logout_or_forget(state: State, command: str) -> int:
  jar = None
  code = 0

  try:
    if command == "forget":
      print("remote sessions unchanged")
    else:
      from .auth import Auth
      from .network import has_cookie

      jar = state.cookies()

      if has_cookie(jar, "SSOTOKEN") or has_cookie(jar, "JSESSIONID"):
        print(
          "warning: logout may end other university sessions",
          file=sys.stderr,
          flush=True,
        )
        Auth(jar).logout()
        print("signed out")
      else:
        print("no saved session")

  except (Failure, OSError, KeyboardInterrupt, EOFError, Cancelled) as error:
    print(
      "warning: remote logout not confirmed: " + terminal(explanation(error)),
      file=sys.stderr,
    )
    code = 130 if cancelled_exception(error) else 2

  finally:
    if jar is not None:
      jar.clear()

    try:
      state.forget()
      print("local credentials removed")
    except (Failure, OSError) as error:
      print("error: " + terminal(explanation(error)), file=sys.stderr)
      if code != 130:
        code = 2

  return code


def auth_command(options: argparse.Namespace) -> int:
  directory = options.state_dir or state_directory()

  with State(directory) as state:
    if options.command in {"logout", "forget"}:
      return _logout_or_forget(state, options.command)

    try:
      jar, _ = authenticate(
        state,
        interactive=options.command == "login",
        login=options.command == "login",
      )
    except Failure as error:
      if options.command == "status" and error.code == 1:
        print("not signed in")
        return 1
      raise

    jar.clear()
    return 0
