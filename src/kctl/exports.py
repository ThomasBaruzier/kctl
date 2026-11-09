from __future__ import annotations

import sys

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .archive import source_content
from .content import LimitedWriter
from .core import (
  CONTAINER_KINDS,
  CONVERT_KINDS,
  DOCUMENTS,
  EXPORT_LIMIT,
  EXTRACTED,
  IMAGE_SUFFIXES,
  TEXT_KINDS,
  TEXT_LIMIT,
  Failure,
  LimitReached,
  SourceProblem,
  decode_text,
  digest,
  explanation,
  fenced,
  join_path,
  label,
  named_kind,
  public_path,
  require,
  terminal,
  text_language,
)
from .records import ready, step_findings
from .storage import (
  checked_local,
  file_digest,
  signature,
  storage_io,
)

if TYPE_CHECKING:
  from .archive import Archive


class _TextUnavailable(Failure):
  pass


@contextmanager
def _source_io(relative: str) -> Iterator[None]:
  with storage_io():
    try:
      yield
    except FileNotFoundError:
      raise SourceProblem(
        f"{relative}: recorded input is missing"
      ) from None


@dataclass(frozen=True)
class Original:
  path: str
  size: int
  sha256: str


@dataclass
class TextReference:
  path: str
  size: int | None
  span: dict[str, Any] | None
  originals: tuple[Original, ...] = ()
  language: str | None = None


@dataclass
class Candidate:
  document: dict[str, Any]
  folder: str
  node: dict[str, Any]
  origin: str

  def originals(self) -> tuple[Original, ...]:
    doc = self.document
    root = Original(
      join_path(doc["folder"], doc["original"]),
      doc["bytes"],
      doc["sha256"],
    )

    if not self.folder:
      return (root,)

    node = self.node
    child = Original(
      join_path(doc["folder"], join_path(self.folder, node["original"])),
      node["bytes"],
      node["sha256"],
    )
    return root, child


@dataclass
class Block:
  reference: TextReference
  sha256: str
  origins: list[str]


def _unprocessed_node(doc: dict[str, Any]) -> dict[str, Any]:
  kind = named_kind(doc["original"])
  if kind == "unsupported":
    kind = named_kind(doc["name"])

  return {
    "name": doc["name"],
    "original": doc["original"],
    "kind": kind,
    "sha256": doc["sha256"],
    "bytes": doc["bytes"],
    "children": [],
    "problems": [],
    "retry": False,
    "products": {},
    "attempts": {},
    "visuals": None,
  }


def _coverage(node: dict[str, Any], step: str) -> set[str]:
  return {
    message
    for field in ("products", "attempts")
    for message in node[field].get(step, {}).get("summary", {}).get(
      "coverage", []
    )
  }


