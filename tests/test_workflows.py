import copy
import http.cookiejar
import sys
import threading

from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import kctl
import kctl.workflows as workflows

from kctl.cli import arguments
from kctl.core import Cancelled, Failure, StorageFailure
from kctl.reader import Home
from kctl.workflows import run_export, run_process, run_sync, select_courses


COMMENT_WARNING = (
  "displayed posts and comments retained; comment completeness is not verified"
)


def event(kind, **data):
  return SimpleNamespace(kind=kind, data=data)


def inventory():
  return {
    "account": "student-1",
    "run": None,
    "items": {
      "course/one": {
        "course_id": "one",
        "title": "First",
        "folder": "documents/First",
        "include_submissions": True,
      },
      "course/two": {
        "course_id": "two",
        "title": "Second",
        "folder": "documents/Second",
        "include_submissions": True,
      },
    },
    "documents": {
      "current-one": {
        "folder": "documents/First/Current",
        "original": "original.pdf",
        "bytes": 20,
        "role": "attachment",
      },
      "submission-one": {
        "folder": "documents/First/Submission",
        "original": "submission.pdf",
        "bytes": 10,
        "role": "submission",
      },
      "historical-one": {
        "folder": "documents/First/Old",
        "original": "old.pdf",
        "bytes": 30,
        "role": "attachment",
      },
      "current-two": {
        "folder": "documents/Second/Current",
        "original": "source.py",
        "bytes": 5,
        "role": "attachment",
      },
    },
  }


