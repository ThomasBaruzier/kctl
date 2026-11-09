from __future__ import annotations

import codecs
import copy
import hashlib
import os
import queue
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from .auth_html import approved_url, soup_html, text_of
from .core import (
  CTL,
  CTL_HOST,
  DOCUMENTS,
  EXTRACTED,
  FILE_LIMIT,
  HTML_LIMIT,
  HTML_SUFFIXES,
  LOGIN_POST_URL,
  OFFICE_SUFFIXES,
  TEXT_LIMIT,
  Cancelled,
  Failure,
  ProtectionFailure,
  SourceProblem,
  StorageFailure,
  bundle_name,
  explanation,
  fenced,
  identity,
  join_path,
  now,
  numbered,
  original_name,
  parent_path,
  reader_url,
  require,
  safe_name,
  token,
  valid_header,
  valid_id,
)
from .reader import (
  Home,
  Reader,
  clean_content,
  field_value,
  literal_calls,
  query_fields,
  source_url,
)
from .records import ATTACHMENT_ROLES
from .storage import (
  checked_file,
  remove_work,
  signature,
  staging_output,
  storage_io,
  verify_source,
)


@dataclass
class Event:
  kind: str
  data: dict[str, Any]


def validate_prefix(raw: bytes, suffix: str, image: bool) -> None:
  prefix = raw.removeprefix(codecs.BOM_UTF8).lstrip()[:512].lower()

  if suffix not in HTML_SUFFIXES:
    require(
      not prefix.startswith((b"<!doctype html", b"<html", b"<script")),
      "attachment returned HTML; run kctl login",
      1,
    )

  if suffix == ".pdf":
    require(raw.startswith(b"%PDF-"), "invalid PDF signature")

  if suffix in OFFICE_SUFFIXES | {".zip"}:
    require(raw.startswith(b"PK"), "invalid ZIP signature")

  if image:
    require(
      raw.startswith((
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF8",
        b"BM",
        b"II*\x00",
        b"MM\x00*",
      ))
      or raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
      "unsupported inline image signature",
    )


def validate_html_attachment(path: Path) -> None:
  with storage_io():
    with path.open("rb") as stream:
      checked_file(os.fstat(stream.fileno()))
      raw = stream.read(HTML_LIMIT)

  page = soup_html(raw)

  for form in page.find_all("form"):
    name = form.get("name")
    action = form.get("action", "")

    if name == "idLoginForm":
      try:
        approved_url(action, LOGIN_POST_URL, LOGIN_POST_URL)
      except Failure:
        continue

      require(
        form.select_one('input[name="loginName"]') is None
        or form.select_one('input[name="password"]') is None,
        "attachment returned the SSO login form; run kctl login",
        1,
      )

    if name == "mainForm":
      try:
        approved_url(action, CTL + "/Main.do", CTL + "/Main.do")
      except Failure:
        continue

      require(
        form.select_one('input[name="userType"][value="guest"]') is None,
        "attachment returned the CTL guest page; run kctl login",
        1,
      )

  require(
    "an error has occurred while processing" not in text_of(page).lower(),
    "attachment returned an application error",
  )


def response_validators(headers: Any) -> dict[str, str]:
  result = {}

  for source, target in (("ETag", "etag"), ("Last-Modified", "last_modified")):
    values = headers.get_all(source, [])
    if len(values) == 1 and values[0] and valid_header(values[0]):
      result[target] = values[0]

  return result


def response_length(headers: Any) -> int | None:
  values = headers.get_all("Content-Length", [])
  require(len(values) <= 1, "ambiguous attachment length")

  if not values:
    return None

  value = values[0].strip()
  require(
    value.isascii() and value.isdigit() and len(value) <= 20,
    "invalid attachment length",
  )
  length = int(value)
  require(0 < length <= FILE_LIMIT, "attachment is empty or exceeds size limit")
  return length


def _attachment_route(path: str) -> bool:
  return path == "/fileDownServlet" or path.startswith("/lmsdata/")