class CourseExporter:
  def __init__(self, archive: Archive, course_key: str):
    self.archive = archive
    self.course_key = course_key
    self.course = archive.data["items"][course_key]
    self.groups: dict[tuple[str, str], list[Candidate]] = {}
    self.blocks: dict[tuple[str | None, str], Block] = {}
    self.dependencies: dict[str, dict[str, int]] = {}
    self.verified: dict[Original, dict[str, int]] = {}
    self.diagnostics: dict[tuple[str, str], bool] = {}
    self.notes: dict[str, set[str]] = {}
    self.partial = False
    self.has_ocr = False
    self.text_bytes = 0
    self.duplicates = 0

  def warn(
    self,
    origin: str,
    message: str,
    *,
    partial: bool = True,
    record: bool = True,
  ) -> None:
    self.partial |= partial
    if record:
      self.notes.setdefault(origin, set()).add(message)

    diagnostic = origin, message
    self.diagnostics[diagnostic] = self.diagnostics.get(diagnostic, False) or partial

  def report(self, relative: str) -> None:
    if not self.diagnostics:
      return

    title = terminal(self.course["title"])[:100]
    for severity, prefix in ((True, "warning"), (False, "note")):
      groups: dict[str, list[str]] = {}
      for (origin, message), partial in sorted(self.diagnostics.items()):
        if partial == severity:
          groups.setdefault(origin, []).append(message)
      if not groups:
        continue

      print(
        f"{prefix}: {title}: {len(groups)} sources with findings",
        file=sys.stderr,
      )
      for origin, messages in list(groups.items())[:3]:
        print("  " + terminal(origin)[:140], file=sys.stderr)
        for message in messages[:2]:
          print("    " + terminal(message)[:200], file=sys.stderr)
        if len(messages) > 2:
          print(f"    {len(messages) - 2} more findings", file=sys.stderr)
      if len(groups) > 3:
        print(f"  {len(groups) - 3} more sources", file=sys.stderr)

    if self.notes:
      print(
        "  Full extraction limitations: " + terminal(relative)
        + " — Extraction notes",
        file=sys.stderr,
      )

  def _originals(
    self, originals: tuple[Original, ...]
  ) -> dict[str, dict[str, int]]:
    dependencies = {}

    for original in originals:
      with _source_io(original.path):
        path = checked_local(self.archive.root, original.path)
        before = signature(path)

        if before["size"] != original.size:
          raise SourceProblem(f"{original.path}: original size changed")

        if self.verified.get(original) != before:
          if file_digest(path) != original.sha256:
            raise SourceProblem(f"{original.path}: original contents changed")
          if signature(path) != before:
            raise SourceProblem(
              f"{original.path}: original changed while reading"
            )
          self.verified[original] = before

        dependencies[original.path] = before

    return dependencies

  def _read(
    self, reference: TextReference
  ) -> tuple[str, dict[str, dict[str, int]]]:
    dependencies = self._originals(reference.originals)

    with _source_io(reference.path):
      path = checked_local(self.archive.root, reference.path)
      before = signature(path)

      if reference.size is not None and before["size"] != reference.size:
        raise SourceProblem(f"{reference.path}: recorded text size changed")

      if before["size"] > TEXT_LIMIT:
        raise LimitReached(f"{reference.path}: text input exceeds 32 MiB")

      if (
        reference.path in dependencies
        and dependencies[reference.path] != before
      ):
        raise SourceProblem(
          f"{reference.path}: original changed before reading"
        )

      raw = self.archive.read(reference.path, TEXT_LIMIT)

      if signature(path) != before:
        raise SourceProblem(f"{reference.path}: text changed while reading")

    dependencies[reference.path] = before

    if reference.span is None:
      try:
        text = decode_text(raw)
      except Failure as error:
        raise _TextUnavailable(error.message) from None
    else:
      text = source_content(raw, reference.span).decode("utf-8")
      text = text.replace("\r\n", "\n").replace("\r", "\n")

    if len(text.encode()) > TEXT_LIMIT:
      raise LimitReached(f"{reference.path}: decoded text exceeds 32 MiB")

    return text, dependencies

  def _remember(self, dependencies: dict[str, dict[str, int]]) -> None:
    for path, current in dependencies.items():
      previous = self.dependencies.get(path)
      if previous is not None and previous != current:
        raise SourceProblem(f"{path}: export input changed")
      self.dependencies[path] = current

  def _add(
    self,
    reference: TextReference,
    text: str,
    dependencies: dict[str, dict[str, int]],
    origins: list[str],
  ) -> bool:
    self._remember(dependencies)

    if not text.strip():
      return False

    raw = text.encode()
    text_hash = digest(raw)
    key = reference.language, text_hash
    block = self.blocks.get(key)

    if block is not None:
      self.duplicates += 1
      block.origins = list(dict.fromkeys([*block.origins, *origins]))
      return True

    self.text_bytes += len(raw)
    if self.text_bytes > EXPORT_LIMIT:
      raise LimitReached("course export exceeds 256 MiB")

    self.blocks[key] = Block(
      reference, text_hash, list(dict.fromkeys(origins))
    )
    return True

  def _gather_document(self, doc: dict[str, Any]) -> None:
    require(
      doc["folder"].startswith(self.course["folder"] + "/"),
      "document reference leaves its course",
    )
    owner = self.archive.data["items"][doc["owner"]]
    root_origin = (
      owner["path"].removeprefix(DOCUMENTS + "/")
      + " / "
      + doc["name"]
    )
    documents = self.archive.data["documents"]
    if sum(
      documents.get(key, {}).get("name") == doc["name"]
      for key in owner.get("documents", [])
    ) > 1:
      root_origin += " (" + Path(doc["folder"]).name + ")"

    result = doc.get("result")
    nodes = result["nodes"] if result is not None else {
      "": _unprocessed_node(doc),
    }
    stack = [("", root_origin)]
    seen = set()

    while stack:
      folder, origin = stack.pop()
      require(folder not in seen, "duplicate content-node traversal")
      seen.add(folder)
      node = nodes[folder]
      candidate = Candidate(doc, folder, node, origin)
      self.groups.setdefault(
        (node["sha256"], node["kind"]), []
      ).append(candidate)

      names = Counter(nodes[child]["name"] for child in node["children"])
      for child in reversed(node["children"]):
        name = nodes[child]["name"]
        if names[name] > 1:
          name += " (" + Path(child).name + ")"
        stack.append((child, origin + " / " + name))

  def gather(self) -> None:
    documents = self.archive.data["documents"]
    seen_documents = set()

    for key, item in self.archive.walk_course(self.course_key):
      if item is None:
        self.warn(key, "referenced CTL page is not recorded")
        continue

      origin = item.get("path", item["title"]).removeprefix(DOCUMENTS + "/")
      has_body = "content_bytes" in item
      problems = item.get("problems", [])

      for problem in problems:
        self.warn(origin, problem)

      if item["status"] in {"failed", "partial", "unsupported"}:
        reason = item.get("message")
        if reason or not problems:
          if has_body and (
            item["status"] == "failed" or reason == "retrieval incomplete"
          ):
            reason = "Previous page content retained: " + (
              reason or "refresh failed"
            )
          self.warn(origin, reason or item["status"])
      elif item["status"] == "absent" and (
        has_body or item.get("children") or item.get("related")
      ):
        self.warn(origin, "not currently offered; retained content is historical")

      if has_body and not item.get("empty_listing", False):
        reference = TextReference(item["path"], None, item)
        text, dependencies = self._read(reference)
        self._add(reference, text, dependencies, [origin])
      elif (
        not has_body
        and "children" not in item
        and item["status"] not in {"empty", "absent"}
      ):
        self.warn(origin, "source page content has not been captured")

      for doc_key in item.get("documents", []):
        if doc_key in seen_documents:
          continue

        doc = documents.get(doc_key)
        if doc is None:
          self.warn(origin, "referenced attachment record is missing")
          continue

        require(doc["owner"] == key, "document owner reference mismatch")
        if (
          doc["role"] == "submission"
          and not self.course["include_submissions"]
        ):
          continue

        seen_documents.add(doc_key)
        self._gather_document(doc)

  def _healthy(self, candidates: list[Candidate]) -> list[Candidate]:
    healthy = []
    errors = []

    for candidate in candidates:
      try:
        self._originals(candidate.originals())
      except SourceProblem as error:
        errors.append((candidate.origin, error.message))
      else:
        healthy.append(candidate)

    if not healthy:
      raise SourceProblem(
        errors[0][1] if errors else "recorded original unavailable"
      )

    for origin, message in errors:
      self.warn(
        origin,
        message + "; using identical original at " + healthy[0].origin,
        partial=False,
      )

    return healthy

  def _findings(
    self,
    candidate: Candidate,
    step: str,
    *,
    coverage: bool,
    partial: bool = True,
    suffix: str = "",
  ) -> None:
    covered = _coverage(candidate.node, step)

    for message, origins in step_findings(candidate.node, step).items():
      if (message in covered) == coverage:
        self.warn(
          candidate.origin,
          f"{step}, {' / '.join(origins)}: {message}{suffix}",
          partial=partial,
        )

  def _discovery(
    self,
    candidates: list[Candidate],
    healthy: list[Candidate],
  ) -> None:
    self._remember(self._originals(healthy[0].originals()))

    for candidate in candidates:
      node = candidate.node
      result = candidate.document.get("result")

      for problem in node["problems"]:
        self.warn(candidate.origin, problem)

      if node["retry"] and not node["problems"]:
        self.warn(candidate.origin, "content discovery requires retry")

      if (
        result is not None
        and result["settings"] != self.archive.settings["walk"]
      ):
        self.warn(
          candidate.origin,
          "content discovery uses older processing settings",
        )

      for step in ("convert", "ocr"):
        self._findings(candidate, step, coverage=True)

      if not candidate.folder:
        for step, attempt in candidate.document["attempts"].items():
          if attempt.get("error"):
            self.warn(
              candidate.origin,
              f"{step}: latest root attempt failed: {attempt['error']}",
            )

  def _text(
    self, candidates: list[Candidate], origins: list[str]
  ) -> None:
    errors = []
    unsupported = []

    for candidate in candidates:
      node = candidate.node
      original = candidate.originals()[-1]
      reference = TextReference(
        original.path,
        original.size,
        None,
        candidate.originals(),
        text_language(node["kind"], node["name"]),
      )

      try:
        text, dependencies = self._read(reference)
      except SourceProblem as error:
        errors.append(error.message)
        continue
      except _TextUnavailable as error:
        unsupported.append(error.message)
        continue

      self._add(reference, text, dependencies, origins)
      return

    if unsupported:
      for origin in origins:
        self.warn(origin, unsupported[0])
      return

    raise SourceProblem(
      errors[0] if errors else "recorded source text unavailable"
    )

  def _product_diagnostics(
    self,
    candidates: list[Candidate],
    selected: Candidate,
    step: str,
    damaged: dict[int, str],
  ) -> None:
    selected_product = selected.node["products"][step]

    for candidate in candidates:
      node = candidate.node
      product = node["products"].get(step)
      attempt = node["attempts"].get(step)
      messages = []

      if id(candidate) in damaged:
        messages.append(damaged[id(candidate)])
      elif product is None:
        messages.append("no local result")
      else:
        if (
          product["status"] != "complete"
          and not product["problems"]
          and not product.get("summary", {}).get("coverage")
        ):
          messages.append(product["status"])
        if product["settings"] != self.archive.settings[step]:
          messages.append("result uses older processing settings")
        if (
          candidate is not selected
          and selected_product["content_bytes"]
          and not product["content_bytes"]
        ):
          messages.append("local result contains no source text")

      if attempt and attempt["retained"]:
        messages.append("Previous result retained after the latest attempt")

      replacement = candidate is not selected
      suffix = (
        "; using result from identical original at " + selected.origin
        if replacement else ""
      )
      self._findings(
        candidate,
        step,
        coverage=False,
        partial=not replacement,
        suffix=suffix,
      )

      for message in dict.fromkeys(messages):
        self.warn(
          candidate.origin,
          f"{step}: {message}{suffix}",
          partial=not replacement,
        )

  def _product(
    self,
    candidates: list[Candidate],
    origins: list[str],
    step: str,
    *,
    required: bool,
  ) -> tuple[dict[str, Any] | None, bool]:
    choices = [
      (candidate, candidate.node["products"][step])
      for candidate in candidates
      if step in candidate.node["products"]
    ]
    choices.sort(key=lambda pair: (
      pair[1]["status"] != "complete",
      pair[1]["settings"] != self.archive.settings[step],
      pair[0].origin,
      pair[0].document["folder"],
    ))
    damaged = {}
    damaged_text = []
    empty = None

    for candidate, product in choices:
      try:
        dependencies = self._originals(candidate.originals())
        text = ""
        reference = None

        if product["markdown"] is not None:
          result = candidate.document["result"]
          reference = TextReference(
            join_path(candidate.document["folder"], product["markdown"]),
            result["files"][product["markdown"]],
            product,
            candidate.originals(),
          )
          text, dependencies = self._read(reference)

        if not text.strip():
          if product["status"] == "complete" and empty is None:
            empty = candidate, product, dependencies
          continue

      except SourceProblem as error:
        damaged[id(candidate)] = error.message
        if product["content_bytes"]:
          damaged_text.append(error.message)
        continue

      labels = [
        origin + (" / OCR" if step == "ocr" else "")
        for origin in origins
      ]
      self._add(reference, text, dependencies, labels)
      self.has_ocr |= step == "ocr"
      self._product_diagnostics(candidates, candidate, step, damaged)
      return product, True

    if damaged_text:
      raise SourceProblem(damaged_text[0])

    if empty is not None:
      candidate, product, dependencies = empty
      self._remember(dependencies)
      self._product_diagnostics(candidates, candidate, step, damaged)
      return product, False

    if damaged:
      raise SourceProblem(next(iter(damaged.values())))

    attempted = any(
      step in candidate.node["attempts"]
      or step in candidate.document["attempts"]
      for candidate in candidates
    )

    if choices or attempted or required:
      for candidate in candidates:
        node = candidate.node
        self._findings(candidate, step, coverage=False)
        advice = (
          "run kctl process"
          if candidate.document["attempts"].get(step, {}).get("error")
          or not ready(node, step, self.archive.settings[step])
          else "original retained"
        )
        self.warn(candidate.origin, f"no usable {step} result; {advice}")

    return None, False

  def select(self) -> None:
    for (_, kind), candidates in self.groups.items():
      candidates.sort(key=lambda candidate: (
        candidate.origin,
        candidate.document["folder"],
        candidate.folder,
      ))
      origins = list(dict.fromkeys(
        candidate.origin for candidate in candidates
      ))
      healthy = self._healthy(candidates)
      self._discovery(candidates, healthy)

      if kind in TEXT_KINDS:
        self._text(candidates, origins)
        continue

      if kind in CONTAINER_KINDS:
        if not any(candidate.document.get("result") for candidate in candidates):
          for origin in origins:
            self.warn(
              origin,
              "container has not been extracted; run kctl process",
            )
        continue

      if kind not in CONVERT_KINDS:
        for origin in origins:
          self.warn(origin, "unsupported file type; original retained")
        continue

      converted, native_text = self._product(
        candidates,
        origins,
        "convert",
        required=kind not in IMAGE_SUFFIXES,
      )
      has_visuals = any(
        (candidate.node.get("visuals") or {}).get("images")
        or candidate.node["products"].get("convert", {}).get(
          "summary", {}
        ).get("images", 0)
        for candidate in candidates
      )
      recognized, ocr_text = self._product(
        candidates,
        origins,
        "ocr",
        required=kind in IMAGE_SUFFIXES or has_visuals or not native_text,
      )

      if not native_text and not ocr_text and recognized is not None:
        self.warn(
          origins[0],
          "OCR completed without recognized text; original retained",
          partial=False,
          record=False,
        )
      elif not native_text and not ocr_text and converted is not None:
        for origin in origins:
          self.warn(origin, "conversion produced no source text")

  def _check_dependencies(self) -> None:
    for relative, expected in self.dependencies.items():
      with _source_io(relative):
        path = checked_local(self.archive.root, relative)
        if signature(path) != expected:
          raise SourceProblem(f"{relative}: export input changed")

  def write(self) -> tuple[str, int]:
    relative = EXTRACTED + "/" + Path(self.course["folder"]).name + ".txt"
    public_path(relative)
    self._check_dependencies()

    with self.archive.atomic(relative) as stream:
      writer = LimitedWriter(stream, EXPORT_LIMIT)

      for block in self.blocks.values():
        text, dependencies = self._read(block.reference)
        self._remember(dependencies)
        if digest(text.encode()) != block.sha256:
          raise SourceProblem(f"{block.reference.path}: export text changed")

        if len(block.origins) == 1:
          writer.write(("## " + label(block.origins[0]) + "\n\n").encode())
        else:
          writer.write(b"## Sources\n\n")
          for origin in block.origins:
            writer.write(("- " + label(origin) + "\n").encode())
          writer.write(b"\n")

        if block.reference.language is not None:
          writer.write(fenced(text, block.reference.language).encode())
        else:
          writer.write(text.encode())
          if not text.endswith("\n"):
            writer.write(b"\n")

        writer.write(b"\n")

      writer.write((
        "## Extraction notes\n\nGenerated by kctl\n"
        f"Sources with notes: {len(self.notes)}\n"
      ).encode())

      if self.has_ocr:
        writer.write(
          b"\nOCR text may contain errors and does not establish "
          b"complete visual recovery.\n"
        )

      if self.notes:
        writer.write(b"\n")
        for origin, messages in sorted(self.notes.items()):
          line = "- " + label(origin) + ": " + "; ".join(
            label(message) for message in sorted(messages)
          )
          writer.write((line + "\n").encode())

      self._check_dependencies()

    return relative, writer.size

  def run(self) -> tuple[str, int]:
    self.gather()
    self.select()
    return self.write()