@pytest.fixture
def stages(monkeypatch, private_dir):
  state = SimpleNamespace(
    data=inventory(),
    calls=[],
    archives=[],
    processors=[],
    jars=[],
    producer_events=[
      event(
        "done",
        status="complete",
        code=0,
        downloaded=0,
        revalidated=0,
        reused=0,
      ),
    ],
    enrolled={"one": "First", "two": "Second"},
    account="student-1",
    home_account="student-1",
    home_page=object(),
    home_error=None,
    auth_error=None,
    tick_error=None,
    export_error=None,
    export_code=0,
    incomplete=set(),
    missing=set(),
    damaged={},
    failed_roots=0,
    limited_roots=0,
    pending=False,
    producer_reader=None,
    producer_snapshot=None,
    producer_finished=threading.Event(),
  )

  class State:
    def __init__(self, path):
      self.path = path

    def __enter__(self):
      state.calls.append(("state-enter", self.path))
      return self

    def __exit__(self, *args):
      state.calls.append(("state-close",))

    def account(self):
      return state.account

    def save_cookies(self, jar):
      state.calls.append(("save-cookies",))

  def authenticate(saved_state, interactive, quiet):
    state.calls.append(("authenticate", interactive, quiet))
    if state.auth_error is not None:
      raise state.auth_error
    jar = http.cookiejar.CookieJar()
    state.jars.append(jar)
    return jar, state.home_page

  def ctl_jar(saved):
    jar = http.cookiejar.CookieJar()
    state.jars.append(jar)
    return jar

  def merge_ctl(saved, isolated):
    jar = http.cookiejar.CookieJar()
    state.jars.append(jar)
    return jar

  class Reader:
    def __init__(self, jar, stop):
      self.jar = jar
      self.stop = stop

    def home(self, page):
      assert page is state.home_page
      state.calls.append(("home",))
      if state.home_error is not None:
        raise state.home_error
      return Home(page, state.home_account, dict(state.enrolled))

  class Archive:
    def __init__(self, root, create):
      state.calls.append(("archive-open", create))
      self.root = root
      self.data = copy.deepcopy(state.data)
      self.active_run = False
      self.prepared = False
      self.missing = set(state.missing)
      self.damaged = dict(state.damaged)
      self.current = {
        "course/one": ["current-one", "submission-one"],
        "course/two": ["current-two"],
      }
      state.archives.append(self)

    def prepare(self):
      state.calls.append(("prepare",))
      self.prepared = True

    def prepare_export(self):
      state.calls.append(("prepare-export",))

    def account(self, account):
      state.calls.append(("bind-account", account))
      self.data["account"] = account

    def begin(self, command):
      state.calls.append(("begin", command))
      self.active_run = True
      self.data["run"] = {"command": command, "status": "running"}

    def document(self, key, record, work):
      state.calls.append(("document", key, work))
      self.data["documents"][key] = copy.deepcopy(record)

    def item(self, key, changes, body):
      state.calls.append(("item", key))
      self.data["items"].setdefault(key, {}).update(changes)

    def document_keys(self, courses):
      keys = []
      for course in courses:
        include = self.data["items"][course]["include_submissions"]
        keys.extend(
          key for key in self.current[course]
          if include or self.data["documents"][key]["role"] != "submission"
        )
      return keys

    def course_for_document(self, key):
      if key == "current-two":
        return "course/two"
      return "course/one"

    def checkpoint(self):
      state.calls.append(("checkpoint",))

    def flush(self):
      pass

    def flush_views(self):
      state.calls.append(("views",))

    def scan_missing(self):
      state.calls.append(("scan-missing",))

    def incomplete(self):
      return state.incomplete

    def cleanup_work(self):
      state.calls.append(("cleanup-work",))

    def exists(self, path):
      return state.pending and path == ".ctl/pending.json"

    def finish(self, status, **details):
      state.calls.append(("finish", status))
      self.data["run"].update(status=status, **details)
      self.active_run = False

    def close(self):
      state.calls.append(("archive-close",))

  class Processor:
    def __init__(self, archive, *, force, jobs, timeout):
      self.archive = archive
      self.queued = []
      self.seen = set()
      self.completed = 0
      state.calls.append(("processor", force, jobs, timeout))
      state.processors.append(self)

    def enqueue(self, key):
      if key not in self.seen:
        self.seen.add(key)
        self.queued.append(key)
        state.calls.append(("enqueue", key))

    def busy(self):
      return bool(self.queued)

    def tick(self):
      if state.tick_error is not None:
        raise state.tick_error
      if self.queued:
        key = self.queued.pop(0)
        self.completed += 1
        state.calls.append(("processed", key))

    def close(self, message=None):
      state.calls.append(("processor-close", message))
      self.queued.clear()

    def progress(self):
      state.calls.append(("progress",))

    def summary(self):
      return {
        "processed_roots": self.completed,
        "reused_roots": 0,
        "failed_roots": state.failed_roots,
        "limited_roots": state.limited_roots,
        "worker_seconds": 0,
      }

  class Producer:
    def __init__(
      self, *, reader, root, snapshot, options, events, stop, home
    ):
      assert home.page is state.home_page
      assert snapshot is not state.archives[-1].data
      self.events = events
      self.stop = stop
      state.producer_reader = reader
      state.producer_snapshot = snapshot
      state.calls.append(("producer", options.include_submissions))

    def run(self):
      try:
        for item in state.producer_events:
          if self.stop.is_set():
            return
          self.events.put(copy.deepcopy(item))
      finally:
        state.producer_finished.set()

  def export_courses(archive, courses):
    state.calls.append(("export", tuple(courses)))
    if state.export_error is not None:
      raise state.export_error
    return state.export_code

  modules = {
    "archive": {"Archive": Archive},
    "processing": {"Processor": Processor},
    "retrieval": {"Producer": Producer},
    "exports": {"export_courses": export_courses},
  }
  for name, members in modules.items():
    module = ModuleType("kctl." + name)
    module.__dict__.update(members)
    monkeypatch.setitem(sys.modules, "kctl." + name, module)
    monkeypatch.setattr(kctl, name, module, raising=False)

  monkeypatch.setattr(workflows, "State", State)
  monkeypatch.setattr(workflows, "authenticate", authenticate)
  monkeypatch.setattr(workflows, "ctl_jar", ctl_jar)
  monkeypatch.setattr(workflows, "merge_ctl", merge_ctl)
  monkeypatch.setattr(workflows, "Reader", Reader)
  monkeypatch.setattr(workflows, "state_directory", lambda: private_dir / "state")

  return state


def test_default_sync_runs_all_stages_once(stages):
  assert run_sync(arguments([])) == 0

  calls = stages.calls
  assert calls.count(("authenticate", True, True)) == 1
  assert calls.count(("home",)) == 1
  assert calls.count(("begin", "sync")) == 1
  assert calls.count(("producer", True)) == 1
  assert ("export", ("course/one", "course/two")) in calls
  assert ("finish", "complete") in calls
  assert stages.producer_finished.is_set()
  assert stages.producer_reader.stop.is_set()
  assert all(len(jar) == 0 for jar in stages.jars)

  selected = {
    call[1] for call in calls if call[0] == "enqueue"
  }
  assert selected == {"current-one", "submission-one", "current-two"}
  assert "historical-one" not in selected


