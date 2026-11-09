from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .archive import source_content
from .core import (
  TEXT_KINDS,
  TEXT_LIMIT,
  SourceProblem,
  join_path,
  label,
  link_to,
  require,
)
from .records import node_index, product_title, result_problems
from .storage import signature

if TYPE_CHECKING:
  from .archive import Archive


def _reachable(archive: Archive, key: str) -> set[str]:
  pending = [key]
  seen = set()

  while pending:
    current = pending.pop()
    if current in seen:
      continue

    seen.add(current)
    item = archive.data["items"].get(current, {})
    pending.extend(item.get("children", []))
    pending.extend(item.get("related", []))

  return seen


def item_link(
  archive: Archive,
  key: str,
  origin: str,
  incomplete: set[str],
) -> str:
  item = archive.data["items"].get(key)

  if item is None:
    return "- " + label(key.rsplit("/", 1)[-1]) + " - unavailable"

  title = label(item["title"])
  if item.get("path") and key not in archive.missing and key not in archive.damaged:
    title = f"[{title}]({link_to(item['path'], origin)})"

  status = "incomplete" if key in incomplete else item["status"]
  return "- " + title + ("" if status == "archived" else " - " + status)


def document_lines(
  archive: Archive, key: str, origin: str
) -> list[str]:
  doc = archive.data["documents"].get(key)

  if doc is None:
    return ["- Document unavailable."]

  links = []
  notes = []

  def add(title: str, relative: str) -> None:
    target = join_path(doc["folder"], relative)
    if archive.path(target).is_file():
      links.append(f"[{title}]({link_to(target, origin)})")
    else:
      notes.append(title + " missing")

  add("original", doc["original"])
  result = doc.get("result")

  if result is not None:
    root = result["nodes"][""]

    if node_index(root):
      add("contents / details", "index.md")

    if root["kind"] == "unsupported":
      notes.append("Original only; unsupported file type.")
    elif root["kind"] in TEXT_KINDS:
      notes.append("Text/source original; no duplicate conversion needed.")

    for step, title in (("convert", "text"), ("ocr", "OCR")):
      product = root["products"].get(step)

      if product and product["markdown"]:
        if step == "convert" and not product["content_bytes"]:
          title = "images"
        add(
          product_title(root, step, archive.settings, title),
          product["markdown"],
        )
      elif product and product["status"] == "complete" and step == "ocr":
        notes.append("OCR completed; no text recognized.")

    problems = result_problems(result)
    if problems:
      notes.append(f"{len(problems)} local processing limitations.")
      notes.extend(problems[:2])

  notes.extend(
    f"{step}: latest root attempt failed: {attempt['error']}"
    for step, attempt in doc["attempts"].items()
    if attempt.get("error")
  )
  line = "- " + label(doc["name"])

  if links:
    line += ": " + " | ".join(links)

  return [line] + ["  - " + label(note) for note in notes]


def page_status(item: dict[str, Any], has_body: bool) -> list[str]:
  status = item["status"]
  message = item.get("message", "")
  lines = []

  if has_body and (
    status in {"failed", "absent"} or message == "retrieval incomplete"
  ):
    lines += ["Showing previous content.", ""]

  defaults = {
    "failed": "Refresh failed",
    "partial": "Extraction incomplete",
    "unsupported": "This view is not fully supported",
    "absent": "Not available",
  }
  if status in defaults:
    lines += [label(message or defaults[status]), ""]

  return lines


def _index(
  archive: Archive,
  key: str,
  item: dict[str, Any],
  incomplete: set[str],
) -> bytes:
  lines = [f"# {label(item['title'])}", ""]
  lines.extend(page_status(item, has_body=False))
  lines.extend(
    item_link(archive, child, item["path"], incomplete)
    for child in item["children"]
  )

  if not item["children"]:
    lines.append(
      "No entries." if item["status"] == "empty" else "No entries recorded."
    )

  if key == "index":
    reachable = _reachable(archive, key)
    affected = [
      child
      for child, record in archive.data["items"].items()
      if child in reachable
      and (
        record["status"] in {"failed", "partial", "unsupported"}
        or child in archive.missing
        or child in archive.damaged
      )
    ]
    if affected:
      lines += ["", "## Incomplete", ""]

      for child in sorted(affected, key=archive.context):
        record = archive.data["items"][child]
        reason = (
          "stored page missing"
          if child in archive.missing
          else archive.damaged.get(child)
          or record.get("message")
          or record["status"]
        )
        lines.append(
          f"- {label(archive.context(child))}: {label(reason)}"
        )

  return ("\n".join(lines).rstrip("\n") + "\n").encode()


def _page(
  archive: Archive,
  key: str,
  item: dict[str, Any],
  incomplete: set[str],
) -> bytes | None:
  has_body = "content_bytes" in item

  if has_body:
    try:
      if signature(archive.path(item["path"]))["size"] > TEXT_LIMIT:
        raise SourceProblem("stored source page exceeds text limit")
      existing = archive.read(item["path"], TEXT_LIMIT)
    except FileNotFoundError:
      archive.missing.add(key)
      return None

    source_content(existing, item)
    end = item["header_bytes"] + item["content_bytes"]
    base = existing[:end]
  else:
    base = (
      f"# {label(item['title'])}\n\nPage content not archived yet.\n"
    ).encode()

  lines = ["", "---", ""]
  lines.extend(page_status(item, has_body))

  if item.get("documents"):
    lines += ["## Documents", ""]
    for doc_key in item["documents"]:
      lines.extend(document_lines(archive, doc_key, item["path"]))
    lines.append("")

  if item.get("related"):
    lines += ["## Related pages", ""]
    lines.extend(
      item_link(archive, child, item["path"], incomplete)
      for child in item["related"]
    )
    lines.append("")

  if item.get("problems"):
    lines += ["## Extraction limitations", ""]
    lines.extend("- " + label(problem) for problem in item["problems"])
    lines.append("")

  if item.get("fetched"):
    lines.append("Fetched: " + item["fetched"])

  return base + ("\n".join(lines).rstrip("\n") + "\n").encode()


def render(
  archive: Archive, key: str, incomplete: set[str]
) -> None:
  item = archive.data["items"].get(key)

  if item is None or not item.get("path"):
    return

  if "children" in item:
    raw = _index(archive, key, item, incomplete)
  else:
    raw = _page(archive, key, item, incomplete)
    if raw is None:
      return

  parent = archive.data["items"].get(item.get("parent"), {})
  if parent.get("path"):
    raw += (
      "\n[Back](" + link_to(parent["path"], item["path"]) + ")\n"
    ).encode()

  require(len(raw) <= TEXT_LIMIT, "generated page exceeds text limit")
  path = item["path"]

  if (
    not archive.exists(path)
    or signature(archive.path(path))["size"] != len(raw)
    or archive.read(path, TEXT_LIMIT) != raw
  ):
    archive.write(path, raw)