def _attachment_name(url: str, fallback: str) -> str:
  parts = urlsplit(url)

  if parts.path == "/fileDownServlet":
    name = field_value(query_fields(parts.query), "rFileName")
    return unquote(name.replace("*", "%")) if name else fallback or "attachment"

  return unquote(Path(parts.path).name) or fallback or "attachment"


def _attachment_label(tag: Any, name: str) -> None:
  tag.attrs.pop("onclick", None)
  tag.attrs.pop("href", None)

  if tag.name in {"input", "img"}:
    text = (
      tag.get("value") or tag.get("alt") or tag.get("aria-label")
      or tag.get("title") or name
    )
    tag.name = "span"
    tag.string = str(text)
  elif tag.name == "button":
    tag.name = "span"

  if not text_of(tag):
    tag.append(name)


def _table_scaffolding(node: Any) -> bool:
  from bs4 import Comment, NavigableString

  if isinstance(node, Comment):
    return True
  if isinstance(node, NavigableString):
    return not node.strip()

  return (
    node.name == "div"
    and not set(node.attrs) - {"class", "id"}
    and node.find(True) is None
    and not node.get_text(strip=True)
  )


def _summary_table_rows(table: Any) -> list[list[Any]] | None:
  if (
    "boardListInfo" not in table.get("class", [])
    or table.select("table, [rowspan], [colspan], pre, textarea")
  ):
    return None

  heads = table.find_all("thead", recursive=False)
  bodies = table.find_all("tbody", recursive=False)
  captions = table.find_all("caption", recursive=False)

  if len(heads) != 1 or len(bodies) != 1 or len(captions) > 1:
    return None

  head, body = heads[0], bodies[0]
  rows = head.find_all("tr", recursive=False)
  if len(rows) != 1:
    return None

  header = rows[0].find_all(["th", "td"], recursive=False)
  values = body.find_all("td", recursive=False)
  if (
    not header
    or not all(cell.name == "th" for cell in header)
    or len(values) != len(header)
  ):
    return None

  groups = (
    (table, [head, body, *captions]),
    (head, rows),
    (rows[0], header),
    (body, values),
  )
  for parent, children in groups:
    allowed = {id(child) for child in children}
    if any(
      id(child) not in allowed and not _table_scaffolding(child)
      for child in parent.children
    ):
      return None

  return [header, values]


def _table_text(table: Any, limit: int) -> str:
  from bs4 import Comment
  from markdownify import markdownify

  rows = _summary_table_rows(table)
  if rows is None:
    rows = [
      row.find_all(["th", "td"], recursive=False)
      for row in table.find_all("tr")
      if row.find_parent("table") is table
    ]
    rows = [row for row in rows if row]

  accounted = {id(cell) for row in rows for cell in row}
  caption = table.find("caption", recursive=False)

  if not rows or any(
    id(cell) not in accounted
    for cell in table.find_all(["th", "td"])
    if cell.find_parent("table") is table
  ) or any(
    not isinstance(text, Comment)
    and text.strip()
    and not any(
      id(parent) in accounted or parent is caption
      for parent in text.parents
    )
    for text in table.find_all(string=True)
  ):
    for anchor in table.select("a[href]"):
      target = str(anchor["href"])
      if target != anchor.get_text("", strip=True):
        anchor.append(f" ({target})")
    return fenced(table.get_text("\n", strip=False))

  complex_table = bool(table.select("[rowspan], [colspan], pre, table"))
  width = max(map(len, rows))
  has_header = all(cell.name == "th" for cell in rows[0])

  if not complex_table:
    line_count = len(rows) + (1 if has_header else 2)
    require(
      (3 * width + 2) * line_count + 3 * width <= limit,
      "page exceeds text limit",
    )

  prefix = (
    markdownify(
      caption.decode_contents(), heading_style="ATX", bullets="-"
    ).strip("\n") + "\n\n"
    if caption is not None else ""
  )
  values = [
    [
      markdownify(
        cell.decode_contents(), heading_style="ATX", bullets="-"
      ).strip("\n")
      for cell in row
    ]
    for row in rows
  ]

  if complex_table:
    return prefix + fenced(
      "\n\n".join(" | ".join(row) for row in values)
    )

  header = values.pop(0) if has_header else []
  lines = [header, ["---"] * width, *values]

  return prefix + "\n".join(
    "| " + " | ".join(
      value.replace("|", r"\|").replace("\r\n", "\n").replace("\n", "<br>")
      for value in row + [""] * (width - len(row))
    ) + " |"
    for row in lines
  ) + "\n"