def test_authentication_state_is_released_before_final_processing_and_export(stages):
  assert run_sync(arguments([])) == 0

  calls = stages.calls
  close = calls.index(("state-close",))
  assert close < calls.index(("processed", "current-one"))
  assert close < calls.index(("export", ("course/one", "course/two")))


def test_archive_account_mismatch_prevents_preparation_or_run_creation(stages):
  stages.data["account"] = "other-student"

  with pytest.raises(Failure, match="another account"):
    run_sync(arguments([]))

  assert ("prepare",) not in stages.calls
  assert not any(call[0] == "begin" for call in stages.calls)
  assert not any(call[0] == "producer" for call in stages.calls)
  assert not any(call[0] == "export" for call in stages.calls)
  assert ("archive-close",) in stages.calls


def test_account_change_during_bootstrap_prevents_archive_opening(stages):
  stages.home_account = "other-student"

  with pytest.raises(Failure, match="account changed"):
    run_sync(arguments([]))

  assert not any(call[0] == "archive-open" for call in stages.calls)
  assert ("state-close",) in stages.calls


def test_supplied_homepage_failure_does_not_refetch_or_reauthenticate(stages):
  stages.home_error = Failure("returned homepage is not a learner page")

  with pytest.raises(Failure, match="learner page"):
    run_sync(arguments([]))

  assert stages.calls.count(("authenticate", True, True)) == 1
  assert stages.calls.count(("home",)) == 1
  assert not any(call[0] == "archive-open" for call in stages.calls)
  assert not any(call[0] == "producer" for call in stages.calls)
  assert ("state-close",) in stages.calls
  assert all(len(jar) == 0 for jar in stages.jars)


def test_unknown_enrolled_course_prevents_archive_changes(stages):
  with pytest.raises(Failure, match="not enrolled"):
    run_sync(arguments(["--course", "missing"]))

  assert not any(call[0] == "archive-open" for call in stages.calls)


def test_authentication_failure_does_not_construct_archive_or_reader(stages):
  stages.auth_error = Failure("not signed in", 1)

  with pytest.raises(Failure) as captured:
    run_sync(arguments([]))

  assert captured.value.code == 1
  assert ("home",) not in stages.calls
  assert not any(call[0] == "archive-open" for call in stages.calls)
  assert ("state-close",) in stages.calls


def test_selected_sync_only_processes_and_exports_that_course(stages):
  assert run_sync(arguments(["--course", "two"])) == 0

  assert ("enqueue", "current-two") in stages.calls
  assert ("enqueue", "current-one") not in stages.calls
  assert ("enqueue", "submission-one") not in stages.calls
  assert ("export", ("course/two",)) in stages.calls


def test_sync_passes_processing_and_submission_options(stages):
  assert run_sync(arguments([
    "--course", "one",
    "--no-submissions",
    "--force",
    "--jobs", "2",
    "--job-timeout", "100",
  ])) == 0

  assert ("producer", False) in stages.calls
  assert ("processor", True, 2, 100) in stages.calls
  assert ("enqueue", "submission-one") not in stages.calls
  assert ("export", ("course/one",)) in stages.calls
  snapshot = stages.producer_snapshot
  assert snapshot["items"]["course/one"]["include_submissions"] is False
  assert snapshot["items"]["course/two"]["include_submissions"] is True


def test_submission_policy_is_applied_before_early_upstream_failure(stages):
  stages.producer_events = [
    event(
      "done",
      status="failed",
      code=1,
      message="session expired",
      fatal_local=False,
    ),
  ]

  assert run_sync(arguments(["--course", "one", "--no-submissions"])) == 1

  assert stages.producer_snapshot["items"]["course/one"][
    "include_submissions"
  ] is False
  assert ("enqueue", "submission-one") not in stages.calls
  assert ("export", ("course/one",)) in stages.calls
  assert stages.archives[0].data["items"]["course/two"][
    "include_submissions"
  ] is True


