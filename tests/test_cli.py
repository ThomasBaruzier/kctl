import getpass
import sys

from pathlib import Path
from types import ModuleType

import pytest

import kctl.cli as cli

from kctl.core import Cancelled, Failure


def test_no_command_selects_complete_sync():
  options = cli.arguments([])

  assert options.command == "sync"
  assert options.archive == Path("dump")
  assert options.include_submissions is True
  assert options.refresh is False
  assert options.force is False
  assert options.course is None
  assert options.state_dir is None
  assert options.job_timeout == 900
  assert 1 <= options.jobs <= 4


def test_implicit_sync_accepts_refresh_and_courses():
  options = cli.arguments([
    "--refresh", "--course", "course-1", "--course", "course-2",
  ])

  assert options.command == "sync"
  assert options.refresh is True
  assert options.course == ["course-1", "course-2"]


def test_option_value_named_like_a_command_is_not_a_command():
  options = cli.arguments(["--course", "export"])

  assert options.command == "sync"
  assert options.course == ["export"]


def test_explicit_sync_accepts_custom_archive():
  options = cli.arguments([
    "sync", "./semester", "--force", "--jobs", "2",
    "--job-timeout", "1200", "--no-submissions",
  ])

  assert options.archive == Path("semester")
  assert options.force is True
  assert options.jobs == 2
  assert options.job_timeout == 1200
  assert options.include_submissions is False


@pytest.mark.parametrize(
  "argv",
  [
    ["--state-dir", "private", "sync"],
    ["sync", "--state-dir", "private"],
    ["--state-dir=private"],
  ],
)
def test_global_state_directory(argv):
  options = cli.arguments(argv)

  assert options.command == "sync"
  assert options.state_dir == Path("private")


def test_state_directory_value_is_not_mistaken_for_a_command():
  options = cli.arguments(["--state-dir", "export"])

  assert options.command == "sync"
  assert options.state_dir == Path("export")


def test_process_supports_explicit_originals():
  options = cli.arguments([
    "process",
    "--file", "documents/Course/a/original.pdf",
    "--file", "documents/Course/b/source.py",
    "--force",
  ])

  assert options.command == "process"
  assert options.archive == Path("dump")
  assert options.file == [
    "documents/Course/a/original.pdf",
    "documents/Course/b/source.py",
  ]
  assert options.course is None
  assert options.force is True


def test_process_supports_repeated_courses():
  options = cli.arguments([
    "process", "semester", "--course", "one", "--course", "two",
  ])

  assert options.archive == Path("semester")
  assert options.course == ["one", "two"]
  assert options.file is None


def test_export_has_no_processing_flags():
  options = cli.arguments(["export", "--course", "one"])

  assert options.command == "export"
  assert options.archive == Path("dump")
  assert options.course == ["one"]
  assert not hasattr(options, "jobs")
  assert not hasattr(options, "force")


@pytest.mark.parametrize("command", ["login", "status", "logout", "forget"])
def test_authentication_commands(command):
  options = cli.arguments([command])

  assert options.command == command
  assert not hasattr(options, "archive")


@pytest.mark.parametrize(
  "argv",
  [
    ["./semester"],
    ["dump"],
    ["convert"],
    ["ocr"],
    ["sync", "--convert"],
    ["sync", "--ocr"],
    ["sync", "--include-submissions"],
    ["sync", "--output", "dump"],
    ["sync", "--jobs", "0"],
    ["sync", "--jobs", "33"],
    ["sync", "--jobs", "two"],
    ["sync", "--job-timeout", "0"],
    ["sync", "--course", ""],
    ["sync", "--course", " padded"],
    ["sync", "--ref"],
    ["process", "--refresh"],
    ["process", "--no-submissions"],
    ["process", "--course", "one", "--file", "documents/file.txt"],
    ["export", "--force"],
    ["export", "--jobs", "2"],
    ["status", "--refresh"],
    ["login", "dump"],
    ["--state-dir"],
  ],
)
def test_invalid_arguments_fail_without_dispatch(argv):
  with pytest.raises(SystemExit) as captured:
    cli.arguments(argv)

  assert captured.value.code == 2


def test_root_help_describes_default_without_running_workflow(capsys):
  with pytest.raises(SystemExit) as captured:
    cli.arguments(["--help"])

  assert captured.value.code == 0
  text = capsys.readouterr().out
  assert "With no command, run sync" in text
  assert "local-only" in text


