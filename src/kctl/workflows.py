from __future__ import annotations

import argparse
import copy
import queue
import signal
import sys
import threading
import time

from collections import Counter
from contextlib import contextmanager
from typing import Any, Iterator

from .core import (
  Failure,
  StorageFailure,
  cancelled_exception,
  explanation,
  join_path,
  require,
  terminal,
)
from .network import ctl_jar, merge_ctl
from .reader import Home, Reader
from .session import State, authenticate, state_directory


@contextmanager
def _ignore_interrupts() -> Iterator[None]:
  previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
  try:
    yield
  finally:
    signal.signal(signal.SIGINT, previous)


def select_courses(
  archive: Any,
  course_ids: list[str] | None = None,
  *,
  missing_ok: bool = False,
) -> list[str]:
  courses = {
    item["course_id"]: key
    for key, item in archive.data["items"].items()
    if "course_id" in item
  }
  requested = set(courses if course_ids is None else course_ids)
  missing = requested - courses.keys()
  require(
    missing_ok or not missing,
    "requested course is not archived: " + ", ".join(sorted(missing)),
  )
  keys = [courses[course_id] for course_id in requested & courses.keys()]
  return sorted(
    keys,
    key=lambda key: (
      archive.data["items"][key]["folder"].casefold(),
      archive.data["items"][key]["course_id"],
    ),
  )


def _file_selection(
  archive: Any, paths: list[str]
) -> tuple[list[str], list[str]]:
  originals = {
    join_path(doc["folder"], doc["original"]): key
    for key, doc in archive.data["documents"].items()
  }
  selected = set(paths)
  require(
    selected <= originals.keys(),
    "--file must name an archive-relative downloaded original",
  )
  keys = [originals[path] for path in sorted(selected)]
  courses = {
    course
    for key in keys
    if (course := archive.course_for_document(key)) is not None
  }
  return keys, sorted(courses)


def _enqueue(archive: Any, processor: Any, keys: list[str]) -> None:
  ordered = sorted(
    set(keys),
    key=lambda key: (-archive.data["documents"][key]["bytes"], key),
  )
  for key in ordered:
    processor.enqueue(key)


def _progress(archive: Any, processor: Any, previous: float) -> float:
  archive.flush()
  current = time.monotonic()

  if current - previous >= 5:
    archive.flush_views()
    processor.progress()
    return current

  return previous


def _receive(archive: Any, processor: Any, event: Any) -> dict | None:
  data = event.data

  if event.kind == "message":
    print(terminal(data["text"]), file=sys.stderr, flush=True)

  elif event.kind == "document":
    archive.document(data["key"], data["record"], data["work"])
    processor.enqueue(data["key"])

  elif event.kind == "item":
    archive.item(data["key"], data["changes"], data["body"])

  elif event.kind == "done":
    require(
      data.get("status") in {"complete", "failed"},
      "invalid retrieval completion status",
    )
    require(
      type(data.get("code")) is int
      and (
        data["status"] == "complete" and data["code"] == 0
        or data["status"] == "failed" and data["code"] in {1, 2}
      ),
      "invalid retrieval completion code",
    )

    if data.get("fatal_local"):
      raise StorageFailure(data.get("message") or "retrieval storage failure")

    archive.data["run"]["upstream"] = copy.deepcopy(data)
    archive.checkpoint()
    return data

  else:
    raise Failure("unknown retrieval event")

  return None


def _stop_retrieval(stop: threading.Event, thread: threading.Thread) -> None:
  stop.set()

  with _ignore_interrupts():
    if thread.is_alive():
      thread.join(timeout=0.1)

    if thread.is_alive():
      print("waiting for network request", file=sys.stderr, flush=True)
      thread.join()


def _retrieve(
  archive: Any,
  processor: Any,
  reader: Reader,
  home: Home,
  options: argparse.Namespace,
  stop: threading.Event,
) -> dict[str, Any]:
  from .retrieval import Producer

  events = queue.Queue(maxsize=16)
  producer = Producer(
    reader=reader,
    root=archive.root,
    snapshot=copy.deepcopy({
      "items": archive.data["items"],
      "documents": archive.data["documents"],
    }),
    options=options,
    events=events,
    stop=stop,
    home=home,
  )
  thread = threading.Thread(target=producer.run, name="kctl-upstream")
  previous = time.monotonic()
  report = None

  try:
    thread.start()

    while report is None:
      processor.tick()

      for handled in range(8):
        try:
          event = events.get(timeout=0.05 if handled == 0 else 0)
        except queue.Empty:
          break

        report = _receive(archive, processor, event)
        if report is not None:
          break

      previous = _progress(archive, processor, previous)

      if report is None and not thread.is_alive() and events.empty():
        raise Failure("retrieval thread ended without completion record")

    return report

  finally:
    _stop_retrieval(stop, thread)