def test_document_events_publish_before_processing_is_queued(stages):
  record = {
    "folder": "documents/First/New",
    "original": "new.pdf",
    "bytes": 40,
    "role": "attachment",
  }
  stages.producer_events.insert(0, event(
    "document", key="new-document", record=record, work=".ctl/work/example"
  ))

  assert run_sync(arguments(["--course", "one"])) == 0

  published = stages.calls.index((
    "document", "new-document", ".ctl/work/example",
  ))
  queued = stages.calls.index(("enqueue", "new-document"))
  assert published < queued


def test_ordinary_retrieval_failure_finishes_local_work_and_exports(stages):
  stages.producer_events = [
    event(
      "done",
      status="failed",
      code=2,
      message="connection failed",
      fatal_local=False,
    ),
  ]

  assert run_sync(arguments(["--course", "one"])) == 2
  assert ("processed", "current-one") in stages.calls
  assert ("export", ("course/one",)) in stages.calls
  assert ("finish", "failed") in stages.calls


def test_midrun_session_expiry_does_not_restart_authentication(stages):
  stages.producer_events = [
    event(
      "done",
      status="failed",
      code=1,
      message="session expired",
      fatal_local=False,
    ),
  ]

  assert run_sync(arguments([])) == 1
  assert stages.calls.count(("authenticate", True, True)) == 1
  assert stages.calls.count(("home",)) == 1
  assert any(call[0] == "export" for call in stages.calls)


def test_fatal_retrieval_storage_failure_does_not_export(stages):
  stages.producer_events = [
    event(
      "done",
      status="failed",
      code=2,
      message="unsafe managed file",
      fatal_local=True,
    ),
  ]

  with pytest.raises(StorageFailure, match="unsafe"):
    run_sync(arguments([]))

  assert not any(call[0] == "export" for call in stages.calls)
  assert ("finish", "failed") in stages.calls
  assert stages.producer_finished.is_set()


def test_missing_retrieval_completion_is_a_fatal_failure(stages):
  stages.producer_events = []

  with pytest.raises(Failure, match="without completion"):
    run_sync(arguments([]))

  assert not any(call[0] == "export" for call in stages.calls)
  assert ("finish", "failed") in stages.calls


def test_unknown_retrieval_events_fail_instead_of_being_ignored(stages):
  stages.producer_events = [event("unknown")]

  with pytest.raises(Failure, match="unknown retrieval event"):
    run_sync(arguments([]))

  assert not any(call[0] == "export" for call in stages.calls)


def test_missing_course_after_partial_retrieval_is_reported_without_losing_others(
  stages, capsys
):
  del stages.data["items"]["course/two"]

  assert run_sync(arguments([])) == 3
  assert ("export", ("course/one",)) in stages.calls
  assert "two: course was not recorded" in capsys.readouterr().err
  assert stages.archives[0].data["run"]["unrecorded_courses"] == 1


def test_local_processing_never_authenticates_or_fetches_home(stages):
  assert run_process(arguments(["process"])) == 0

  assert not any(call[0] == "state-enter" for call in stages.calls)
  assert not any(call[0] == "authenticate" for call in stages.calls)
  assert ("home",) not in stages.calls
  assert ("begin", "process") in stages.calls
  assert ("export", ("course/one", "course/two")) in stages.calls


def test_local_processing_follows_current_submission_policy(stages):
  stages.data["items"]["course/one"]["include_submissions"] = False

  assert run_process(arguments(["process", "--course", "one"])) == 0

  assert ("enqueue", "current-one") in stages.calls
  assert ("enqueue", "submission-one") not in stages.calls
  assert ("enqueue", "historical-one") not in stages.calls


def test_explicit_historical_file_is_processed_without_retrieval_or_reattachment(
  stages
):
  path = "documents/First/Old/old.pdf"
  assert run_process(arguments(["process", "--file", path, "--force"])) == 0

  enqueued = [call for call in stages.calls if call[0] == "enqueue"]
  assert enqueued == [("enqueue", "historical-one")]
  assert ("export", ("course/one",)) in stages.calls
  assert not any(call[0] in {"document", "item", "producer"} for call in stages.calls)
  assert stages.archives[0].current["course/one"] == [
    "current-one", "submission-one",
  ]


def test_invalid_file_selection_does_not_start_processing(stages):
  with pytest.raises(Failure, match="archive-relative"):
    run_process(arguments(["process", "--file", "../outside.pdf"]))

  assert not any(call[0] == "begin" for call in stages.calls)
  assert not any(call[0] == "processor" for call in stages.calls)
  assert not any(call[0] == "export" for call in stages.calls)