def test_implicit_sync_help(capsys):
  with pytest.raises(SystemExit) as captured:
    cli.arguments(["--refresh", "--help"])

  assert captured.value.code == 0
  text = capsys.readouterr().out
  assert "--no-submissions" in text
  assert "--refresh" in text


def test_default_jobs_prefers_affinity_and_caps_workers(monkeypatch):
  monkeypatch.setattr(
    cli.os, "sched_getaffinity", lambda pid: set(range(20)), raising=False
  )
  assert cli.default_jobs() == 4

  monkeypatch.setattr(cli.os, "sched_getaffinity", lambda pid: {1})
  assert cli.default_jobs() == 1


def test_default_jobs_falls_back_when_affinity_is_unavailable(monkeypatch):
  def unavailable(pid):
    raise OSError("unavailable")

  monkeypatch.setattr(
    cli.os, "sched_getaffinity", unavailable, raising=False
  )
  monkeypatch.setattr(cli.os, "cpu_count", lambda: None)
  assert cli.default_jobs() == 1


@pytest.mark.parametrize(
  ("command", "handler"),
  [
    ("sync", "run_sync"),
    ("process", "run_process"),
    ("export", "run_export"),
  ],
)
def test_workflow_dispatch_is_explicit_and_lazy(monkeypatch, command, handler):
  calls = []
  module = ModuleType("kctl.workflows")

  def callback(options):
    calls.append(options.command)
    return 3

  for name in ("run_sync", "run_process", "run_export"):
    setattr(module, name, callback)

  monkeypatch.setitem(sys.modules, "kctl.workflows", module)
  options = cli.arguments([command])

  assert cli.dispatch(options) == 3
  assert calls == [command]
  assert getattr(module, handler) is callback


def test_authentication_dispatch_uses_session_handler(monkeypatch):
  calls = []
  module = ModuleType("kctl.session")

  def callback(options):
    calls.append(options.command)
    return 1

  module.auth_command = callback
  monkeypatch.setitem(sys.modules, "kctl.session", module)

  assert cli.dispatch(cli.arguments(["status"])) == 1
  assert calls == ["status"]


@pytest.fixture
def runtime(monkeypatch):
  calls = []
  monkeypatch.setattr(cli, "require_runtime", lambda: None)
  monkeypatch.setattr(cli.os, "umask", lambda value: calls.append(value))
  monkeypatch.setattr(
    cli.multiprocessing, "freeze_support", lambda: None
  )
  return calls


def test_main_preserves_workflow_exit_status(monkeypatch, runtime):
  monkeypatch.setattr(cli, "dispatch", lambda options: 3)

  assert cli.main([]) == 3
  assert runtime == [0o077]


@pytest.mark.parametrize(
  ("error", "code", "message"),
  [
    (Failure("not signed in", 1), 1, "not signed in"),
    (Failure("archive damaged"), 2, "archive damaged"),
    (KeyboardInterrupt(), 130, "interrupted"),
    (EOFError(), 130, "interrupted"),
    (Cancelled(), 130, "interrupted"),
    (
      getpass.GetPassWarning("unsafe input"),
      2,
      "non-echoing password input unavailable",
    ),
    (ValueError("private information"), 2, "unexpected ValueError"),
  ],
)
def test_main_reports_failures_without_exception_details(
  monkeypatch, runtime, capsys, error, code, message
):
  def fail(options):
    raise error

  monkeypatch.setattr(cli, "dispatch", fail)

  assert cli.main([]) == code
  stderr = capsys.readouterr().err
  assert message in stderr
  assert "private information" not in stderr
  assert "unsafe input" not in stderr


def test_main_sanitizes_known_error_output(monkeypatch, runtime, capsys):
  def fail(options):
    raise Failure("bad\n\x1b[31mvalue")

  monkeypatch.setattr(cli, "dispatch", fail)

  assert cli.main([]) == 2
  stderr = capsys.readouterr().err
  assert "\x1b" not in stderr
  assert stderr.count("\n") == 1


def test_help_does_not_run_platform_checks(monkeypatch):
  def unexpected():
    raise AssertionError("runtime check must not run for help")

  monkeypatch.setattr(cli, "require_runtime", unexpected)

  with pytest.raises(SystemExit) as captured:
    cli.main(["--help"])

  assert captured.value.code == 0


def test_cli_module_does_not_import_heavy_dependencies():
  assert "markitdown" not in cli.__dict__
  assert "tesserocr" not in cli.__dict__
  assert "pypdfium2" not in cli.__dict__
