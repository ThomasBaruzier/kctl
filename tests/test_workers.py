import os
import signal
import time

from pathlib import Path

import pytest

from kctl.core import Failure, StorageFailure, token
from kctl.workers import Workers


def protocol_worker(connection):
  try:
    while True:
      raw = connection.recv_bytes(16384)
      if not raw:
        return

      root = Path(os.fsdecode(raw)).parents[2]
      mode = root.name

      if mode == "exit":
        return
      if mode == "bad":
        connection.send_bytes(b"too-long")
        return
      if mode == "storage":
        connection.send_bytes(b"S")
        continue
      if mode == "wait":
        time.sleep(60)
        continue

      connection.send_bytes(b"1")

  except (EOFError, BrokenPipeError):
    pass

  finally:
    connection.close()


@pytest.fixture
def pool():
  value = Workers(1, 30, target=protocol_worker)

  try:
    yield value
  finally:
    value.close()


def spec(tmp_path, mode="normal"):
  return {
    "key": token(),
    "root": str(tmp_path / mode),
    "work": ".ctl/work/" + token(),
  }


def results(pool, count=1):
  deadline = time.monotonic() + 15
  collected = []

  while len(collected) < count:
    collected.extend(pool.collect())

    if len(collected) >= count:
      return collected

    if time.monotonic() >= deadline:
      pytest.fail("worker did not complete within the test deadline")

    time.sleep(0.01)

  return collected


@pytest.mark.parametrize("count", [0, -1, 33, True])
def test_worker_count_is_bounded(count):
  with pytest.raises(Failure, match="worker count"):
    Workers(count, 30, target=protocol_worker)


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_worker_timeout_must_be_positive(timeout):
  with pytest.raises(Failure, match="timeout"):
    Workers(1, timeout, target=protocol_worker)


def test_worker_target_is_required_to_be_callable():
  with pytest.raises(Failure, match="callable"):
    Workers(1, 30, target=None)


def test_pool_is_lazy_and_uses_spawn(pool):
  assert pool.context.get_start_method() == "spawn"
  assert pool.workers == []
  assert pool.running() == 0
  assert pool.capacity()


def test_successful_job_returns_the_original_specification(pool, tmp_path):
  job = spec(tmp_path)
  pool.submit(job)

  assert pool.running() == 1
  assert not pool.capacity()
  assert results(pool) == [(job, None)]
  assert pool.running() == 0
  assert pool.capacity()
  assert pool.workers[0].completed == 1


def test_idle_worker_is_reused_for_the_next_job(pool, tmp_path):
  first = spec(tmp_path)
  second = spec(tmp_path)
  pool.submit(first)
  assert results(pool) == [(first, None)]
  pid = pool.workers[0].process.pid

  pool.submit(second)

  assert results(pool) == [(second, None)]
  assert pool.workers[0].process.pid == pid
  assert pool.workers[0].completed == 2


def test_capacity_limit_does_not_replace_an_active_job(pool, tmp_path):
  first = spec(tmp_path, "wait")
  pool.submit(first)

  with pytest.raises(Failure, match="capacity"):
    pool.submit(spec(tmp_path))

  assert pool.running() == 1
  assert pool.workers[0].spec is first


def test_two_workers_can_complete_independent_jobs(tmp_path):
  pool = Workers(2, 30, target=protocol_worker)
  jobs = [spec(tmp_path), spec(tmp_path)]

  try:
    for job in jobs:
      pool.submit(job)

    assert pool.running() == 2
    completed = results(pool, count=2)

    assert {item[0]["key"] for item in completed} == {
      job["key"] for job in jobs
    }
    assert all(error is None for _, error in completed)
    assert pool.running() == 0

  finally:
    pool.close()


@pytest.mark.parametrize("mode", ["exit", "bad"])
def test_unexpected_worker_exit_or_message_becomes_a_job_error(
  pool, tmp_path, mode
):
  job = spec(tmp_path, mode)
  pool.submit(job)
  completed = results(pool)

  assert completed[0][0] is job
  assert completed[0][1] is not None
  assert "worker exited" in completed[0][1]
  assert pool.workers == []
  assert pool.capacity()


def test_storage_failure_marker_remains_fatal(pool, tmp_path):
  job = spec(tmp_path, "storage")
  pool.submit(job)

  with pytest.raises(StorageFailure, match="could not write result"):
    results(pool)

  assert pool.running() == 1
  assert pool.close() == [job]
  assert pool.workers == []


def test_timeout_terminates_worker_and_returns_retryable_job_error(pool, tmp_path):
  job = spec(tmp_path, "wait")
  pool.submit(job)
  pool.workers[0].started = time.monotonic() - pool.timeout - 1

  completed = list(pool.collect())

  assert completed == [
    (job, f"processing timed out after {pool.timeout}s"),
  ]
  assert pool.workers == []
  assert pool.running() == 0


def test_workers_are_recycled_after_the_completion_limit(pool, tmp_path):
  job = spec(tmp_path)
  pool.submit(job)
  pool.workers[0].completed = 31

  assert results(pool) == [(job, None)]
  assert pool.workers == []


def test_close_reports_abandoned_jobs_and_is_idempotent(pool, tmp_path):
  job = spec(tmp_path, "wait")
  pool.submit(job)

  assert pool.close() == [job]
  assert pool.workers == []
  assert pool.close() == []


def test_close_gracefully_retires_idle_workers(pool, tmp_path):
  job = spec(tmp_path)
  pool.submit(job)
  assert results(pool) == [(job, None)]

  assert pool.close() == []
  assert pool.workers == []


def test_submission_path_limit_is_checked_before_starting_a_worker(pool):
  job = {
    "root": "/" + "x" * 17000,
    "work": ".ctl/work/" + token(),
  }

  with pytest.raises(Failure, match="path too long"):
    pool.submit(job)

  assert pool.workers == []


def test_worker_startup_restores_parent_interrupt_mask(pool, tmp_path):
  before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
  job = spec(tmp_path)
  pool.submit(job)
  after = signal.pthread_sigmask(signal.SIG_BLOCK, set())

  assert after == before
  assert results(pool) == [(job, None)]