def test_unknown_archived_course_does_not_start_processing(stages):
  with pytest.raises(Failure, match="not archived"):
    run_process(arguments(["process", "--course", "missing"]))

  assert not any(call[0] == "begin" for call in stages.calls)


def test_processing_orders_larger_originals_first(stages):
  assert run_process(arguments(["process"])) == 0

  enqueued = [call[1] for call in stages.calls if call[0] == "enqueue"]
  assert enqueued == ["current-one", "submission-one", "current-two"]


@pytest.mark.parametrize("field", ["failed_roots", "limited_roots"])
def test_processing_limitations_produce_partial_status(stages, field):
  setattr(stages, field, 1)

  assert run_process(arguments(["process", "--course", "one"])) == 3
  assert ("finish", "partial") in stages.calls
  assert ("export", ("course/one",)) in stages.calls


def test_unselected_incomplete_course_does_not_change_selected_run_status(stages):
  stages.incomplete = {"course/two"}

  assert run_process(arguments(["process", "--course", "one"])) == 0
  assert stages.archives[0].data["run"]["incomplete_courses"] == 0


def test_selected_incomplete_course_makes_local_run_partial(stages, capsys):
  stages.incomplete = {"course/one"}

  assert run_process(arguments(["process", "--course", "one"])) == 3
  assert stages.archives[0].data["run"]["incomplete_courses"] == 1
  assert "First: incomplete; see extraction notes" in capsys.readouterr().out


@pytest.mark.parametrize("error", [KeyboardInterrupt(), Cancelled()])
def test_processing_interruption_stops_workers_without_starting_export(
  stages, error
):
  stages.tick_error = error

  with pytest.raises(type(error)):
    run_process(arguments(["process"]))

  assert not any(call[0] == "export" for call in stages.calls)
  assert ("finish", "interrupted") in stages.calls
  assert (
    "processor-close", "interrupted before publication"
  ) in stages.calls
  assert ("archive-close",) in stages.calls


def test_retrieval_interruption_joins_thread_before_closing_state(stages):
  stages.tick_error = KeyboardInterrupt()

  with pytest.raises(KeyboardInterrupt):
    run_sync(arguments([]))

  assert stages.producer_finished.is_set()
  assert stages.producer_reader.stop.is_set()
  assert ("state-close",) in stages.calls
  assert not any(call[0] == "export" for call in stages.calls)
  assert ("finish", "interrupted") in stages.calls


def test_pending_publication_is_not_finalized_during_abort(stages, capsys):
  stages.pending = True
  stages.tick_error = StorageFailure("publication failed")

  with pytest.raises(StorageFailure):
    run_process(arguments(["process"]))

  assert not any(call[0] == "finish" for call in stages.calls)
  assert "recovery on next run" in capsys.readouterr().err
  assert ("archive-close",) in stages.calls


def test_export_only_does_not_replace_processing_run_or_regenerate_views(stages):
  stages.data["run"] = {"command": "sync", "status": "partial"}
  original = copy.deepcopy(stages.data["run"])

  assert run_export(arguments(["export", "--course", "two"])) == 0

  assert ("prepare-export",) in stages.calls
  assert ("prepare",) not in stages.calls
  assert ("views",) not in stages.calls
  assert not any(call[0] in {"begin", "finish", "processor"} for call in stages.calls)
  assert not any(call[0] == "authenticate" for call in stages.calls)
  assert ("export", ("course/two",)) in stages.calls
  assert stages.archives[0].data["run"] == original


@pytest.mark.parametrize("code", [0, 2, 3])
def test_export_exit_status_is_preserved(stages, code):
  stages.export_code = code

  assert run_export(arguments(["export"])) == code
  assert ("archive-close",) in stages.calls


def test_export_integrity_failure_overrides_an_ordinary_partial_run(stages):
  stages.failed_roots = 1
  stages.export_code = 2

  assert run_process(arguments(["process"])) == 2
  assert ("finish", "failed") in stages.calls


def test_export_exception_does_not_claim_previous_file_was_preserved(
  stages, capsys
):
  stages.export_error = StorageFailure(
    "file replaced but directory synchronization failed"
  )

  with pytest.raises(StorageFailure, match="file replaced"):
    run_process(arguments(["process"]))

  output = capsys.readouterr()
  assert "previous export preserved" not in output.out + output.err
  assert ("finish", "failed") in stages.calls


