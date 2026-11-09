from __future__ import annotations

import copy
import hashlib
import os
import re
import signal
import sys
import time
import warnings

from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive import source_content
from .content import Blob, ContentWalker, LimitedWriter, Output
from .conversion import Engines, FileProcessor
from .core import (
  CONTENT_LIMIT,
  FILE_LIMIT,
  IMAGE_LIMIT,
  JOB_LIMIT,
  REPORT_LIMIT,
  TEXT_LIMIT,
  BudgetReached,
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  WorkerResultFailure,
  display,
  explanation,
  join_path,
  json_bytes,
  label,
  link_to,
  now,
  parent_path,
  parse_json,
  require,
  terminal,
  token,
)
from .records import (
  STEPS,
  applicable,
  attempt_record,
  content_files,
  content_images,
  error_outcome,
  node_index,
  node_problems,
  outcome,
  product_title,
  ready,
  result_problems,
  step_findings,
  validate_document,
  validate_result_data,
  validate_signature,
)
from .storage import (
  READ_FLAGS,
  check_job_source,
  checked_directory,
  checked_file,
  checked_local,
  document_source,
  file_digest,
  read_private,
  remove_work,
  signature,
  staging_output,
  storage_io,
  verify_source,
  write_new,
)
from .workers import Workers

if TYPE_CHECKING:
  from .archive import Archive


def _validate_spec(spec: dict[str, Any]) -> None:
  require(
    isinstance(spec, dict)
    and isinstance(spec.get("root"), str)
    and Path(spec["root"]).is_absolute()
    and isinstance(spec.get("work"), str)
    and re.fullmatch(r"\.ctl/work/[0-9a-f]{32}", spec["work"])
    and isinstance(spec.get("steps"), list)
    and spec["steps"]
    and all(isinstance(step, str) and step in STEPS for step in spec["steps"])
    and len(spec["steps"]) == len(set(spec["steps"]))
    and isinstance(spec.get("retry_steps"), list)
    and all(step in spec["steps"] for step in spec["retry_steps"])
    and type(spec.get("force")) is bool
    and isinstance(spec.get("settings"), dict)
    and all(
      isinstance(spec["settings"].get(name), dict)
      for name in ("walk", "visuals", "convert", "ocr")
    ),
    "malformed processing job",
  )
  validate_document(spec.get("key"), spec.get("document"))
  validate_signature(spec.get("signature"), spec["document"]["bytes"])


def _copy_blob(
  blob: Blob,
  target: Path,
  header: bytes = b"",
  expected: str | None = None,
) -> None:
  before = signature(blob.path)
  require(before["size"] == blob.size, "processing dependency size changed")

  if target == blob.path:
    require(
      not header and blob.offset == 0,
      "staged dependency requires rewriting",
    )
    if expected is not None:
      require(file_digest(target) == expected, "staged content identity mismatch")
    require(signature(blob.path) == before, "processing dependency changed")
    return

  descriptor = os.open(blob.path, READ_FLAGS)
  hasher = hashlib.sha256()

  with os.fdopen(descriptor, "rb") as incoming:
    info = os.fstat(incoming.fileno())
    checked_file(info)
    require(
      info.st_size == blob.size
      and info.st_ino == before["inode"]
      and info.st_dev == before["device"],
      "processing dependency changed before reading",
    )
    incoming.seek(blob.offset)

    with staging_output(target) as stream:
      outgoing = LimitedWriter(stream, FILE_LIMIT)
      outgoing.write(header)
      remaining = blob.length

      while remaining:
        chunk = incoming.read(min(1024 * 1024, remaining))
        require(chunk, "processing dependency truncated")
        outgoing.write(chunk)
        hasher.update(chunk)
        remaining -= len(chunk)

      require(not incoming.read(1), "processing dependency grew")

  require(signature(blob.path) == before, "processing dependency changed")

  if expected is not None:
    require(hasher.hexdigest() == expected, "copied content identity mismatch")