def page_markdown(fragment: Any, problems: list[str]) -> str:
  from markdownify import markdownify

  media = {"iframe", "object", "embed", "canvas", "svg", "audio", "video"}

  for node in list(fragment.find_all(list(media))):
    if node.find_parent(list(media)) is not None:
      continue

    description = node.get("aria-label") or node.get("title") or text_of(node)
    node.replace_with(str(description))
    problems.append("embedded CTL content is not archived")

  for node in list(fragment.select(
    "head, script, style, input, select, textarea, button, source"
  )):
    if node.parent is not None:
      node.decompose()

  for form in list(fragment.select("form")):
    if form.parent is not None:
      form.unwrap()

  for anchor in list(fragment.select("a")):
    href = str(anchor.get("href", "")).strip()

    if not href or href.startswith("#"):
      anchor.unwrap()
      continue

    try:
      resolved = urljoin(CTL + "/", href)
      parts = urlsplit(resolved)
    except ValueError:
      anchor.unwrap()
      problems.append("malformed source link omitted")
      continue

    if parts.scheme not in {"http", "https", "mailto"}:
      anchor.unwrap()
    elif parts.hostname == CTL_HOST:
      try:
        target = reader_url(resolved)
        anchor["href"] = (
          target if _attachment_route(urlsplit(target).path)
          else source_url(target)
        )
      except Failure:
        anchor.unwrap()
        problems.append("unsupported CTL source link omitted")
    else:
      anchor["href"] = resolved

  tables = {}
  table_bytes = 0

  for table in list(fragment.select("table")):
    if table.find_parent("table") is not None:
      continue

    text = _table_text(table, TEXT_LIMIT - table_bytes)
    table_bytes += len(text.encode())
    require(table_bytes <= TEXT_LIMIT, "page exceeds text limit")
    key = "KCTLTABLE" + token()
    tables[key] = text
    replacement = fragment.new_tag("p")
    replacement.string = key
    table.replace_with(replacement)

  for node in fragment.find_all(True):
    node.attrs = {
      name: value
      for name, value in node.attrs.items()
      if name in {"href", "colspan", "rowspan"}
    }

  body = markdownify(
    str(fragment), heading_style="ATX", bullets="-"
  ).strip()

  for key, text in tables.items():
    require(body.count(key) == 1, "CTL table was lost during conversion")
    body = body.replace(key, text.rstrip("\n"))

  require(len(body.encode()) <= TEXT_LIMIT, "page exceeds text limit")
  return body


