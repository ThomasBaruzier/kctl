from __future__ import annotations

import copy
import math
import os
import re
import sys
import time

from collections import deque
from pathlib import Path
from typing import Any, Iterator

from .core import (
  DOCUMENTS,
  EXTRACTED,
  MANIFEST_LIMIT,
  TEXT_LIMIT,
  TOKEN,
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  WorkerResultFailure,
  digest,
  display,
  explanation,
  join_path,
  json_bytes,
  label,
  now,
  parent_path,
  parse_json,
  publication_path,
  require,
  terminal,
  token,
  valid_id,
)
from .records import (
  RUN_STATUSES,
  STEPS,
  applicable,
  content_images,
  document_files,
  file_parents,
  new_manifest,
  processing_settings,
  ready,
  validate_document,
  validate_item,
  validate_manifest,
  validate_problems,
  validate_result_data,
  validate_span,
)
from .storage import (
  Tree,
  check_job_source,
  checked_directory,
  checked_file,
  checked_local,
  file_digest,
  read_private,
  remove_work,
  signature,
  source_signature,
  storage_io,
  sync_directory,
  sync_tree,
  write_new,
)


def _walk_error(error: OSError) -> None:
  raise error


def _work_path(value: Any) -> None:
  require(
    isinstance(value, str)
    and re.fullmatch(r"\.ctl/work/[0-9a-f]{32}", value),
    "invalid publication workspace",
  )


def source_content(raw: bytes, record: dict[str, Any]) -> bytes:
  try:
    validate_span(record, len(raw))
  except Failure:
    raise SourceProblem("stored source-content boundaries are invalid") from None

  start = record["header_bytes"]
  content = raw[start:start + record["content_bytes"]]

  if digest(content) != record["content_sha256"]:
    raise SourceProblem("stored source content changed")

  try:
    content.decode("utf-8")
  except UnicodeError:
    raise SourceProblem("stored source content is not valid UTF-8") from None

  return content


def check_bundle(
  path: Path, allowed: set[str], exact: bool = False
) -> None:
  checked_directory(path.lstat())
  directories_allowed = file_parents(allowed)
  seen = set()

  for directory, directories, files in os.walk(
    path, followlinks=False, onerror=_walk_error
  ):
    checked_directory(Path(directory).lstat())

    for name in directories:
      child = Path(directory) / name
      checked_directory(child.lstat())
      require(
        child.relative_to(path).as_posix() in directories_allowed,
        "untracked directory prevents document replacement",
      )

    for name in files:
      child = Path(directory) / name
      checked_file(child.lstat())
      relative = child.relative_to(path).as_posix()
      require(
        relative in allowed,
        "untracked file prevents document replacement",
      )
      seen.add(relative)

  if exact:
    require(seen == allowed, "document output missing")


def cached_result_valid(root: Path, result: dict[str, Any]) -> bool:
  images = content_images(result["nodes"])
  originals = {
    join_path(folder, node["original"]): node["sha256"]
    for folder, node in result["nodes"].items()
    if folder
  }
  products = {
    product["markdown"]: product
    for node in result["nodes"].values()
    for product in node["products"].values()
    if product["markdown"] is not None
  }

  try:
    for relative, size in result["files"].items():
      path = checked_local(root, relative)
      before = signature(path)

      if before["size"] != size:
        return False

      expected = originals.get(relative)
      if relative in images:
        expected = Path(relative).stem

      if expected is not None and file_digest(path) != expected:
        return False

      if relative in products:
        source_content(read_private(path, TEXT_LIMIT), products[relative])

      if signature(path) != before:
        return False

  except (FileNotFoundError, SourceProblem):
    return False

  return True