def product_header(
  node: dict[str, Any],
  step: str,
  settings: dict[str, Any],
) -> bytes:
  from urllib.parse import quote

  product = node["products"][step]
  attempt = node["attempts"].get(step)
  lines = [
    "# " + label(display(node["name"], 160)),
    "",
    f"[Original]({quote(node['original'])})",
    "",
    "Generated: " + product["finished"],
    "",
  ]

  if step == "ocr":
    lines += ["OCR: eng, Tesseract fast. Text may contain errors.", ""]

  if attempt and attempt["retained"]:
    lines += [
      "> Retained result. The latest attempt did not replace this output.",
      "",
    ]

  if product["settings"] != settings[step]:
    lines += ["> Generated with older processing settings.", ""]

  problems = [
    f"{' / '.join(origins)}: {message}"
    for message, origins in step_findings(node, step).items()
  ]

  if problems:
    lines.extend(
      "> " + label(display(problem, 160)) for problem in problems[:2]
    )
    if len(problems) > 2:
      lines.append("> Additional limitations are recorded in the document index.")
    lines.append("")

  return ("\n".join(lines) + "\n").encode()


class DocumentJob:
  def __init__(self, spec: dict[str, Any], engines: Engines):
    _validate_spec(spec)
    self.spec = spec
    self.doc = spec["document"]
    self.settings = spec["settings"]
    self.engines = engines
    self.root = Path(spec["root"])
    self.old_root = self.root / self.doc["folder"]
    self.work = self.root / spec["work"]
    self.stage = self.work / "new"
    self.old = self.doc.get("result")
    self.retry_steps = set(spec["retry_steps"])
    self.nodes: dict[str, dict[str, Any]] = {}
    self.blobs: dict[str, Blob] = {}
    self.cached_visuals: dict[str, dict[str, Any]] = {}
    self.verified_assets: dict[str, dict[str, int]] = {}
    self.tasks: list[tuple[str, str]] = []

  def old_blob(
    self,
    relative: str,
    offset: int = 0,
    *,
    asset: bool = False,
  ) -> Blob | None:
    if self.old is None or relative not in self.old["files"]:
      return None

    try:
      path = checked_local(self.old_root, relative)
    except FileNotFoundError:
      return None

    size = self.old["files"][relative]
    current = signature(path)
    if current["size"] != size:
      return None

    if asset and self.verified_assets.get(relative) != current:
      if (
        file_digest(path) != Path(relative).stem
        or signature(path) != current
      ):
        return None
      self.verified_assets[relative] = current

    return Blob(path, size, offset)

  def extraction_reusable(self) -> bool:
    if (
      self.spec["force"]
      or self.retry_steps
      or not self.old
      or self.old["settings"] != self.settings["walk"]
      or self.old["nodes"][""]["name"] != display(self.doc["name"])
      or any(node["retry"] for node in self.old["nodes"].values())
    ):
      return False

    for folder, node in self.old["nodes"].items():
      if not folder:
        continue

      relative = join_path(folder, node["original"])
      blob = self.old_blob(relative)
      if blob is None:
        return False

      before = signature(blob.path)
      if (
        file_digest(blob.path) != node["sha256"]
        or signature(blob.path) != before
      ):
        return False

    return True

  def extract(self) -> None:
    with storage_io():
      if self.extraction_reusable():
        self.nodes = copy.deepcopy(self.old["nodes"])

        for folder, node in self.nodes.items():
          if folder:
            relative = join_path(folder, node["original"])
            blob = self.old_blob(relative)
            require(blob is not None, "extracted original disappeared")
            self.blobs[relative] = blob

          node["products"] = {}
          node["attempts"] = {}
          node["visuals"] = None

        return

    output = Output(self.stage)
    walker = ContentWalker(output)
    walker.node(
      "",
      self.doc["name"],
      self.doc["original"],
      self.doc["bytes"],
      self.doc["sha256"],
    )
    walker.walk("")
    self.nodes = walker.nodes
    self.blobs.update(output.files)

    if self.old is None:
      return

    for folder, previous in self.old["nodes"].items():
      node = self.nodes.get(folder)
      require(
        node is not None
        and all(
          node[name] == previous[name]
          for name in ("original", "sha256")
        )
        and (not folder or node["name"] == previous["name"]),
        "rediscovery omitted or changed previously extracted content; "
        "previous result retained",
      )

  def load_product(
    self,
    product: dict[str, Any],
    *,
    visual: bool = False,
  ) -> dict[str, Any] | None:
    found = {}
    markdown = None if visual else product["markdown"]

    for relative in product["outputs"]:
      offset = product["header_bytes"] if relative == markdown else 0
      blob = self.old_blob(
        relative, offset, asset=relative != markdown
      )
      if blob is None:
        return None

      if relative == markdown:
        before = signature(blob.path)
        try:
          source_content(read_private(blob.path, TEXT_LIMIT), product)
        except (FileNotFoundError, SourceProblem):
          return None
        if signature(blob.path) != before:
          return None

      found[relative] = blob

    self.blobs.update(found)
    return copy.deepcopy(product)

  def import_products(self) -> None:
    if self.old is not None:
      for folder, node in self.nodes.items():
        previous = self.old["nodes"].get(folder)
        if previous is None or any(
          node[name] != previous[name]
          for name in ("original", "sha256", "kind")
        ):
          continue

        node["attempts"] = copy.deepcopy(previous["attempts"])

        if previous.get("visuals") is not None:
          visuals = self.load_product(previous["visuals"], visual=True)
          if visuals is not None:
            self.cached_visuals[folder] = visuals

        for step, product in previous["products"].items():
          loaded = self.load_product(product)

          if loaded is None:
            missing = outcome(
              self.settings[step],
              problems=["previous output missing or damaged; rerun this operation"],
              retry=True,
              failed=True,
            )
            node["attempts"][step] = attempt_record(missing, retained=False)
          else:
            node["products"][step] = loaded

    for folder, node in self.nodes.items():
      for step in self.spec["steps"]:
        if not applicable(node["kind"], step):
          continue

        if (
          self.spec["force"]
          or step in self.retry_steps
          or not ready(node, step, self.settings[step])
        ):
          self.tasks.append((folder, step))

    self.check_budget(self.nodes, self.blobs)

  @staticmethod
  def check_budget(
    nodes: dict[str, Any], blobs: dict[str, Blob]
  ) -> None:
    paths = content_files(nodes)
    require(paths <= blobs.keys(), "processing dependency missing")

    if sum(blobs[path].length for path in paths) > CONTENT_LIMIT:
      raise BudgetReached("final content would exceed the root byte budget")

    if len(content_images(nodes)) > IMAGE_LIMIT:
      raise BudgetReached("final result would exceed the root image limit")

  def source(self, folder: str, node: dict[str, Any]) -> Path:
    if not folder:
      return self.stage / self.doc["original"]
    return self.blobs[join_path(folder, node["original"])].path

  def _without_product(
    self, folder: str, step: str
  ) -> dict[str, dict[str, Any]]:
    node = self.nodes[folder]
    products = {
      name: product
      for name, product in node["products"].items()
      if name != step
    }
    return {
      **self.nodes,
      folder: {**node, "products": products, "visuals": None},
    }

  def accept(
    self,
    folder: str,
    step: str,
    candidate: dict[str, Any],
    output: Output,
  ) -> bool:
    node = self.nodes[folder]
    previous = node["products"].get(step)
    retain = bool(
      candidate["status"] != "complete"
      and previous is not None
      and (
        previous["status"] == "complete"
        or previous["content_bytes"] > 0
        or previous["markdown"] and candidate["content_bytes"] == 0
      )
    )

    if retain:
      node["attempts"][step] = attempt_record(
        candidate, retained=True, previous=node["attempts"].get(step)
      )
      return False

    selected = candidate
    blobs = {
      path: output.resolve(path) for path in candidate["outputs"]
    }
    proposed = {
      **node,
      "visuals": None,
      "products": {**node["products"], step: selected},
    }

    try:
      self.check_budget(
        {**self.nodes, folder: proposed},
        {**self.blobs, **blobs},
      )
    except BudgetReached as error:
      coverage = candidate.get("summary", {}).get("coverage")
      candidate = error_outcome(self.settings[step], error)
      if coverage:
        candidate["summary"]["coverage"] = copy.deepcopy(coverage)

      if previous is not None:
        node["attempts"][step] = attempt_record(
          candidate, retained=True, previous=node["attempts"].get(step)
        )
        return False

      selected = candidate
      blobs = {}

    node["visuals"] = None
    node["products"][step] = selected
    node["attempts"][step] = attempt_record(candidate, retained=False)
    self.blobs.update(blobs)
    return selected is candidate

  def attach_visuals(self, folder: str) -> None:
    node = self.nodes[folder]
    visuals = self.cached_visuals.get(folder)
    node["visuals"] = None

    if visuals is None:
      return

    referenced = {
      path
      for product in node["products"].values()
      for path in product["outputs"]
    }
    if set(visuals["outputs"]) <= referenced:
      node["visuals"] = copy.deepcopy(visuals)

  def process(self) -> None:
    fresh_visuals = set()

    for folder, step in self.tasks:
      baseline = self._without_product(folder, step)
      protected = {
        path: self.blobs[path] for path in content_files(baseline)
      }
      candidate_root = self.work / ("candidate-" + token())

      with storage_io():
        candidate_root.mkdir(mode=0o700)

      output = Output(
        candidate_root,
        existing=self.blobs,
        protected=protected,
        protected_images=content_images(baseline),
      )
      processor = FileProcessor(
        self.source(folder, self.nodes[folder]),
        output,
        folder,
        self.nodes[folder],
        step,
        self.settings,
        self.engines,
        self.cached_visuals.get(folder),
        force_visuals=(
          self.spec["force"] or step in self.retry_steps
        ) and folder not in fresh_visuals,
      )
      candidate, visuals = processor.run()
      accepted = self.accept(folder, step, candidate, output)

      if accepted and visuals is not None:
        referenced = {
          path
          for product in self.nodes[folder]["products"].values()
          for path in product["outputs"]
        }
        if set(visuals["outputs"]) <= referenced:
          self.cached_visuals[folder] = visuals
          if visuals["status"] == "complete":
            fresh_visuals.add(folder)

      self.attach_visuals(folder)
      keep = {
        self.blobs[path].path for path in content_files(self.nodes)
      }
      output.cleanup(keep)

    for folder in self.nodes:
      self.attach_visuals(folder)

    required = content_files(self.nodes)
    self.blobs = {path: self.blobs[path] for path in required}
    keep = {blob.path for blob in self.blobs.values()}

    with storage_io():
      for directory in self.work.glob("candidate-*"):
        Output(directory).cleanup(keep)

  def links(
    self, folder: str, node: dict[str, Any], origin: str
  ) -> list[str]:
    original = join_path(folder, node["original"])
    links = [f"[original]({link_to(original, origin)})"]

    for step, title in (("convert", "text"), ("ocr", "OCR")):
      product = node["products"].get(step)
      if product and product["markdown"]:
        title = product_title(node, step, self.settings, title)
        links.append(f"[{title}]({link_to(product['markdown'], origin)})")

    return links

  def reports(self) -> dict[str, bytes]:
    from .core import CONTAINER_KINDS

    reports = {}

    for folder, node in self.nodes.items():
      if not node_index(node):
        continue

      path = join_path(folder, "index.md")
      lines = [
        "# " + label(display(node["name"], 160)),
        "",
        " | ".join(self.links(folder, node, path)),
        "",
      ]

      if node["children"]:
        lines += ["## Contents", ""]

        for child in node["children"]:
          item = self.nodes[child]
          title = label(display(item["name"], 160))

          if node_index(item):
            destination = join_path(child, "index.md")
            title = f"[{title}]({link_to(destination, path)})"

          lines.append(
            f"- {title}: " + " | ".join(self.links(child, item, path))
          )

          if item["kind"] == "unsupported":
            lines.append("  - Original only; unsupported file type.")

          if node_problems(item):
            lines.append("  - Processing limitations; see details.")

          ocr = item["products"].get("ocr")
          if ocr and ocr["status"] == "complete" and not ocr["content_bytes"]:
            lines.append("  - OCR completed; no text recognized.")

        lines.append("")

      elif node["kind"] in CONTAINER_KINDS and not node["problems"]:
        lines += ["No regular files.", ""]

      problems = node_problems(node)
      if problems:
        lines += ["## Limitations", ""]
        lines.extend(
          "- " + label(display(problem, 160))
          for problem in problems[:4]
        )
        if len(problems) > 4:
          lines.append(
            f"- {len(problems) - 4} additional diagnostics are retained internally."
          )
        lines.append("")

      if folder:
        parent = parent_path(parent_path(folder))
        destination = join_path(parent, "index.md")
        lines += [f"[Back]({link_to(destination, path)})", ""]

      reports[path] = ("\n".join(lines).rstrip("\n") + "\n").encode()

    return reports

  def materialize(self) -> dict[str, int]:
    self.check_budget(self.nodes, self.blobs)
    reports = self.reports()
    headers = {}
    products = {}
    expected_hashes = {}

    for folder, node in self.nodes.items():
      if folder:
        expected_hashes[join_path(folder, node["original"])] = node["sha256"]

      for step, product in node["products"].items():
        if product["markdown"] is None:
          continue

        header = product_header(node, step, self.settings)
        product["header_bytes"] = len(header)
        headers[product["markdown"]] = header
        products[product["markdown"]] = product

    for path in content_images(self.nodes):
      expected_hashes[path] = Path(path).stem

    require(
      sum(map(len, reports.values())) + sum(map(len, headers.values()))
      <= REPORT_LIMIT,
      "bounded report allowance exceeded",
    )
    files = {}

    for relative in sorted(content_files(self.nodes)):
      blob = self.blobs[relative]
      header = headers.get(relative, b"")
      size = blob.length + len(header)
      target = self.stage / relative

      if relative in headers:
        require(size <= TEXT_LIMIT, "rendered Markdown exceeds text limit")

      _copy_blob(blob, target, header, expected_hashes.get(relative))
      require(target.stat().st_size == size, "staged output size mismatch")

      if relative in products:
        source_content(read_private(target, TEXT_LIMIT), products[relative])

      files[relative] = size

    for relative, raw in reports.items():
      write_new(self.stage / relative, raw)
      files[relative] = len(raw)

    keep = {
      self.stage / path
      for path in set(files) | {self.doc["original"]}
    }
    Output(self.stage).cleanup(keep)
    return files

  def run(self) -> dict[str, Any]:
    with storage_io():
      checked_directory(self.root.lstat())
      checked_directory(self.work.lstat())
      checked_directory(self.stage.lstat())
      check_job_source(self.spec)

      source = document_source(self.root, self.doc)
      _copy_blob(
        Blob(source, self.doc["bytes"]),
        self.stage / self.doc["original"],
        expected=self.doc["sha256"],
      )
      check_job_source(self.spec)

    self.extract()

    with storage_io():
      self.import_products()

    self.process()

    with storage_io():
      files = self.materialize()
      check_job_source(self.spec)

    result = {
      "source": self.doc["sha256"],
      "settings": self.settings["walk"],
      "nodes": self.nodes,
      "files": files,
      "finished": now(),
    }
    validate_result_data(result, self.doc)
    return {"result": result, "error": None, "fatal": None}