class Producer:
  def __init__(
    self,
    *,
    reader: Reader,
    root: Path,
    snapshot: dict[str, Any],
    options: Any,
    events: queue.Queue,
    stop: threading.Event,
    home: Home,
  ):
    self.reader = reader
    self.root = root
    self.items = snapshot["items"]
    self.documents = snapshot["documents"]
    self.options = options
    self.events = events
    self.stop = stop
    self.home = home
    self.seen_documents = set()
    self.downloaded = 0
    self.revalidated = 0
    self.reused = 0
    self.used = set()

    self.reserve("", ".ctl", DOCUMENTS, EXTRACTED)
    self.reserve(DOCUMENTS, "index.md", "dashboard.md")

    for item in self.items.values():
      for value in (item.get("folder"), item.get("path")):
        if value:
          self.used.add(value.casefold())

    self.used.update(
      doc["folder"].casefold() for doc in self.documents.values()
    )

  def emit(self, kind: str, **data: Any) -> None:
    event = Event(kind, copy.deepcopy(data))

    while not self.stop.is_set():
      try:
        self.events.put(event, timeout=0.1)
        return
      except queue.Full:
        pass

    raise Cancelled()

  def reserve(self, folder: str, *names: str) -> None:
    if folder:
      self.used.add(folder.casefold())

    self.used.update(
      join_path(folder, name).casefold() for name in names
    )

  def allocate(self, parent: str, title: str) -> str:
    name = safe_name(title)
    number = 1

    while True:
      candidate = join_path(parent, numbered(name, number))
      if (
        candidate.casefold() not in self.used
        and not os.path.lexists(self.root / candidate)
      ):
        self.used.add(candidate.casefold())
        return candidate
      number += 1

  def folder(self, key: str, parent: str, title: str) -> str:
    return self.items.get(key, {}).get("folder") or self.allocate(parent, title)

  def update(
    self,
    key: str,
    changes: dict[str, Any],
    body: str | None = None,
  ) -> None:
    self.items[key] = {**self.items.get(key, {}), **changes}
    self.emit("item", key=key, changes=changes, body=body)

  def prepare(
    self,
    key: str,
    title: str,
    path: str,
    parent: str | None,
    source: str = "",
  ) -> None:
    folder = parent_path(path)
    self.reserve(folder, Path(path).name)
    self.update(key, {
      "title": title,
      "path": path,
      "folder": folder,
      "parent": parent,
      "source": source or self.items.get(key, {}).get("source", ""),
      "status": "partial",
      "message": "retrieval incomplete",
    })

  def index(
    self,
    key: str,
    title: str,
    folder: str,
    children: list[str],
    source: str = "",
    complete: bool = True,
    parent: str | None = None,
    *,
    course_id: str | None = None,
  ) -> None:
    self.reserve(folder, "index.md")
    visible = list(children)

    if not complete:
      visible.extend(self.items.get(key, {}).get("children", []))

    changes = {
      "title": title,
      "folder": folder,
      "path": join_path(folder, "index.md"),
      "source": source,
      "status": ("archived" if children else "empty") if complete else "partial",
      "message": "" if complete else "incomplete listing",
      "children": list(dict.fromkeys(visible)),
      "related": [],
      "problems": [],
      "parent": parent,
    }

    if course_id is not None:
      require(valid_id(course_id), "invalid course identity")
      changes["course_id"] = course_id
      changes["include_submissions"] = self.options.include_submissions

    self.update(key, changes)

  def child(self, key: str, child: str) -> None:
    children = self.items[key]["children"]

    if child not in children:
      self.update(key, {"children": [*children, child]})

  def related(self, key: str, children: list[str]) -> None:
    self.update(key, {"related": list(dict.fromkeys(children))})

  def attempt(self, key: str, title: str, action: Any, *args: Any) -> None:
    try:
      action(*args)
    except (ProtectionFailure, StorageFailure):
      raise
    except Failure as error:
      self.update(key, {
        "title": title,
        "status": "failed",
        "message": error.message,
      })
      if error.code == 1:
        raise

  def _cached(
    self, key: str, role: str
  ) -> tuple[dict[str, Any] | None, bool, dict[str, str]]:
    old = self.documents.get(key)

    if old is None:
      return None, False, {}

    try:
      with storage_io():
        verified = verify_source(self.root, old)
    except SourceProblem:
      if not self.options.refresh:
        raise
      return old, False, {}

    old = {**old, "signature": verified, "role": role}
    headers = {}

    if old.get("etag"):
      headers["If-None-Match"] = old["etag"]
    elif old.get("last_modified"):
      headers["If-Modified-Since"] = old["last_modified"]

    return old, True, headers

  def _transfer(
    self,
    response: Any,
    incoming: Path,
    name: str,
    image: bool,
  ) -> tuple[int, str, str]:
    require(
      response.status not in {301, 302, 303, 307, 308},
      "attachment redirected; run kctl login",
      1,
    )
    require(
      response.status == 200,
      f"attachment returned HTTP {response.status}",
    )
    require(
      len(response.headers.get_all("Content-Type", [])) <= 1,
      "ambiguous attachment content type",
    )
    mime = response.headers.get_content_type()
    suffix = Path(name).suffix.lower()
    require(
      suffix in HTML_SUFFIXES
      or mime not in {"text/html", "application/xhtml+xml"},
      "attachment returned HTML; run kctl login",
      1,
    )

    expected = response_length(response.headers)
    size = 0
    prefix = bytearray()
    hasher = hashlib.sha256()

    with staging_output(incoming) as output:
      for chunk in self.reader.http.chunks(response, FILE_LIMIT, 600):
        prefix.extend(chunk[:max(0, 512 - len(prefix))])
        size += len(chunk)
        hasher.update(chunk)
        output.write(chunk)

      require(
        size > 0 and (expected is None or size == expected),
        "empty or incomplete attachment",
      )
      validate_prefix(bytes(prefix), suffix, image)

    if suffix in HTML_SUFFIXES:
      validate_html_attachment(incoming)

    return size, hasher.hexdigest(), mime

  def download(
    self,
    owner: str,
    folder: str,
    url: str,
    name: str,
    image: bool = False,
    *,
    role: str = "attachment",
  ) -> tuple[str, dict[str, Any]]:
    require(role in ATTACHMENT_ROLES, "invalid attachment role")
    require(
      role != "submission" or self.options.include_submissions,
      "submission downloads are excluded",
    )
    require(
      owner in self.items and self.items[owner].get("path"),
      "document owner has no assigned page",
    )
    require(
      folder == self.items[owner]["folder"],
      "attachment folder differs from its owner",
    )

    url = reader_url(url)
    require(
      _attachment_route(urlsplit(url).path),
      "unsupported attachment location",
    )
    key = identity(owner, url)

    if key in self.seen_documents:
      record = self.documents[key]
      require(record["role"] == role, "conflicting attachment roles")
      return key, record

    old, available, headers = self._cached(key, role)
    if old is not None:
      old = {**old, "name": name}

    if available and not headers and not self.options.refresh:
      record = {**old, "cache": "local", "seen": now()}
      self.reused += 1
      self.documents[key] = record
      self.seen_documents.add(key)
      self.emit("document", key=key, record=record, work=None)
      return key, record

    destination = (
      old["folder"] if old else self.allocate(folder, bundle_name(name))
    )
    original = old["original"] if old else original_name(name)
    work = ".ctl/work/" + token()
    path = self.root / work

    with storage_io():
      path.mkdir(mode=0o700)
      (path / "new").mkdir(mode=0o700)

    handed_off = False

    try:
      with self.reader.http.open(url, headers=headers) as response:
        if response.status == 304:
          require(available and headers, "unexpected not-modified response")
          record = {
            **old,
            "role": role,
            "cache": "revalidated",
            "checked": now(),
            "seen": now(),
            **response_validators(response.headers),
          }
          self.revalidated += 1
          self.emit("document", key=key, record=record, work=None)

        else:
          incoming = path / "new" / original
          size, sha256, mime = self._transfer(
            response, incoming, name, image
          )
          with storage_io():
            current = signature(incoming)

          record = {
            "owner": owner,
            "url": url,
            "name": name,
            "folder": destination,
            "original": original,
            "role": role,
            "type": mime,
            "bytes": size,
            "sha256": sha256,
            "signature": current,
            "cache": "downloaded",
            "seen": now(),
            "checked": now(),
            "result": None,
            "attempts": {},
            **response_validators(response.headers),
          }
          self.downloaded += 1

          if available and sha256 == old["sha256"]:
            record["signature"] = old["signature"]
            self.emit("document", key=key, record=record, work=None)
          else:
            self.emit("document", key=key, record=record, work=work)
            handed_off = True

      self.documents[key] = record
      self.seen_documents.add(key)
      return key, record

    finally:
      if not handed_off:
        with storage_io():
          remove_work(self.root, work)

  def _capture(
    self,
    key: str,
    folder: str,
    url: str,
    name: str,
    role: str,
    documents: list[str],
    problems: list[str],
    *,
    image: bool = False,
  ) -> None:
    doc_key = None

    try:
      url = reader_url(url)
      doc_key = identity(key, url)
      if doc_key in documents:
        return
      self.download(key, folder, url, name, image, role=role)

    except (ProtectionFailure, StorageFailure):
      raise
    except Failure as error:
      if error.code == 1:
        raise

      problems.append(f"{name}: {error.message}")
      previous = self.documents.get(doc_key)
      if previous is None or previous["role"] != role:
        return

    documents.append(doc_key)

  def _attachments(
    self,
    fragment: Any,
    key: str,
    folder: str,
    role: str,
    enabled: bool,
    documents: list[str],
    problems: list[str],
    *,
    calls: list[tuple[Any, ...]],
  ) -> bool:
    found = bool(calls)

    for tag, display_name, stored, storage in calls:
      name = (
        unquote(display_name.replace("*", "%")) or text_of(tag) or "attachment"
      )
      _attachment_label(tag, name)

      if enabled:
        url = CTL + "/fileDownServlet?" + urlencode({
          "rFileName": display_name,
          "sFileName": stored,
          "filePath": storage,
        })
        self._capture(
          key, folder, url, name, role, documents, problems
        )

    for tag in list(fragment.select("a[href]")):
      try:
        url = reader_url(str(tag["href"]).strip())
      except Failure:
        continue

      if not _attachment_route(urlsplit(url).path):
        continue

      found = True
      title = text_of(tag)
      try:
        name = _attachment_name(url, title)
      except Failure as error:
        _attachment_label(tag, title or "attachment")
        if enabled:
          problems.append(error.message)
        continue

      _attachment_label(tag, title or name)
      if enabled:
        self._capture(
          key, folder, url, name, role, documents, problems
        )

    return found

  def _images(
    self,
    fragment: Any,
    key: str,
    folder: str,
    role: str,
    enabled: bool,
    documents: list[str],
    problems: list[str],
    *,
    controls: set[int],
  ) -> bool:
    found = False

    for picture in list(fragment.select("picture")):
      if picture.find("img") is None:
        found = True
        if enabled:
          problems.append("picture has no supported fallback image")

      for source in list(picture.find_all("source")):
        source.decompose()
      picture.unwrap()

    for image in list(fragment.select("img")):
      if id(image) in controls:
        continue

      if (
        str(image.get("aria-hidden", "")).lower() == "true"
        or str(image.get("role", "")).lower() in {"none", "presentation"}
      ):
        image.decompose()
        continue

      found = True
      description = (
        image.get("alt") or image.get("aria-label") or image.get("title") or ""
      )
      source = str(image.get("src", "")).strip()
      image.replace_with(str(description))

      if not enabled:
        continue

      try:
        require(source, "inline image has no supported source URL")
        url = reader_url(source)
        require(
          _attachment_route(urlsplit(url).path),
          "inline image is outside supported CTL storage",
        )
        name = _attachment_name(url, "image")
      except Failure as error:
        problems.append("inline image not archived: " + error.message)
        continue

      self._capture(
        key,
        folder,
        url,
        name,
        role,
        documents,
        problems,
        image=True,
      )

    return found

  def content(
    self,
    key: str,
    title: str,
    path: str,
    nodes: list[Any],
    source: str,
    include_files: bool = True,
    status: str = "archived",
    limitations: list[str] | None = None,
    parent: str | None = None,
    *,
    role: str = "attachment",
    empty_listing: bool = False,
  ) -> None:
    require(role in ATTACHMENT_ROLES, "invalid attachment role")
    self.prepare(key, title, path, parent, source)
    require(nodes, "content region missing")

    folder = parent_path(path)
    fragment = clean_content(soup_html("\n".join(str(node) for node in nodes)))
    problems = list(limitations or [])
    documents = []
    enabled = include_files and (
      role != "submission" or self.options.include_submissions
    )
    calls = literal_calls(fragment, "fileDownload", 3)
    images = self._images(
      fragment,
      key,
      folder,
      role,
      enabled,
      documents,
      problems,
      controls={id(call[0]) for call in calls},
    )
    attachments = self._attachments(
      fragment, key, folder, role, enabled, documents, problems, calls=calls
    )
    body = page_markdown(fragment, problems)
    require(
      body or attachments or images or problems,
      "content region contains no extractable source content",
    )

    problems = list(dict.fromkeys(problems))
    if problems and status in {"archived", "empty"}:
      status = "partial"

    self.update(key, {
      "title": title,
      "folder": folder,
      "path": path,
      "source": source,
      "status": status,
      "message": "",
      "problems": problems,
      "documents": list(dict.fromkeys(documents)),
      "related": [],
      "parent": parent,
      "empty_listing": empty_listing and not (problems or attachments or images),
    }, body or "\n")

  def dashboard(self) -> None:
    url = CTL + "/Study.do?cmd=viewStudyMyClassroom"
    path = DOCUMENTS + "/dashboard.md"
    self.prepare("dashboard", "My Classroom", path, "index", url)
    page = self.reader.page(url, "studyForm")
    nodes = page.select("#PORTLET_0002, #PORTLET_0003, #PORTLET_0005")
    require(len(nodes) == 3, "unsupported classroom dashboard")
    self.content(
      "dashboard",
      "My Classroom",
      path,
      nodes,
      url,
      parent="index",
    )

  def statistics(self, started: float) -> dict[str, Any]:
    return {
      "requests": self.reader.http.requests,
      "bytes": self.reader.http.bytes,
      "request_operation_seconds": round(self.reader.http.seconds, 3),
      "elapsed_seconds": round(time.monotonic() - started, 3),
      "downloaded": self.downloaded,
      "revalidated": self.revalidated,
      "reused": self.reused,
    }

  def _course(
    self, traversal: Any, course_id: str, title: str
  ) -> None:
    key = "course/" + course_id
    folder = self.folder(key, DOCUMENTS, title)
    self.reader.course = course_id
    self.index(
      key,
      title,
      folder,
      [],
      complete=False,
      parent="index",
      course_id=course_id,
    )
    traversal.run(course_id, title)

  def run(self) -> None:
    from .courses import CourseTraversal

    started = time.monotonic()

    try:
      require(valid_id(self.home.account), "invalid verified archive account")
      courses = self.home.courses
      require(
        courses and all(valid_id(course_id) for course_id in courses),
        "invalid enrolled course inventory",
      )
      selected = set(self.options.course or courses)
      require(selected <= courses.keys(), "requested course is not enrolled")
      traversal = CourseTraversal(self)
      children = ["dashboard"]

      self.index(
        "index", "CTL archive", DOCUMENTS, children, complete=False
      )
      self.attempt("dashboard", "My Classroom", self.dashboard)

      for course_id, title in courses.items():
        if self.stop.is_set():
          raise Cancelled()

        key = "course/" + course_id

        if course_id not in selected:
          if key in self.items:
            children.append(key)
          continue

        children.append(key)
        self.child("index", key)
        self.emit("message", text="course: " + title)
        self.attempt(
          key, title, self._course, traversal, course_id, title
        )

      self.index("index", "CTL archive", DOCUMENTS, children)
      self.emit(
        "done", status="complete", code=0, **self.statistics(started)
      )

    except Cancelled:
      pass

    except Exception as error:
      try:
        self.emit(
          "done",
          status="failed",
          code=error.code if isinstance(error, Failure) else 2,
          message=explanation(error),
          fatal_local=isinstance(error, (StorageFailure, ProtectionFailure)),
          **self.statistics(started),
        )
      except Cancelled:
        pass

    finally:
      self.reader.course = ""