def export_courses(archive: Archive, course_keys: list[str]) -> int:
  courses = {
    key: item
    for key, item in archive.data["items"].items()
    if "course_id" in item
  }
  requested = set(course_keys)
  require(requested <= courses.keys(), "unknown course selected for export")

  names = [
    (Path(course["folder"]).name + ".txt").casefold()
    for course in courses.values()
  ]
  require(
    len(names) == len(set(names)),
    "course export filename collision",
  )

  if not requested:
    return 0

  with storage_io():
    archive.mkdir(EXTRACTED)
    archive.clean_atomic(EXTRACTED)

  failed = False
  partial = False

  for key in sorted(
    requested,
    key=lambda value: (
      courses[value]["folder"].casefold(),
      courses[value]["course_id"],
    ),
  ):
    exporter = CourseExporter(archive, key)

    try:
      relative, size = exporter.run()
    except (SourceProblem, LimitReached) as error:
      failed = True
      print(
        f"error: {terminal(courses[key]['title'])}: "
        + terminal(explanation(error))
        + "; course export was not replaced",
        file=sys.stderr,
      )
      continue

    partial |= exporter.partial
    exporter.report(relative)
    print(
      f"exported: {terminal(archive.root / relative)} "
      f"({size} bytes, {len(exporter.blocks)} sections, "
      f"{exporter.duplicates} duplicate bodies omitted)"
    )

  return 2 if failed else 3 if partial else 0
