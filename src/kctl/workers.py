from __future__ import annotations

import multiprocessing
import os
import signal
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .core import StorageFailure, require


@dataclass
class Worker:
  process: Any
  connection: Any
  spec: dict[str, Any] | None = None
  started: float = 0.0
  completed: int = 0


class Workers:
  def __init__(
    self,
    count: int,
    timeout: int,
    *,
    target: Callable[[Any], None],
  ):
    require(
      type(count) is int and 1 <= count <= 32,
      "worker count must be between 1 and 32",
    )
    require(
      type(timeout) is int and timeout > 0,
      "worker timeout must be positive",
    )
    require(callable(target), "worker target must be callable")

    self.count = count
    self.timeout = timeout
    self.target = target
    self.context = multiprocessing.get_context("spawn")
    self.workers: list[Worker] = []

  def capacity(self) -> bool:
    return (
      len(self.workers) < self.count
      or any(worker.spec is None for worker in self.workers)
    )

  def running(self) -> int:
    return sum(worker.spec is not None for worker in self.workers)

  def create(self) -> Worker:
    require(len(self.workers) < self.count, "worker capacity exhausted")
    parent, child = self.context.Pipe()
    process = self.context.Process(
      target=self.target, args=(child,)
    )
    previous = signal.pthread_sigmask(
      signal.SIG_BLOCK, {signal.SIGINT}
    )

    try:
      try:
        process.start()
      except BaseException:
        parent.close()
        if process.pid is not None:
          if process.is_alive():
            process.terminate()
          process.join()
        process.close()
        raise

      worker = Worker(process, parent)
      self.workers.append(worker)
      return worker

    finally:
      child.close()
      signal.pthread_sigmask(signal.SIG_SETMASK, previous)

  def submit(self, spec: dict[str, Any]) -> None:
    path = os.fsencode(str(Path(spec["root"]) / spec["work"]))
    require(len(path) <= 16384, "worker staging path too long")

    worker = next(
      (worker for worker in self.workers if worker.spec is None),
      None,
    )
    if worker is None:
      worker = self.create()

    worker.spec = spec
    worker.started = time.monotonic()

    try:
      worker.connection.send_bytes(path)
    except (OSError, EOFError):
      raise StorageFailure("could not submit worker job") from None

  def retire(self, worker: Worker, force: bool) -> None:
    if not force and worker.process.is_alive():
      try:
        worker.connection.send_bytes(b"")
      except (OSError, EOFError):
        pass
      worker.process.join(timeout=0.5)

    if worker.process.is_alive():
      worker.process.terminate()
      worker.process.join(timeout=0.5)

    if worker.process.is_alive():
      worker.process.kill()

    worker.process.join()
    worker.connection.close()
    worker.process.close()
    self.workers.remove(worker)

  @staticmethod
  def _readable(worker: Worker) -> bool:
    try:
      return worker.connection.poll()
    except (OSError, EOFError):
      return True

  def collect(self) -> Iterator[tuple[dict[str, Any], str | None]]:
    for worker in list(self.workers):
      if self._readable(worker):
        if worker.spec is None:
          self.retire(worker, force=True)
          continue

        spec = worker.spec
        try:
          message = worker.connection.recv_bytes(1)
        except (OSError, EOFError):
          message = b""

        if message == b"S":
          raise StorageFailure("worker could not write result")

        if message != b"1":
          self.retire(worker, force=True)
          yield spec, "worker exited without completion record"
          continue

        worker.spec = None
        worker.completed += 1
        yield spec, None

        if worker.completed >= 32:
          self.retire(worker, force=False)

      elif not worker.process.is_alive():
        spec = worker.spec
        self.retire(worker, force=True)

        if spec is not None:
          yield spec, "worker exited unexpectedly"

      elif (
        worker.spec is not None
        and time.monotonic() - worker.started > self.timeout
      ):
        spec = worker.spec
        self.retire(worker, force=True)
        yield spec, f"processing timed out after {self.timeout}s"

  def close(self) -> list[dict[str, Any]]:
    abandoned = [
      worker.spec
      for worker in self.workers
      if worker.spec is not None
    ]
    first_error = None

    for worker in list(self.workers):
      try:
        self.retire(worker, force=worker.spec is not None)
      except BaseException as error:
        if first_error is None:
          first_error = error

    if first_error is not None:
      raise first_error

    return abandoned