def test_interrupted_export_is_not_restarted(stages):
  stages.export_error = KeyboardInterrupt()

  with pytest.raises(KeyboardInterrupt):
    run_process(arguments(["process"]))

  assert sum(call[0] == "export" for call in stages.calls) == 1
  assert ("finish", "interrupted") in stages.calls


def test_course_selection_is_deterministic_and_supports_missing_partial_roots():
  archive = SimpleNamespace(data=inventory())

  assert select_courses(archive) == ["course/one", "course/two"]
  assert select_courses(archive, ["two", "one", "one"]) == [
    "course/one", "course/two",
  ]
  assert select_courses(
    archive, ["missing", "one"], missing_ok=True
  ) == ["course/one"]

  with pytest.raises(Failure, match="not archived"):
    select_courses(archive, ["missing"])


def test_archive_without_recorded_courses_is_not_exported(stages):
  stages.data["items"] = {}

  with pytest.raises(Failure, match="no recorded courses"):
    run_export(arguments(["export"]))

  assert not any(call[0] == "export" for call in stages.calls)


def test_custom_archive_and_authentication_directory_are_forwarded(stages):
  configured = arguments([
    "--state-dir", "private-state", "sync", "semester",
    "--course", "one",
  ])

  assert run_sync(configured) == 0
  assert ("state-enter", Path("private-state")) in stages.calls
  assert stages.archives[0].root == Path("semester")


def add_problem_area(
  stages,
  name="area",
  *,
  course="course/one",
  problem=COMMENT_WARNING,
):
  key = course + "/" + name
  stages.data["items"][key] = {
    "title": name,
    "status": "partial",
    "parent": course,
    "problems": [problem],
    "related": [],
  }
  stages.data["items"][course].setdefault("children", []).append(key)
  stages.incomplete.update({course, key})
  return key


@pytest.mark.parametrize("count", [1, 2])
def test_comment_summary_counts_unique_areas_without_changing_completion(
  stages, capsys, count
):
  for number in range(count):
    key = add_problem_area(stages, f"area-{number}")
    stages.data["items"][key]["problems"].append(COMMENT_WARNING)
    stages.data["items"][key]["message"] = COMMENT_WARNING
    stages.data["items"]["course/one"].setdefault("related", []).append(key)

  assert run_process(arguments(["process", "--course", "one"])) == 3

  output = capsys.readouterr().out
  noun = "area" if count == 1 else "areas"
  assert output.count(
    f"First: comment completeness unverified ({count} {noun})"
  ) == 1
  assert "courses: 1 incomplete" in output
  assert "result: partial" in output
  archive = stages.archives[0]
  assert archive.data["run"]["incomplete_courses"] == 1
  assert archive.data["items"] == stages.data["items"]
  assert all(
    call[0] not in {"authenticate", "home", "producer", "item"}
    for call in stages.calls
  )


def test_summary_preserves_mixed_comment_capture_and_reference_failures(
  stages, capsys
):
  add_problem_area(stages, "comments")
  failed = add_problem_area(stages, "board", problem="board capture failed")
  stages.data["items"][failed].update(
    status="failed",
    message="board capture failed",
    problems=[],
    documents=["missing-document", "missing-document"],
  )

  assert run_process(arguments(["process", "--course", "one"])) == 3

  output = capsys.readouterr().out
  assert (
    "First: board capture failed (1 area); "
    "comment completeness unverified (1 area); "
    "referenced attachment record is missing (1 area)"
  ) in output
  assert "courses: 1 incomplete" in output
  assert stages.archives[0].data["run"]["incomplete_courses"] == 1


def test_summary_preserves_missing_damaged_and_unrecorded_page_reasons(
  stages, capsys
):
  missing = add_problem_area(stages, "missing")
  damaged = add_problem_area(stages, "damaged")
  for key in (missing, damaged):
    stages.data["items"][key].update(status="archived", problems=[])
  stages.missing.add(missing)
  stages.damaged[damaged] = "stored source content changed"
  course = stages.data["items"]["course/one"]
  course["children"].append("unrecorded")
  course["related"] = ["unrecorded", missing, damaged]

  assert run_process(arguments(["process", "--course", "one"])) == 3

  output = capsys.readouterr().out
  assert (
    "First: referenced CTL page is not recorded (1 area); "
    "stored page missing (1 area); stored source content changed (1 area)"
  ) in output
  assert "comment completeness" not in output
  assert stages.archives[0].data["run"]["incomplete_courses"] == 1