def worker_main(connection: Any) -> None:
  engines = None

  try:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.umask(0o077)

    for name in (
      "OMP_THREAD_LIMIT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
      "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
    ):
      os.environ[name] = "1"

    os.environ.pop("KCTL_PASSWORD", None)
    engines = Engines()

    while True:
      message = connection.recv_bytes(16384)
      if not message:
        return

      directory = Path(os.fsdecode(message))
      started = time.monotonic()
      captured = []

      try:
        with warnings.catch_warnings(record=True) as captured:
          warnings.simplefilter("always")

          with storage_io():
            checked_directory(directory.lstat())
            raw = read_private(directory / "job.json", JOB_LIMIT)

          spec = parse_json(raw)
          _validate_spec(spec)
          require(
            directory == Path(spec["root"]) / spec["work"],
            "worker staging mismatch",
          )
          result = DocumentJob(spec, engines).run()

      except (ProtectionFailure, StorageFailure) as error:
        result = {
          "result": None,
          "error": None,
          "fatal": display(explanation(error)),
        }
      except Exception as error:
        result = {
          "result": None,
          "error": display(explanation(error)),
          "fatal": None,
        }

      result["notes"] = list(dict.fromkeys(
        f"{warning.category.__name__}: {display(warning.message, 1000)}"
        for warning in captured
      ))[:20]
      result["seconds"] = round(time.monotonic() - started, 3)
      raw = json_bytes(result)

      if len(raw) > JOB_LIMIT:
        raw = json_bytes({
          "result": None,
          "error": "worker result too large",
          "fatal": None,
          "notes": [],
          "seconds": result["seconds"],
        })

      try:
        write_new(directory / "result.json", raw)
      except (StorageFailure, ProtectionFailure, OSError):
        connection.send_bytes(b"S")
        return

      connection.send_bytes(b"1")

  except (EOFError, BrokenPipeError, KeyboardInterrupt):
    pass

  finally:
    try:
      if engines is not None:
        engines.close()
    finally:
      connection.close()