class Archive(Tree):
  def __init__(self, root: Path, create: bool):
    super().__init__(root, create)
    self.run_id = token()
    self.settings = processing_settings()
    self.active_run = False
    self.dirty = False
    self.dirty_items: set[str] = set()
    self.missing: set[str] = set()
    self.damaged: dict[str, str] = {}
    self.last_checkpoint = time.monotonic()

    try:
      if not self.exists(".ctl"):
        require(create and not os.listdir(self.fd), "expected empty archive")
        self.mkdir(".ctl")

      self.lock(".ctl/lock")

      if self.exists(".ctl/manifest.json"):
        self.data = parse_json(
          self.read(".ctl/manifest.json", MANIFEST_LIMIT)
        )
        self.validate()
      else:
        require(
          create
          and set(os.listdir(self.fd)) == {".ctl"}
          and all(
            name == "lock" or re.fullmatch(r"\.write-[0-9a-f]{32}", name)
            for name in os.listdir(self.path(".ctl"))
          ),
          "archive manifest missing or directory not empty",
        )
        self.clean_atomic(".ctl")
        self.data = new_manifest()
        self.checkpoint()

    except BaseException:
      self.close()
      raise

  def validate(self) -> None:
    validate_manifest(self.data)

  def _prepare_storage(self) -> None:
    self.clean_atomic(".ctl")
    self.mkdir(".ctl/work")

    if self.exists(".ctl/pending.json"):
      self.recover()

    self.cleanup_work()

  def prepare(self) -> None:
    self._prepare_storage()
    self.mkdir(DOCUMENTS)
    parents = {DOCUMENTS} | {
      parent_path(item["path"])
      for item in self.data["items"].values()
      if item.get("path")
    }

    for parent in sorted(parents):
      self.clean_atomic(parent)

    self.scan_missing()
    self.dirty_items.update(self.data["items"])
    self.flush_views()

  def prepare_export(self) -> None:
    self._prepare_storage()
    self.mkdir(EXTRACTED)
    self.clean_atomic(EXTRACTED)

  def checkpoint(self) -> None:
    raw = json_bytes(self.data)
    require(len(raw) <= MANIFEST_LIMIT, "archive manifest too large")
    self.write(".ctl/manifest.json", raw)
    self.dirty = False
    self.last_checkpoint = time.monotonic()

  def flush(self, force: bool = False) -> None:
    if self.dirty and (
      force or time.monotonic() - self.last_checkpoint >= 1
    ):
      self.checkpoint()

  def begin(self, command: str) -> None:
    require(command in {"sync", "process"}, "invalid archive command")
    require(not self.active_run, "archive run already active")
    require(
      not self.exists(".ctl/pending.json"),
      "publication recovery required",
    )
    self.data["run"] = {
      "id": self.run_id,
      "command": command,
      "started": now(),
      "status": "running",
    }
    self.checkpoint()
    self.active_run = True

  def account(self, account: str) -> None:
    require(
      valid_id(account) and self.data["account"] in {None, account},
      "archive belongs to another account",
    )

    if self.data["account"] != account:
      self.data["account"] = account
      self.checkpoint()

  def new_work(self) -> str:
    relative = ".ctl/work/" + token()
    self.mkdir(relative)
    return relative

  def cleanup_work(self) -> None:
    require(
      not self.exists(".ctl/pending.json"),
      "publication recovery incomplete",
    )

    if not self.exists(".ctl/work"):
      return

    for child in self.path(".ctl/work").iterdir():
      require(TOKEN.fullmatch(child.name), "unrecognized work entry")
      with storage_io():
        remove_work(self.root, ".ctl/work/" + child.name)

  def assert_target(
    self, section: str, key: str, record: dict[str, Any]
  ) -> str:
    require(section in {"items", "documents"}, "invalid publication section")
    field = "folder" if section == "documents" else "path"
    target = record[field]
    publication_path(target)
    old = self.data[section].get(key)

    if old and old.get(field):
      require(old[field] == target, "assigned publication path changed")

    for other, item in self.data["items"].items():
      path = item.get("path")
      if not path or section == "items" and other == key:
        continue

      require(
        target != path
        and not target.startswith(path + "/")
        and not path.startswith(target + "/"),
        "publication overlaps a page",
      )

    for other, doc in self.data["documents"].items():
      if section == "documents" and other == key:
        require(
          doc["original"] == record["original"],
          "original name changed",
        )
        continue

      folder = doc["folder"]
      require(
        target != folder
        and not target.startswith(folder + "/")
        and not folder.startswith(target + "/"),
        "publication overlaps a document",
      )

    if section == "documents":
      owner = self.data["items"].get(record["owner"])
      require(
        owner is not None
        and owner.get("path")
        and parent_path(target) == owner["folder"],
        "document is outside its owner folder",
      )

    return target

  def check_target(
    self,
    section: str,
    relative: str,
    record: dict[str, Any],
    exact: bool,
  ) -> None:
    path = self.path(relative)

    if section == "items":
      info = path.lstat()
      checked_file(info)
      if exact:
        require(info.st_size <= TEXT_LIMIT, "page exceeds size limit")
        if "content_bytes" in record:
          source_content(self.read(relative, TEXT_LIMIT), record)
      return

    check_bundle(path, document_files(record), exact)

    if not exact:
      return

    require(
      (path / record["original"]).stat().st_size == record["bytes"],
      "original size mismatch",
    )
    for name, size in (record.get("result") or {}).get("files", {}).items():
      require(
        (path / name).stat().st_size == size,
        "processing output size mismatch",
      )

  def _validate_publication(
    self, section: str, relative: str, record: dict[str, Any]
  ) -> None:
    self.check_target(section, relative, record, exact=True)

    if section == "items":
      return

    path = self.path(relative)
    original = checked_local(path, record["original"])
    before = signature(original)
    require(
      file_digest(original) == record["sha256"]
      and signature(original) == before,
      "staged original identity mismatch",
    )

    result = record.get("result")
    require(
      result is None or cached_result_valid(path, result),
      "staged processing output missing or damaged",
    )

  def _prepare_publication(
    self,
    section: str,
    key: str,
    record: dict[str, Any],
    work: str,
  ) -> dict[str, Any]:
    require(
      not self.exists(".ctl/pending.json"),
      "publication needs recovery",
    )
    _work_path(work)
    require(isinstance(key, str) and key, "invalid publication key")

    if section == "documents":
      validate_document(key, record)
    else:
      require(section == "items", "invalid publication section")
      validate_item(record)

    target = self.assert_target(section, key, record)
    old = self.data[section].get(key)
    had_old = self.exists(target)
    require(
      old is not None or not had_old,
      "untracked publication destination",
    )

    if had_old:
      self.check_target(section, target, old, exact=False)

    with storage_io():
      self._validate_publication(section, work + "/new", record)
      sync_tree(self.path(work + "/new"))
      sync_directory(self.path(work))
      sync_directory(self.path(".ctl/work"))

    return {
      "section": section,
      "key": key,
      "record": copy.deepcopy(record),
      "old": copy.deepcopy(old),
      "work": work,
      "target": target,
      "had_old": had_old,
    }

  def _commit_publication(self, receipt: dict[str, Any]) -> None:
    raw = json_bytes(receipt)
    require(len(raw) <= MANIFEST_LIMIT, "publication receipt too large")
    self.checkpoint()
    self.write(".ctl/pending.json", raw)
    self.recover()

  def install(
    self,
    section: str,
    key: str,
    record: dict[str, Any],
    work: str,
  ) -> None:
    receipt = self._prepare_publication(section, key, record, work)
    self._commit_publication(receipt)

  @staticmethod
  def _same_record(
    section: str, current: Any, expected: Any
  ) -> bool:
    if section != "documents" or current is None or expected is None:
      return current == expected

    return {
      key: value for key, value in current.items() if key != "signature"
    } == {
      key: value for key, value in expected.items() if key != "signature"
    }

  def _read_receipt(self) -> dict[str, Any]:
    receipt = parse_json(
      self.read(".ctl/pending.json", MANIFEST_LIMIT)
    )
    require(
      isinstance(receipt, dict)
      and set(receipt) == {
        "section", "key", "record", "old", "work", "target", "had_old",
      }
      and isinstance(receipt["section"], str)
      and receipt["section"] in {"items", "documents"}
      and isinstance(receipt["key"], str)
      and receipt["key"]
      and isinstance(receipt["record"], dict)
      and type(receipt["had_old"]) is bool,
      "malformed publication receipt",
    )
    _work_path(receipt["work"])
    section = receipt["section"]
    key = receipt["key"]
    record = receipt["record"]
    previous = receipt["old"]

    for value in (record, previous):
      if value is None:
        continue
      if section == "documents":
        validate_document(key, value)
      else:
        validate_item(value)

    target = self.assert_target(section, key, record)
    require(target == receipt["target"], "publication target mismatch")
    require(
      not receipt["had_old"] or previous is not None,
      "publication backup record missing",
    )

    if previous is not None:
      field = "folder" if section == "documents" else "path"
      require(
        previous.get(field) in {None, target},
        "previous publication path mismatch",
      )
      if section == "documents":
        require(
          previous["original"] == record["original"],
          "previous original name mismatch",
        )

    current = self.data[section].get(key)
    require(
      current == previous
      or self._same_record(section, current, record),
      "publication receipt disagrees with manifest",
    )
    return receipt

  def recover(self) -> None:
    receipt = self._read_receipt()
    section = receipt["section"]
    key = receipt["key"]
    record = receipt["record"]
    previous = receipt["old"]
    work = receipt["work"]
    target = receipt["target"]
    new = work + "/new"
    old = work + "/old"

    if self.exists(new):
      self._validate_publication(section, new, record)

      if receipt["had_old"]:
        if self.exists(old):
          require(
            not self.exists(target),
            "conflicting publication targets",
          )
          self.check_target(section, old, previous, exact=False)
        else:
          require(
            self.exists(target),
            "previous publication target missing",
          )
          self.check_target(section, target, previous, exact=False)
          self.move(target, old)
      else:
        require(
          not self.exists(target) and not self.exists(old),
          "publication destination exists",
        )

      self.move(new, target)

    else:
      require(self.exists(target), "published target missing")
      require(
        self.exists(old) == receipt["had_old"],
        "publication backup state mismatch",
      )

      if receipt["had_old"]:
        self.check_target(section, old, previous, exact=False)

      self._validate_publication(section, target, record)

    self.check_target(section, target, record, exact=True)

    if section == "documents":
      record["signature"] = signature(
        self.path(join_path(target, record["original"]))
      )

    self.data[section][key] = record

    if section == "documents":
      self.mark(record["owner"])
    else:
      self.missing.discard(key)
      self.damaged.pop(key, None)
      self.mark(key)

    self.checkpoint()
    self.remove(".ctl/pending.json")

    with storage_io():
      remove_work(self.root, work)

  def mark(self, key: str) -> None:
    seen = set()

    while key is not None and key not in seen:
      seen.add(key)
      self.dirty_items.add(key)
      key = self.data["items"].get(key, {}).get("parent")

  def context(self, key: str) -> str:
    item = self.data["items"].get(key, {})
    path = item.get("path", "").removeprefix(DOCUMENTS + "/")
    return path.removesuffix("/index.md") or item.get("title", key)

  def item(
    self,
    key: str,
    changes: dict[str, Any],
    body: str | None,
  ) -> None:
    require(
      not self.exists(".ctl/pending.json"),
      "publication recovery required",
    )
    previous = self.data["items"].get(key)
    record = {
      **copy.deepcopy(previous or {}),
      **copy.deepcopy(changes),
      "run": self.run_id,
      "checked": now(),
    }

    if "course_id" in record:
      require(
        all(
          other == key or item.get("course_id") != record["course_id"]
          for other, item in self.data["items"].items()
        ),
        "duplicate course identity",
      )

    if record.get("path"):
      self.assert_target("items", key, record)

      if not (previous or {}).get("path"):
        require(
          not self.exists(record["path"]),
          "untracked page destination",
        )

    if body is not None:
      require(isinstance(body, str), "invalid page body")
      record.pop("children", None)
      header = f"# {label(record['title'])}\n\n"

      if record.get("source"):
        header += f"Source: <{record['source']}>\n\n"

      prefix = header.encode()
      content = (body.rstrip("\n") + "\n").encode()
      raw = prefix + content
      require(len(raw) <= TEXT_LIMIT, "page exceeds size limit")
      record.update({
        "header_bytes": len(prefix),
        "content_bytes": len(content),
        "content_sha256": digest(content),
        "fetched": now(),
      })
      validate_item(record)

      work = self.new_work()
      write_new(self.path(work + "/new"), raw)
      self.install("items", key, record, work)

    else:
      if "children" in changes:
        for name in ("header_bytes", "content_bytes", "content_sha256"):
          record.pop(name, None)

      validate_item(record)
      self.data["items"][key] = record
      self.dirty = True

    self.mark(key)

    if record["status"] in {"failed", "unsupported"}:
      print(
        f"warning: {terminal(self.context(key))}: "
        + terminal(record.get("message") or record["status"]),
        file=sys.stderr,
      )

  def document(
    self,
    key: str,
    incoming: dict[str, Any],
    work: str | None,
  ) -> None:
    require(
      not self.exists(".ctl/pending.json"),
      "publication recovery required",
    )
    validate_document(key, incoming)
    record = copy.deepcopy(incoming)
    record["run"] = self.run_id
    self.assert_target("documents", key, record)
    old = self.data["documents"].get(key)

    if work is None:
      require(
        old is not None
        and old["sha256"] == record["sha256"]
        and old["bytes"] == record["bytes"],
        "reused document identity mismatch",
      )
      record["result"] = copy.deepcopy(old.get("result"))
      record["attempts"] = copy.deepcopy(old["attempts"])
      require(
        source_signature(self.root, record) == record["signature"],
        "original changed after retrieval verification",
      )
      self.data["documents"][key] = record
      self.dirty = True
      self.mark(record["owner"])

    else:
      _work_path(work)
      record["result"] = None
      record["attempts"] = {}
      staged = self.path(
        join_path(work + "/new", record["original"])
      )
      require(
        signature(staged) == record["signature"],
        "staged original changed",
      )
      self.install("documents", key, record, work)

    if record.get("cache") == "downloaded":
      print(
        "downloaded: " + terminal(record["name"]),
        file=sys.stderr,
        flush=True,
      )

  def processing_failure(
    self, key: str, steps: list[str], message: str
  ) -> None:
    require(
      steps and set(steps) <= STEPS,
      "invalid processing operations",
    )
    doc = self.data["documents"][key]

    for step in steps:
      doc["attempts"][step] = {
        "error": display(message) or "processing failed",
        "finished": now(),
      }

    self.dirty = True
    self.mark(doc["owner"])

  def reusable(self, doc: dict[str, Any], steps: list[str]) -> bool:
    result = doc.get("result")

    if (
      result is None
      or result["settings"] != self.settings["walk"]
      or result["nodes"][""]["name"] != display(doc["name"])
      or any(doc["attempts"].get(step, {}).get("error") for step in steps)
    ):
      return False

    for node in result["nodes"].values():
      if node["retry"]:
        return False

      for step in steps:
        if applicable(node["kind"], step) and not ready(
          node, step, self.settings[step]
        ):
          return False

    return cached_result_valid(self.path(doc["folder"]), result)

  def publish(self, spec: dict[str, Any], message: Any) -> None:
    try:
      require(
        isinstance(message, dict)
        and set(message) == {"result", "error", "fatal", "notes", "seconds"}
        and type(message["seconds"]) in {int, float}
        and math.isfinite(message["seconds"])
        and message["seconds"] >= 0,
        "malformed worker completion",
      )
      validate_problems(message["notes"])

      for name in ("error", "fatal"):
        require(
          message[name] is None
          or isinstance(message[name], str)
          and 0 < len(message[name]) <= 2048,
          "malformed worker failure",
        )

      if message["fatal"]:
        raise StorageFailure(display(message["fatal"]))
      if message["error"]:
        raise WorkerResultFailure(display(message["error"]))

      require(
        Path(spec["root"]) == self.root
        and spec["settings"] == self.settings
        and isinstance(spec["steps"], list)
        and spec["steps"]
        and all(step in STEPS for step in spec["steps"])
        and len(spec["steps"]) == len(set(spec["steps"])),
        "worker job context mismatch",
      )
      key = spec["key"]
      record = copy.deepcopy(self.data["documents"][key])
      require(
        spec["document"]["sha256"] == record["sha256"]
        and spec["document"]["folder"] == record["folder"]
        and spec["document"]["original"] == record["original"],
        "worker document identity mismatch",
      )
      check_job_source(spec)
      validate_result_data(message["result"], record)
      require(
        message["result"]["settings"] == self.settings["walk"],
        "worker traversal settings mismatch",
      )

      for node in message["result"]["nodes"].values():
        for step in spec["steps"]:
          if not applicable(node["kind"], step):
            continue

          latest = node["attempts"].get(step) or node["products"].get(step)
          require(
            latest is not None
            and latest["settings"] == self.settings[step],
            "worker omitted requested operation",
          )

      record["result"] = message["result"]
      for step in spec["steps"]:
        record["attempts"].pop(step, None)

      receipt = self._prepare_publication(
        "documents", key, record, spec["work"]
      )
      check_job_source(spec)

    except (ProtectionFailure, StorageFailure, SourceProblem):
      raise
    except (Failure, TypeError, KeyError, ValueError, OverflowError) as error:
      raise WorkerResultFailure(explanation(error)) from None

    self._commit_publication(receipt)

  def read_content(
    self,
    relative: str,
    record: dict[str, Any],
    *,
    size: int | None = None,
  ) -> bytes:
    try:
      path = checked_local(self.root, relative)
      before = signature(path)

      if size is not None and before["size"] != size:
        raise SourceProblem("stored text size changed")

      raw = self.read(relative, TEXT_LIMIT)
      content = source_content(raw, record)

      if signature(path) != before:
        raise SourceProblem("stored text changed while being read")

      return content

    except FileNotFoundError:
      raise SourceProblem("stored source content is missing") from None

  def scan_missing(self) -> None:
    missing = set()

    for key, item in self.data["items"].items():
      if "content_bytes" not in item:
        continue

      try:
        checked_file(self.path(item["path"]).lstat())
      except FileNotFoundError:
        missing.add(key)

    changed = self.missing.symmetric_difference(missing)
    self.missing = missing

    for key in changed:
      self.mark(key)

  def incomplete(self) -> set[str]:
    items = self.data["items"]
    parents: dict[str, set[str]] = {}
    affected = self.missing | self.damaged.keys()

    for key, item in items.items():
      if item["status"] in {"failed", "partial", "unsupported"}:
        affected.add(key)

      if any(
        doc not in self.data["documents"]
        for doc in item.get("documents", [])
      ):
        affected.add(key)

      for child in item.get("children", []) + item.get("related", []):
        parents.setdefault(child, set()).add(key)
        if child not in items:
          affected.add(key)

    pending = deque(affected)

    while pending:
      child = pending.popleft()
      for parent in parents.get(child, ()):
        if parent not in affected:
          affected.add(parent)
          pending.append(parent)

    return affected

  def walk_course(
    self, course_key: str
  ) -> Iterator[tuple[str, dict[str, Any] | None]]:
    items = self.data["items"]
    course = items.get(course_key)
    require(
      course is not None and "course_id" in course,
      "unknown course root",
    )
    prefix = course["folder"] + "/"
    seen = set()
    active = set()
    stack = [(course_key, None, False)]

    while stack:
      key, parent, leaving = stack.pop()

      if leaving:
        active.remove(key)
        continue

      require(key not in active, "cycle in course item references")
      item = items.get(key)

      if item is not None and parent is not None:
        require(
          item.get("parent") == parent,
          "course reference has a conflicting parent",
        )

      if key in seen:
        continue
      seen.add(key)

      if item is None:
        yield key, None
        continue

      require(
        key == course_key or "course_id" not in item,
        "course reference points to another course",
      )
      if item.get("path"):
        require(
          item["path"].startswith(prefix),
          "course reference leaves its assigned folder",
        )
      if item.get("folder"):
        require(
          item["folder"] == course["folder"]
          or item["folder"].startswith(prefix),
          "course folder reference leaves its assigned folder",
        )

      active.add(key)
      stack.append((key, parent, True))
      references = list(dict.fromkeys(
        item.get("children", []) + item.get("related", [])
      ))
      stack.extend(
        (child, key, False) for child in reversed(references)
      )
      yield key, item

  def document_keys(self, course_keys: list[str]) -> list[str]:
    keys = []
    seen = set()

    for course_key in dict.fromkeys(course_keys):
      course = self.data["items"].get(course_key)
      require(
        course is not None and "course_id" in course,
        "unknown course root",
      )

      for owner_key, item in self.walk_course(course_key):
        if item is None:
          continue

        for key in item.get("documents", []):
          doc = self.data["documents"].get(key)
          if doc is None:
            continue

          require(
            doc["owner"] == owner_key,
            "document owner reference mismatch",
          )
          if (
            doc["role"] == "submission"
            and not course["include_submissions"]
          ):
            continue

          if key not in seen:
            seen.add(key)
            keys.append(key)

    return keys

  def course_for_document(self, key: str) -> str | None:
    require(key in self.data["documents"], "unknown document")
    doc = self.data["documents"][key]
    owner = self.data["items"].get(doc["owner"])
    require(owner is not None and owner.get("path"), "invalid document owner")

    matches = [
      course_key
      for course_key, course in self.data["items"].items()
      if "course_id" in course
      and owner["path"].startswith(course["folder"] + "/")
    ]
    require(len(matches) <= 1, "ambiguous document course")
    return matches[0] if matches else None

  def flush_views(self) -> None:
    self.flush(force=True)

    if not self.dirty_items:
      return

    from .views import render

    for indexes in (False, True):
      incomplete = self.incomplete()

      for key in sorted(self.dirty_items):
        item = self.data["items"].get(key, {})
        if ("children" in item) != indexes:
          continue

        try:
          render(self, key, incomplete)
        except SourceProblem as error:
          if self.damaged.get(key) != error.message:
            self.damaged[key] = error.message
            self.mark(key)
            incomplete = self.incomplete()
            print(
              f"warning: {terminal(self.context(key))}: "
              + terminal(error.message)
              + "; stored page left unchanged",
              file=sys.stderr,
            )
        else:
          if self.damaged.pop(key, None) is not None:
            self.mark(key)
            incomplete = self.incomplete()
          self.dirty_items.discard(key)

  def finish(self, status: str, **details: Any) -> None:
    require(
      status in RUN_STATUSES - {"running"},
      "invalid final run status",
    )
    require(self.active_run, "no active archive run")
    require(
      not self.exists(".ctl/pending.json"),
      "publication recovery required",
    )

    self.data["run"].update(details)
    self.data["run"].update(status=status, finished=now())
    self.checkpoint()
    self.flush_views()
    self.active_run = False