def test_summary_uses_only_current_references_and_is_cycle_safe(stages):
  key = add_problem_area(stages)
  stages.data["items"]["course/one"]["related"] = [key, key]
  stages.data["items"][key]["related"] = ["course/one", key]
  stages.data["items"]["historical"] = {
    "title": "Historical",
    "parent": "course/one",
    "status": "failed",
    "message": "unreachable old failure",
  }
  stages.missing.add("historical")
  stages.damaged["historical"] = "unreachable damaged page"
  archive = SimpleNamespace(
    data=stages.data,
    missing=stages.missing,
    damaged=stages.damaged,
  )
  before = copy.deepcopy((archive.data, archive.missing, archive.damaged))

  assert workflows._incomplete_reasons(archive, "course/one") == (
    "comment completeness unverified (1 area)"
  )
  assert (archive.data, archive.missing, archive.damaged) == before
  assert stages.calls == []


def test_summary_counts_each_courses_areas_separately(stages, capsys):
  add_problem_area(stages, "first", course="course/one")
  add_problem_area(stages, "second", course="course/two")
  add_problem_area(stages, "third", course="course/two")

  assert run_process(arguments(["process"])) == 3

  output = capsys.readouterr().out
  assert "First: comment completeness unverified (1 area)" in output
  assert "Second: comment completeness unverified (2 areas)" in output
  assert "courses: 2 incomplete" in output
  assert stages.archives[0].data["run"]["incomplete_courses"] == 2


def test_summary_does_not_report_an_unselected_courses_reasons(stages, capsys):
  add_problem_area(stages, course="course/two")

  assert run_process(arguments(["process", "--course", "one"])) == 0

  output = capsys.readouterr().out
  assert "courses: 0 incomplete" in output
  assert "Second:" not in output
  assert "comment completeness" not in output
  assert stages.archives[0].data["run"]["incomplete_courses"] == 0


def test_summary_bounds_distinct_reasons_without_changing_records(stages, capsys):
  for number in range(5):
    add_problem_area(stages, f"area-{number}", problem=f"Reason {number}")
  before = copy.deepcopy(stages.data["items"])

  assert run_process(arguments(["process", "--course", "one"])) == 3

  output = capsys.readouterr().out
  line = next(line for line in output.splitlines() if line.startswith("  First:"))
  assert line == (
    "  First: Reason 0 (1 area); Reason 1 (1 area); Reason 2 (1 area); "
    "2 other reasons; see extraction notes"
  )
  assert stages.archives[0].data["items"] == before
  assert stages.archives[0].data["run"]["incomplete_courses"] == 1


def test_summary_sanitizes_titles_and_reasons_at_the_terminal_boundary(
  stages, capsys
):
  key = add_problem_area(stages)
  stages.data["items"][key].update(
    status="failed",
    problems=[],
    message="failure\ndetails\x1b",
  )
  stages.data["items"]["course/one"]["title"] = "First\ninjected\x1b[31m"

  assert run_process(arguments(["process", "--course", "one"])) == 3

  output = capsys.readouterr().out
  assert "\x1b" not in output
  assert (
    "  First injected [31m: failure details  (1 area)"
    in output.splitlines()
  )
  assert "result: partial" in output
  assert stages.archives[0].data["items"][key]["message"] == "failure\ndetails\x1b"


def test_summary_does_not_change_completeness_semantics(stages, capsys):
  add_problem_area(stages)
  stages.incomplete.clear()

  assert run_process(arguments(["process", "--course", "one"])) == 0

  output = capsys.readouterr().out
  assert "courses: 0 incomplete" in output
  assert "First:" not in output
  assert stages.archives[0].data["run"]["incomplete_courses"] == 0


def test_export_failure_still_dominates_a_reported_comment_limitation(
  stages, capsys
):
  add_problem_area(stages)
  stages.export_code = 2

  assert run_process(arguments(["process", "--course", "one"])) == 2

  output = capsys.readouterr().out
  assert "First: comment completeness unverified (1 area)" in output
  assert "result: failed" in output
  assert stages.archives[0].data["run"]["status"] == "failed"