class Processor:
  def __init__(
    self,
    archive: Archive,
    *,
    force: bool,
    jobs: int,
    timeout: int,
  ):
    self.archive = archive
    self.steps = ["convert", "ocr"]
    self.force = force
    self.workers = Workers(jobs, timeout, target=worker_main)
    self.ready: deque[str] = deque()
    self.seen: set[str] = set()
    self.completed = 0
    self.reused = 0
    self.failed = 0
    self.limited = 0
    self.worker_seconds = 0.0
    self.announced = False

  def enqueue(self, key: str) -> None:
    require(key in self.archive.data["documents"], "unknown processing document")

    if key not in self.seen:
      self.seen.add(key)
      self.ready.append(key)

  def busy(self) -> bool:
    return bool(self.ready or self.workers.running())

  def fail(self, key: str, steps: list[str], message: str) -> None:
    self.archive.processing_failure(key, steps, message)
    self.failed += 1
    folder = self.archive.data["documents"][key]["folder"]
    print(
      f"error: {terminal(folder)}: {terminal(message)}",
      file=sys.stderr,
    )

  def plan(self, key: str) -> dict[str, Any] | None:
    doc = self.archive.data["documents"][key]

    try:
      with storage_io():
        verified = verify_source(self.archive.root, doc)
    except SourceProblem as error:
      self.fail(key, self.steps, error.message)
      return None

    if verified != doc["signature"]:
      doc["signature"] = verified
      self.archive.dirty = True

    if not self.force and self.archive.reusable(doc, self.steps):
      self.reused += 1
      if result_problems(doc["result"]):
        self.limited += 1
      return None

    self.archive.check_target("documents", doc["folder"], doc, exact=False)
    work = self.archive.new_work()
    self.archive.mkdir(work + "/new")
    spec = {
      "key": key,
      "root": str(self.archive.root),
      "document": copy.deepcopy(doc),
      "signature": verified,
      "steps": list(self.steps),
      "retry_steps": [
        step
        for step in self.steps
        if doc["attempts"].get(step, {}).get("error")
      ],
      "force": self.force,
      "settings": copy.deepcopy(self.archive.settings),
      "work": work,
    }
    raw = json_bytes(spec)
    require(len(raw) <= JOB_LIMIT, "worker job too large")
    self.archive.write(work + "/job.json", raw)
    return spec

  def _discard(self, spec: dict[str, Any]) -> None:
    require(
      not self.archive.exists(".ctl/pending.json"),
      "publication recovery required before discarding a job",
    )
    with storage_io():
      remove_work(self.archive.root, spec["work"])

  def _message(self, spec: dict[str, Any]) -> dict[str, Any]:
    try:
      with storage_io():
        try:
          raw = self.archive.read(spec["work"] + "/result.json", JOB_LIMIT)
        except FileNotFoundError:
          raise WorkerResultFailure("worker completion record missing") from None

      return parse_json(raw)

    except (ProtectionFailure, StorageFailure):
      raise
    except Failure as error:
      raise WorkerResultFailure(explanation(error)) from None

  def _completed(self, spec: dict[str, Any], message: dict[str, Any]) -> None:
    result = message["result"]
    problems = result_problems(result)
    self.worker_seconds += message["seconds"]
    self.completed += 1

    failed = any(
      node["retry"]
      or any(
        (node["attempts"].get(step) or node["products"].get(step) or {}).get(
          "retry", False
        )
        for step in spec["steps"]
      )
      for node in result["nodes"].values()
    )

    if failed:
      self.failed += 1
    elif problems:
      self.limited += 1

    name = terminal(spec["document"]["folder"])
    print(
      f"processed: {name} ({message['seconds']:.1f}s)",
      file=sys.stderr,
      flush=True,
    )
    if problems:
      print(
        f"  {len(problems)} findings; see course TXT Extraction notes "
        "after successful export",
        file=sys.stderr,
      )

    quiet = (
      "UserWarning: Workbook contains no default style, apply openpyxl's default"
    )
    notes = [note for note in message["notes"] if note != quiet]
    for note in notes[:3]:
      print(f"  warning: {terminal(note)[:200]}", file=sys.stderr)

  def tick(self) -> None:
    for spec, error in self.workers.collect():
      if error is not None:
        self.fail(spec["key"], spec["steps"], error)
        self._discard(spec)
        continue

      try:
        message = self._message(spec)
        self.archive.publish(spec, message)
      except (SourceProblem, WorkerResultFailure) as error:
        self.fail(spec["key"], spec["steps"], error.message)
        self._discard(spec)
        continue

      self._completed(spec, message)

    planned = 0

    while self.ready and self.workers.capacity() and planned < 16:
      key = self.ready.popleft()
      planned += 1
      spec = self.plan(key)

      if spec is None:
        continue

      if not self.announced:
        print(
          f"workers: {self.workers.count}",
          file=sys.stderr,
          flush=True,
        )
        self.announced = True

      self.workers.submit(spec)

  def close(self, message: str = "interrupted before publication") -> None:
    abandoned = self.workers.close()

    if self.archive.exists(".ctl/pending.json"):
      return

    for spec in abandoned:
      self.archive.processing_failure(
        spec["key"],
        spec["steps"],
        message + "; rerun to retry",
      )

  def progress(self) -> None:
    if self.busy():
      print(
        f"local: {self.workers.running()} running, "
        f"{len(self.ready)} queued, {self.completed} roots processed",
        file=sys.stderr,
        flush=True,
      )

  def summary(self) -> dict[str, Any]:
    return {
      "processed_roots": self.completed,
      "reused_roots": self.reused,
      "failed_roots": self.failed,
      "limited_roots": self.limited,
      "worker_seconds": round(self.worker_seconds, 3),
    }