def _process_pending(archive: Any, processor: Any) -> None:
  previous = time.monotonic()

  while processor.busy():
    processor.tick()
    previous = _progress(archive, processor, previous)
    if processor.busy():
      time.sleep(0.02)

  processor.close()


def _combined_code(*codes: int) -> int:
  require(
    all(type(code) is int and code in {0, 1, 2, 3, 130} for code in codes),
    "invalid workflow completion code",
  )
  return next(code for code in (130, 2, 1, 3, 0) if code in codes)


def _processing_summary(processor: Any) -> None:
  summary = processor.summary()
  print(
    f"local roots: {summary['processed_roots']} processed, "
    f"{summary['reused_roots']} reused, "
    f"{summary['failed_roots']} failed, "
    f"{summary['limited_roots']} with limitations"
  )


def _incomplete_reasons(archive: Any, course_key: str) -> str:
  items = archive.data["items"]
  documents = archive.data["documents"]
  counts: Counter[str] = Counter()
  pending = [course_key]
  seen = set()
  comment_warning = (
    "displayed posts and comments retained; comment completeness is not verified"
  )

  while pending:
    key = pending.pop()
    if key in seen:
      continue
    seen.add(key)
    item = items.get(key)

    if item is None:
      counts["referenced CTL page is not recorded"] += 1
      continue

    reasons = set()
    if key in archive.missing:
      reasons.add("stored page missing")
    if key in archive.damaged:
      reasons.add(archive.damaged[key])

    if item.get("status") in {"failed", "partial", "unsupported"}:
      reasons.update(item.get("problems", []))
      message = item.get("message")
      if message or not item.get("problems"):
        reasons.add(message or item["status"])

    if any(doc not in documents for doc in item.get("documents", [])):
      reasons.add("referenced attachment record is missing")

    counts.update({
      "comment completeness unverified" if reason == comment_warning else reason
      for reason in reasons
    })
    pending.extend(item.get("children", []))
    pending.extend(item.get("related", []))

  parts = [
    f"{reason} ({count} {'area' if count == 1 else 'areas'})"
    for reason, count in sorted(counts.items())[:3]
  ]
  if len(counts) > 3:
    parts.append(f"{len(counts) - 3} other reasons; see extraction notes")

  return "; ".join(parts) or "incomplete; see extraction notes"


def _complete(
  archive: Any,
  processor: Any,
  courses: list[str],
  started: float,
  *,
  upstream: dict[str, Any] | None = None,
  missing_courses: int = 0,
) -> int:
  from .exports import export_courses

  _process_pending(archive, processor)
  archive.scan_missing()
  archive.flush_views()
  incomplete = archive.incomplete()
  affected = sum(key in incomplete for key in courses)
  dashboard = upstream is not None and "dashboard" in incomplete
  summary = processor.summary()
  partial = bool(
    affected
    or dashboard
    or missing_courses
    or summary["failed_roots"]
    or summary["limited_roots"]
  )

  archive.cleanup_work()
  exported = export_courses(archive, courses) if courses else 0
  require(exported in {0, 2, 3}, "invalid export completion code")
  code = _combined_code(
    upstream["code"] if upstream is not None else 0,
    3 if partial else 0,
    exported,
  )
  status = "complete" if code == 0 else "partial" if code == 3 else "failed"
  details = {
    "local": summary,
    "incomplete_courses": affected,
    "unrecorded_courses": missing_courses,
    "elapsed_seconds": round(time.monotonic() - started, 3),
  }
  if upstream is not None:
    details["upstream"] = upstream
    details["incomplete_dashboard"] = dashboard

  archive.finish(status, **details)
  print("archive:", archive.root)

  if upstream is not None:
    print(
      f"downloads: {upstream.get('downloaded', 0)} downloaded, "
      f"{upstream.get('revalidated', 0)} revalidated, "
      f"{upstream.get('reused', 0)} reused"
    )
    if dashboard:
      print("dashboard: incomplete")

  _processing_summary(processor)
  print(f"courses: {affected + missing_courses} incomplete")

  for key in courses:
    if key in incomplete:
      title = archive.data["items"][key]["title"]
      print("  " + terminal(title + ": " + _incomplete_reasons(archive, key)))

  print("result:", status)
  print(f"elapsed: {time.monotonic() - started:.1f}s")
  return code


