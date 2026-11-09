from __future__ import annotations

import argparse
import getpass
import multiprocessing
import os
import signal
import sys

from pathlib import Path
from typing import Sequence

from .core import (
  Cancelled,
  Failure,
  explanation,
  require,
  terminal,
  valid_id,
)


def default_jobs() -> int:
  try:
    count = len(os.sched_getaffinity(0))
  except (AttributeError, OSError):
    count = os.cpu_count() or 1

  return max(1, min(count, 4))


def positive_integer(value: str) -> int:
  try:
    number = int(value)
  except ValueError:
    raise argparse.ArgumentTypeError("must be an integer") from None

  if number < 1:
    raise argparse.ArgumentTypeError("must be positive")

  return number


def job_count(value: str) -> int:
  number = positive_integer(value)

  if number > 32:
    raise argparse.ArgumentTypeError("must be between 1 and 32")

  return number


def course_id(value: str) -> str:
  if not valid_id(value):
    raise argparse.ArgumentTypeError("must be a nonempty course ID")
  return value


def add_archive(parser: argparse.ArgumentParser) -> None:
  parser.add_argument(
    "archive",
    type=Path,
    nargs="?",
    default=Path("dump"),
    help="archive directory (default: ./dump)",
  )


def add_courses(parser) -> None:
  parser.add_argument(
    "--course",
    type=course_id,
    action="append",
    metavar="ID",
    help="select a course ID (repeatable)",
  )


def add_processing(parser: argparse.ArgumentParser) -> None:
  parser.add_argument(
    "--force",
    action="store_true",
    help="rediscover contents and regenerate local conversion and OCR",
  )
  parser.add_argument(
    "--jobs",
    type=job_count,
    default=default_jobs(),
    metavar="N",
    help="local workers (default: up to 4 available CPUs)",
  )
  parser.add_argument(
    "--job-timeout",
    type=positive_integer,
    default=900,
    metavar="SECONDS",
    help="time limit per downloaded-root processing job (default: 900)",
  )


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
  values = list(sys.argv[1:] if argv is None else argv)
  common = argparse.ArgumentParser(
    prog="kctl", add_help=False, allow_abbrev=False
  )
  common.add_argument(
    "--state-dir",
    type=Path,
    metavar="PATH",
    help="private authentication-state directory",
  )
  global_options, remaining = common.parse_known_args(values)

  if not remaining:
    remaining = ["sync"]
  elif (
    remaining[0].startswith("-")
    and remaining[0] not in {"-h", "--help", "--"}
  ):
    remaining.insert(0, "sync")

  parser = argparse.ArgumentParser(
    prog="kctl",
    parents=[common],
    allow_abbrev=False,
    description=(
      "Archive CTL courses, extract documents, run English OCR, "
      "and export clean course text."
    ),
    epilog=(
      "With no command, run sync using ./dump. "
      "process and export are local-only."
    ),
  )
  commands = parser.add_subparsers(dest="command", required=True)

  sync = commands.add_parser(
    "sync",
    allow_abbrev=False,
    help="retrieve, extract, convert, OCR, and export (default)",
  )
  add_archive(sync)
  add_courses(sync)
  add_processing(sync)
  sync.add_argument(
    "--refresh",
    action="store_true",
    help="also fetch cached originals that lack HTTP validators",
  )
  sync.add_argument(
    "--no-submissions",
    action="store_false",
    dest="include_submissions",
    help="exclude submission files and inline submission images",
  )

  process = commands.add_parser(
    "process",
    allow_abbrev=False,
    help="extract, convert, OCR, and export locally",
  )
  add_archive(process)
  selection = process.add_mutually_exclusive_group()
  add_courses(selection)
  selection.add_argument(
    "--file",
    action="append",
    metavar="PATH",
    help="process an archive-relative original path (repeatable)",
  )
  add_processing(process)

  export = commands.add_parser(
    "export",
    allow_abbrev=False,
    help="assemble existing content into one text file per course",
  )
  add_archive(export)
  add_courses(export)

  descriptions = {
    "login": "reuse the session or sign in if necessary",
    "status": "check authentication without initiating login",
    "logout": "sign out remotely and remove local credentials",
    "forget": "remove local credentials without network access",
  }
  for name, description in descriptions.items():
    commands.add_parser(
      name, allow_abbrev=False, help=description
    )

  options = parser.parse_args(remaining)
  options.state_dir = global_options.state_dir
  return options


def require_runtime() -> None:
  require(
    os.name == "posix"
    and (3, 12) <= sys.version_info[:2] < (3, 14),
    "requires POSIX and Python 3.12 or 3.13",
  )
  require(
    all(
      hasattr(os, name)
      for name in (
        "O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK",
      )
    )
    and hasattr(signal, "pthread_sigmask"),
    "required POSIX protections unavailable",
  )


def dispatch(options: argparse.Namespace) -> int:
  if options.command in {"sync", "process", "export"}:
    from .workflows import run_export, run_process, run_sync

    handlers = {
      "sync": run_sync,
      "process": run_process,
      "export": run_export,
    }
    return handlers[options.command](options)

  from .session import auth_command

  return auth_command(options)


def main(argv: Sequence[str] | None = None) -> int:
  try:
    options = arguments(argv)
    require_runtime()
    os.umask(0o077)
    multiprocessing.freeze_support()
    return dispatch(options)

  except Failure as error:
    print("error: " + terminal(error.message), file=sys.stderr)
    return error.code

  except (KeyboardInterrupt, EOFError, Cancelled):
    print("interrupted", file=sys.stderr)
    return 130

  except getpass.GetPassWarning:
    print(
      "error: non-echoing password input unavailable",
      file=sys.stderr,
    )
    return 2

  except Exception as error:
    print("error: " + terminal(explanation(error)), file=sys.stderr)
    return 2