def _abort(archive: Any, processor: Any, error: BaseException) -> None:
  interrupted = cancelled_exception(error)
  errors = []

  with _ignore_interrupts():
    if interrupted:
      print("\nstopping", file=sys.stderr, flush=True)

    if processor is not None:
      try:
        processor.close(
          "interrupted before publication"
          if interrupted else "stopped after fatal run failure"
        )
      except BaseException as cleanup_error:
        errors.append("worker cleanup: " + explanation(cleanup_error))

    if archive is not None and archive.active_run:
      try:
        if archive.exists(".ctl/pending.json"):
          print("publication pending; recovery on next run", file=sys.stderr)
        else:
          archive.finish(
            "interrupted" if interrupted else "failed",
            error=explanation(error),
            local=processor.summary() if processor is not None else {},
          )
      except BaseException as cleanup_error:
        errors.append("archive finalization: " + explanation(cleanup_error))

  for message in errors:
    print("warning: " + terminal(message), file=sys.stderr)


def run_sync(options: argparse.Namespace) -> int:
  from .archive import Archive
  from .processing import Processor

  archive = processor = None
  started = time.monotonic()
  stop = threading.Event()

  try:
    with State(options.state_dir or state_directory()) as state:
      saved = isolated = None

      try:
        saved, page = authenticate(state, interactive=True, quiet=True)
        isolated = ctl_jar(saved)
        reader = Reader(isolated, stop)
        home = reader.home(page)

        require(
          home.account == state.account(),
          "authenticated account changed before retrieval",
        )
        selected = set(options.course or home.courses)
        require(
          selected <= home.courses.keys(),
          "requested course is not enrolled",
        )

        archive = Archive(options.archive, create=True)
        require(
          archive.data["account"] in {None, home.account},
          "archive belongs to another account",
        )
        archive.prepare()
        archive.account(home.account)
        archive.begin("sync")

        for key in select_courses(archive, list(selected), missing_ok=True):
          if (
            archive.data["items"][key]["include_submissions"]
            != options.include_submissions
          ):
            archive.item(key, {
              "include_submissions": options.include_submissions,
            }, None)

        processor = Processor(
          archive,
          force=options.force,
          jobs=options.jobs,
          timeout=options.job_timeout,
        )
        report = _retrieve(
          archive, processor, reader, home, options, stop
        )
        merged = merge_ctl(saved, isolated)

        try:
          state.save_cookies(merged)
        finally:
          merged.clear()

      finally:
        if saved is not None:
          saved.clear()
        if isolated is not None:
          isolated.clear()

    if report["status"] == "failed":
      print(
        "error: retrieval stopped: "
        + terminal(report.get("message") or "retrieval failed"),
        file=sys.stderr,
      )

    courses = select_courses(archive, list(selected), missing_ok=True)
    recorded = {
      archive.data["items"][key]["course_id"] for key in courses
    }
    missing = selected - recorded

    for course_id in sorted(missing):
      print(
        f"warning: {terminal(course_id)}: course was not recorded",
        file=sys.stderr,
      )

    _enqueue(archive, processor, archive.document_keys(courses))
    return _complete(
      archive,
      processor,
      courses,
      started,
      upstream=report,
      missing_courses=len(missing),
    )

  except BaseException as error:
    _abort(archive, processor, error)
    raise

  finally:
    if archive is not None:
      archive.close()


def run_process(options: argparse.Namespace) -> int:
  from .archive import Archive
  from .processing import Processor

  archive = processor = None
  started = time.monotonic()

  try:
    archive = Archive(options.archive, create=False)
    archive.prepare()

    if options.file:
      keys, courses = _file_selection(archive, options.file)
    else:
      courses = select_courses(archive, options.course)
      require(courses, "archive has no recorded courses")
      keys = archive.document_keys(courses)

    archive.begin("process")
    processor = Processor(
      archive,
      force=options.force,
      jobs=options.jobs,
      timeout=options.job_timeout,
    )
    _enqueue(archive, processor, keys)
    return _complete(archive, processor, courses, started)

  except BaseException as error:
    _abort(archive, processor, error)
    raise

  finally:
    if archive is not None:
      archive.close()


def run_export(options: argparse.Namespace) -> int:
  from .archive import Archive
  from .exports import export_courses

  archive = Archive(options.archive, create=False)

  try:
    archive.prepare_export()
    courses = select_courses(archive, options.course)
    require(courses, "archive has no recorded courses")
    code = export_courses(archive, courses)
    require(code in {0, 2, 3}, "invalid export completion code")
    return code

  finally:
    archive.close()
