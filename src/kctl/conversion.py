from __future__ import annotations

import base64
import importlib.metadata
import io
import posixpath
import re
import warnings
import zipfile

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .auth_html import soup_html, text_of
from .content import Markdown, Output
from .core import (
  ASSET_LIMIT,
  DEPTH_LIMIT,
  ENTRY_LIMIT,
  FILE_LIMIT,
  HTML_DOCUMENT_LIMIT,
  IMAGE_LIMIT,
  IMAGE_SUFFIXES,
  OFFICE_SUFFIXES,
  PAGE_LIMIT,
  PIXEL_LIMIT,
  TEXT_LIMIT,
  BudgetReached,
  Cancelled,
  Failure,
  LimitReached,
  ProtectionFailure,
  StorageFailure,
  digest,
  display,
  explanation,
  fenced,
  join_path,
  label,
  link_to,
  require,
  retryable,
  token,
)
from .records import applicable, outcome
from .storage import (
  checked_file,
  decoder_input,
  read_private,
  storage_io,
)


VECTOR_SUFFIXES = frozenset({".svg", ".wmf", ".emf"})
RASTER_MIMES = frozenset({
  "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
  "image/bmp", "image/x-ms-bmp", "image/tif", "image/tiff",
})
IMAGE_TOKENS = re.compile(
  r"(?P<ticks>`+)"
  r"|!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
  r"\(\s*(?:<[^>\n]*>|(?:\\.|[^\\)\n])*)\)"
)
FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HTML_VOID_TAGS = frozenset({
  "area", "base", "br", "col", "embed", "hr", "img", "input",
  "link", "meta", "param", "source", "track", "wbr",
})
ANNOTATION_LIMIT = 1000
PDF_OBJECT_LIMIT = 10000
PDF_FORM_DEPTH = 16
ANNOTATIONS = {
  1: "Text",
  3: "FreeText",
  4: "Line",
  5: "Square",
  6: "Circle",
  7: "Polygon",
  8: "PolyLine",
  9: "Highlight",
  10: "Underline",
  11: "Squiggly",
  12: "StrikeOut",
  13: "Stamp",
  14: "Caret",
  15: "Ink",
  16: "Popup",
  17: "FileAttachment",
  18: "Sound",
  19: "Movie",
  20: "Widget",
}
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_NAMESPACES = {
  "http://schemas.openxmlformats.org/presentationml/2006/main": (
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
  ),
  "http://purl.oclc.org/ooxml/presentationml/main": (
    "http://purl.oclc.org/ooxml/drawingml/main",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
  ),
}


class _PackageError(Failure):
  pass


def _integrity_failure(error: BaseException) -> bool:
  from markitdown import FileConversionException

  pending = [error]
  seen = set()

  while pending:
    current = pending.pop()
    if id(current) in seen or len(seen) >= 64:
      return False
    seen.add(id(current))

    if isinstance(current, zipfile.BadZipFile):
      continue

    if isinstance(current, FileConversionException):
      if not current.attempts:
        return False
      pending.extend(attempt.exc_info[1] for attempt in current.attempts)
    elif isinstance(current, BaseException) and current.__cause__ is not None:
      pending.append(current.__cause__)
    else:
      return False

  return True


def _office_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
  members = archive.infolist()
  require("[Content_Types].xml" in archive.namelist(), "invalid Office container")

  if len(members) > 10000:
    raise LimitReached("Office container has too many entries")
  if len({member.filename for member in members}) != len(members):
    raise _PackageError("Office package contains ambiguous duplicate members")
  if sum(member.file_size for member in members) > ASSET_LIMIT:
    raise LimitReached("Office expanded size exceeds root byte limit")
  if any(member.flag_bits & 1 for member in members):
    raise LimitReached("encrypted Office container is unsupported")

  return members


class _Package:
  def __init__(self, archive: zipfile.ZipFile):
    self.archive = archive
    self.members = _office_members(archive)
    self.used = 0

  def chunks(self, member: zipfile.ZipInfo, limit: int):
    name = display(member.filename, 256)
    if member.file_size > limit:
      raise LimitReached(f"{name}: Office member exceeds read limit")
    if self.used + member.file_size > ASSET_LIMIT:
      raise LimitReached("Office inspection reaches total read limit")

    size = 0
    try:
      with self.archive.open(member) as source:
        while True:
          amount = min(
            128 * 1024,
            limit - size + 1,
            ASSET_LIMIT - self.used + 1,
          )
          raw = source.read(amount)
          if not raw:
            break

          size += len(raw)
          self.used += len(raw)
          if size > limit or self.used > ASSET_LIMIT:
            raise LimitReached("Office inspection reaches decoded-byte limit")
          yield raw
    except zipfile.BadZipFile as error:
      raise _PackageError(
        f"{name}: ZIP integrity check failed: {display(error, 512)}"
      ) from None

    if size != member.file_size:
      raise _PackageError(f"{name}: ZIP member size mismatch")

  def read(self, name: str, limit: int = FILE_LIMIT) -> bytes:
    try:
      member = self.archive.getinfo(name)
    except KeyError:
      raise _PackageError(
        "Office package member missing: " + display(name, 256)
      ) from None

    if member.is_dir():
      raise _PackageError("Office package reference points to a directory")
    return b"".join(self.chunks(member, limit))

  def xml(self, name: str) -> Any:
    from defusedxml import ElementTree
    from defusedxml.common import DefusedXmlException
    from xml.etree.ElementTree import ParseError

    raw = self.read(name, TEXT_LIMIT)
    try:
      return ElementTree.fromstring(
        raw,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
      )
    except (DefusedXmlException, ParseError):
      raise _PackageError(
        "Office XML is malformed or unsafe: " + display(name, 256)
      ) from None

  def verify(self) -> list[_PackageError]:
    errors = []

    for member in self.members:
      if member.is_dir():
        continue
      try:
        for _ in self.chunks(member, FILE_LIMIT):
          pass
      except _PackageError as error:
        errors.append(error)

    return errors


def _package_target(part: str, target: str) -> str:
  try:
    url = urlsplit(target)
    path = unquote(url.path, errors="strict")
  except (ValueError, UnicodeError):
    raise _PackageError("invalid Office relationship target") from None

  if (
    not target
    or url.scheme
    or url.netloc
    or url.query
    or url.fragment
    or "\\" in path
    or any(ord(character) < 32 or ord(character) == 127 for character in path)
  ):
    raise _PackageError("unsupported Office relationship target")

  components = [] if path.startswith("/") else posixpath.dirname(part).split("/")
  components = [component for component in components if component]

  for component in path.split("/"):
    if component in {"", "."}:
      continue
    if component == "..":
      if not components:
        raise _PackageError("Office relationship escapes the package")
      components.pop()
    else:
      components.append(component)

  if not components:
    raise _PackageError("Office relationship has no member target")

  return "/".join(components)


def _relationships(package: _Package, part: str, kind: str) -> dict[str, str]:
  name = (
    posixpath.join(
      posixpath.dirname(part),
      "_rels",
      posixpath.basename(part) + ".rels",
    )
    if part else "_rels/.rels"
  )
  root = package.xml(name)
  if root.tag != f"{{{PACKAGE_NS}}}Relationships":
    raise _PackageError("unsupported Office relationships XML")

  types = {
    relationship + "/" + kind
    for _, relationship in OFFICE_NAMESPACES.values()
  }
  seen = set()
  selected = {}

  for relation in root:
    if relation.tag != f"{{{PACKAGE_NS}}}Relationship":
      raise _PackageError("unsupported Office relationship entry")

    key = relation.get("Id")
    if not key or key in seen:
      raise _PackageError("ambiguous Office relationship identity")
    seen.add(key)

    if relation.get("Type") not in types:
      continue
    if relation.get("TargetMode", "Internal") != "Internal":
      raise _PackageError("essential Office relationship is external")

    selected[key] = _package_target(part, relation.get("Target", ""))

  return selected


def _pptx_order(package: _Package) -> tuple[list[str], dict[str, str]]:
  roots = _relationships(package, "", "officeDocument")
  if len(roots) != 1:
    raise _PackageError("presentation root relationship is missing or ambiguous")

  part = next(iter(roots.values()))
  presentation = package.xml(part)
  namespace = presentation.tag.removeprefix("{").split("}", 1)[0]

  if (
    namespace not in OFFICE_NAMESPACES
    or presentation.tag != f"{{{namespace}}}presentation"
  ):
    raise _PackageError("unsupported presentation XML namespace")

  drawing, relationship = OFFICE_NAMESPACES[namespace]
  namespaces = {"p": namespace, "a": drawing, "r": relationship}
  listing = presentation.find("p:sldIdLst", namespaces)
  if listing is None:
    raise _PackageError("presentation slide order is unavailable")
  if len(listing) > PAGE_LIMIT:
    raise LimitReached("presentation exceeds slide recovery limit")

  targets = _relationships(package, part, "slide")
  slides = []
  identifiers = set()

  for entry in listing:
    key = entry.get(f"{{{relationship}}}id")
    identifier = entry.get("id")
    if (
      entry.tag != f"{{{namespace}}}sldId"
      or not identifier
      or identifier in identifiers
      or key not in targets
      or targets[key] in slides
    ):
      raise _PackageError("presentation slide order is ambiguous or incomplete")

    identifiers.add(identifier)
    slides.append(targets[key])

  return slides, namespaces


def _pptx_breaks(text: str) -> str:
  lines = text.split("\n")
  fence = ""

  for index, line in enumerate(lines):
    opening = FENCE_LINE.match(line)
    if opening:
      if not fence:
        fence = opening[1]
      elif opening[1][0] == fence[0] and len(opening[1]) >= len(fence):
        fence = ""
    separator = "<br>" if not fence and line.startswith("|") else "\n"
    lines[index] = line.replace("\v", separator)

  return "\n".join(lines)


def _paragraph(paragraph: Any, drawing: str) -> str:
  parts = []

  for child in paragraph:
    if child.tag == f"{{{drawing}}}br":
      parts.append("\n")
    elif child.tag in {f"{{{drawing}}}r", f"{{{drawing}}}fld"}:
      text = child.find(f"{{{drawing}}}t")
      if text is not None:
        parts.append(text.text or "")

  return "".join(parts)


def _smartart_findings(slide: Any):
  diagrams = {
    namespace.rsplit("/", 1)[0] + "/diagram"
    for namespace, _ in OFFICE_NAMESPACES.values()
  }
  graphics = {
    f"{{{namespace}}}graphicData" for namespace, _ in OFFICE_NAMESPACES.values()
  }
  frames = 0
  pending = [(slide, 0)]
  visited = 0

  while pending:
    node, depth = pending.pop()
    visited += 1
    if visited > ENTRY_LIMIT * 100 or depth > DEPTH_LIMIT * 16:
      raise LimitReached("SmartArt inspection reaches XML work limit")
    if len(node) + len(pending) > ENTRY_LIMIT * 100:
      raise LimitReached("SmartArt inspection reaches XML work limit")
    pending.extend((child, depth + 1) for child in reversed(list(node)))
    if node.tag not in graphics or node.get("uri") not in diagrams:
      continue

    frames += 1
    if frames > ENTRY_LIMIT:
      raise LimitReached("SmartArt inspection reaches frame limit")
    yield (
      f"SmartArt frame {frames}: diagram text is not extracted; "
      "diagram geometry and reading order are not reconstructed"
    )


def _slide_blocks(slide: Any, namespaces: dict[str, str]):
  presentation = namespaces["p"]
  drawing = namespaces["a"]

  if slide.tag != f"{{{presentation}}}sld":
    raise _PackageError("unsupported slide XML")
  tree = slide.find("p:cSld/p:spTree", namespaces)
  if tree is None:
    raise _PackageError("slide shape tree is unavailable")

  pending = list(reversed(list(tree)))

  while pending:
    shape = pending.pop()

    if shape.tag == f"{{{presentation}}}grpSp":
      pending.extend(reversed(list(shape)))
    elif shape.tag == f"{{{presentation}}}sp":
      for paragraph in shape.findall("p:txBody/a:p", namespaces):
        text = _paragraph(paragraph, drawing)
        if text.strip():
          yield text
    elif shape.tag == f"{{{presentation}}}graphicFrame":
      table = shape.find("a:graphic/a:graphicData/a:tbl", namespaces)
      if table is None:
        continue

      rows = []
      for row in table.findall("a:tr", namespaces):
        cells = [
          "\n".join(
            _paragraph(paragraph, drawing)
            for paragraph in cell.findall("a:txBody/a:p", namespaces)
          )
          for cell in row.findall("a:tc", namespaces)
        ]
        rows.append(" | ".join(cells))

      text = "\n\n".join(rows)
      if text.strip():
        yield fenced(text)


def _suspect_codes(text: str) -> Counter:
  return Counter(
    code
    for character in text
    if (
      (code := ord(character)) == 0xFFFD
      or 0xFDD0 <= code <= 0xFDEF
      or code & 0xFFFF in {0xFFFE, 0xFFFF}
      or code < 0x20 and code not in (9, 10, 13)
    )
  )


def _page_ranges(pages: set[int]) -> str:
  ranges = []
  start = previous = None

  for page in sorted(pages):
    if previous is not None and page != previous + 1:
      ranges.append(str(start) if start == previous else f"{start}-{previous}")
      start = page
    if start is None:
      start = page
    previous = page

  if start is not None:
    ranges.append(str(start) if start == previous else f"{start}-{previous}")

  text = ", ".join(ranges[:30])
  if len(ranges) > 30:
    text += f", ... ({len(pages)} pages total)"
  return text


def _annotations(page: Any) -> set[str]:
  import pypdfium2 as pdfium

  count = pdfium.raw.FPDFPage_GetAnnotCount(page)
  require(count >= 0, "PDF annotation count unavailable")
  if count > ANNOTATION_LIMIT:
    raise LimitReached("PDF annotation inspection limit reached")

  kinds = set()

  for index in range(count):
    annotation = pdfium.raw.FPDFPage_GetAnnot(page, index)
    require(annotation, "PDF annotation unavailable")

    try:
      subtype = pdfium.raw.FPDFAnnot_GetSubtype(annotation)
      if subtype != 2:
        kinds.add(ANNOTATIONS.get(subtype, f"type {subtype}"))
    finally:
      pdfium.raw.FPDFPage_CloseAnnot(annotation)

  return kinds


def _pdf_vectors(page: Any) -> bool:
  import pypdfium2 as pdfium

  raw = pdfium.raw
  pending = [(page, 0)]
  inspected = 0

  while pending:
    parent, depth = pending.pop()
    count = (
      raw.FPDFFormObj_CountObjects(parent)
      if depth else raw.FPDFPage_CountObjects(parent)
    )
    require(count >= 0, "PDF object count unavailable")
    inspected += count
    if inspected > PDF_OBJECT_LIMIT:
      raise LimitReached("PDF vector inspection object limit reached")

    for index in range(count):
      obj = (
        raw.FPDFFormObj_GetObject(parent, index)
        if depth else raw.FPDFPage_GetObject(parent, index)
      )
      require(obj, "PDF object unavailable")
      kind = raw.FPDFPageObj_GetType(obj)
      require(kind != 0, "PDF object type unavailable")

      if kind == raw.FPDF_PAGEOBJ_PATH:
        return True
      if kind == raw.FPDF_PAGEOBJ_FORM:
        if depth >= PDF_FORM_DEPTH:
          raise LimitReached("PDF vector inspection nesting limit reached")
        pending.append((obj, depth + 1))

  return False


def processing_error(error: BaseException) -> BaseException:
  from PIL import Image

  if isinstance(
    error, (Image.DecompressionBombWarning, Image.DecompressionBombError)
  ):
    return LimitReached("image exceeds decoder pixel limit")

  return error


def normalize_image(image: Any) -> Any:
  from PIL import Image, ImageOps

  if (
    image.width <= 0
    or image.height <= 0
    or image.width * image.height > PIXEL_LIMIT
  ):
    raise LimitReached("image exceeds pixel limit")

  oriented = image

  try:
    if image.getexif().get(274, 1) not in {0, 1}:
      oriented = ImageOps.exif_transpose(image)

    if "A" in oriented.getbands() or "transparency" in oriented.info:
      with oriented.convert("RGBA") as rgba:
        result = Image.new("RGB", rgba.size, "white")
        with rgba.getchannel("A") as alpha:
          try:
            result.paste(rgba, mask=alpha)
          except BaseException:
            result.close()
            raise
    else:
      result = oriented.convert("RGB")

    result.info.clear()
    return result

  finally:
    if oriented is not image:
      oriented.close()


def image_descriptions(text: str) -> str:
  result = []
  fence = ""
  inline = 0

  for line in text.splitlines(keepends=True):
    opening = FENCE_LINE.match(line)

    if fence:
      result.append(line)
      if (
        opening
        and opening[1][0] == fence[0]
        and len(opening[1]) >= len(fence)
        and not opening[2].strip()
      ):
        fence = ""
      continue

    if opening and inline == 0:
      fence = opening[1]
      result.append(line)
      continue

    position = 0
    for match in IMAGE_TOKENS.finditer(line):
      result.append(line[position:match.start()])

      if match["ticks"] is not None:
        length = len(match["ticks"])
        if inline == 0:
          inline = length
        elif inline == length:
          inline = 0
        result.append(match[0])
      elif inline:
        result.append(match[0])
      else:
        result.append(match["alt"])

      position = match.end()

    result.append(line[position:])

  return "".join(result)


class _MermaidEscapes(HTMLParser):
  def __init__(self, source: str):
    super().__init__(convert_charrefs=False)
    self.source = source
    self.stack: list[tuple[str, bool]] = []
    self.replacements: list[tuple[int, int]] = []
    self.line = 1
    self.line_start = 0

  def position(self) -> int:
    line, column = self.getpos()
    while self.line < line:
      self.line_start = self.source.index("\n", self.line_start) + 1
      self.line += 1
    return self.line_start + column

  def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
    active = bool(self.stack and self.stack[-1][1])
    spelling = self.get_starttag_text()

    if active and re.fullmatch(r"<[A-Za-z_][A-Za-z0-9_]*>", spelling):
      start = self.position()
      end = start + len(spelling)
      if (
        self.source[max(0, start - 4):start] == "&lt;"
        and self.source[end:end + 4] == "&gt;"
      ):
        self.replacements.append((start, end))
        return

    attributes = dict(attrs)
    classes = (attributes.get("class") or "").split()
    mermaid = (
      "mermaid" in classes
      or tag in {"pre", "code"} and "language-mermaid" in classes
    )
    if tag not in HTML_VOID_TAGS:
      self.stack.append((tag, active or mermaid))

  def handle_endtag(self, tag: str) -> None:
    for index in range(len(self.stack) - 1, -1, -1):
      if self.stack[index][0] == tag:
        del self.stack[index:]
        return


def _mermaid_markup(source: str) -> str:
  if "&lt;<" not in source:
    return source

  parser = _MermaidEscapes(source)
  parser.feed(source)
  parser.close()
  parts = []
  position = 0

  for start, end in parser.replacements:
    parts.extend((
      source[position:start],
      "&lt;",
      source[start + 1:end - 1],
      "&gt;",
    ))
    position = end

  parts.append(source[position:])
  return "".join(parts)


@dataclass
class HTMLContent:
  markup: bytes
  literals: dict[str, str]
  images: list[tuple[str, str]]
  problems: list[str]


def _block_language(node: Any) -> str:
  classes = list(node.get("class", []))
  code = node.find("code")

  if code is not None:
    classes.extend(code.get("class", []))

  if (
    "mermaid" in classes
    or "language-mermaid" in classes
    or str(node.get("type", "")).lower() == "text/vnd.mermaid"
  ):
    return "mermaid"

  for value in classes:
    if value.startswith("language-"):
      language = value.removeprefix("language-")
      if re.fullmatch(r"[A-Za-z0-9_+.-]{1,32}", language):
        return language

  return ""


def _protect_blocks(page: Any) -> dict[str, str]:
  literals = {}
  blocks = page.select(
    "pre, textarea, .mermaid, code.language-mermaid, "
    'script[type="text/vnd.mermaid"]'
  )
  selected = {id(node) for node in blocks}

  for block in blocks:
    if any(id(parent) in selected for parent in block.parents):
      continue

    for linebreak in list(block.find_all("br")):
      linebreak.replace_with("\n")

    key = "KCTLHTMLCODE" + token()
    literals[key] = fenced(
      block.get_text("", strip=False), _block_language(block)
    )
    replacement = page.new_tag("p")
    replacement.string = key
    block.replace_with(replacement)

  return literals


def _protect_tables(page: Any, literals: dict[str, str]) -> None:
  from markdownify import markdownify

  for table in list(page.find_all("table")):
    if table.find_parent("table") is not None:
      continue

    key = "KCTLHTMLTABLE" + token()
    literals[key] = markdownify(
      str(table), heading_style="ATX", bullets="-"
    ).strip("\n")
    replacement = page.new_tag("p")
    replacement.string = key
    table.replace_with(replacement)


def _html_blocks(page: Any) -> None:
  for node in page.select(
    "div.card > strong + span, "
    "details.project > summary > span.number + span, "
    "header .faculty > strong + span, "
    "header .faculty > span + span, "
    'nav > a[href^="#"] + a[href^="#"], '
    "header > div.meta > span + span, "
    ".chart > .legend > span + span"
  ):
    if node.find_parent("code") is None:
      node.insert_before(page.new_tag("br"))

  for node in page.select("main, article, section, header, footer, aside, nav"):
    node.name = "div"


def _html_media(page: Any) -> list[str]:
  problems = []

  for node in list(page.select(
    "picture, iframe, object, embed, canvas, audio, video"
  )):
    if node.parent is None:
      continue

    if node.name == "picture":
      if (
        str(node.get("aria-hidden", "")).lower() == "true"
        or str(node.get("role", "")).lower() in {"none", "presentation"}
      ):
        node.decompose()
        continue
      if node.find("img") is None:
        problems.append("HTML picture has no supported fallback image")
    else:
      problems.append("embedded HTML content is not executed or fetched")

    description = node.get("aria-label") or node.get("title")
    if description and str(description).strip() != text_of(node):
      paragraph = page.new_tag("p")
      paragraph.string = str(description)
      node.insert(0, paragraph)

    node.unwrap()

  return list(dict.fromkeys(problems))


def _html_images(
  page: Any,
) -> tuple[list[tuple[str, str]], list[str]]:
  images = []
  problems = []

  for image in list(page.find_all("img")):
    decorative = (
      str(image.get("aria-hidden", "")).lower() == "true"
      or str(image.get("role", "")).lower() in {"none", "presentation"}
    )
    if decorative:
      image.decompose()
      continue

    source = str(image.get("src", "")).strip()
    description = str(
      image.get("alt") or image.get("aria-label") or image.get("title") or ""
    )

    if source.lower().startswith("data:"):
      if len(images) >= IMAGE_LIMIT:
        problems.append("HTML image-reference limit reached")
      elif len(source) > FILE_LIMIT * 4 // 3 + 1024:
        problems.append("embedded HTML image exceeds size limit")
      else:
        images.append((
          source,
          display(description or f"Embedded image {len(images) + 1}", 256),
        ))
    elif source or image.get("srcset"):
      problems.append("linked HTML images are not fetched")

    image.replace_with(description)

  for svg in list(page.find_all("svg")):
    if svg.parent is None:
      continue

    decorative = str(svg.get("aria-hidden", "")).lower() == "true"
    description = "" if decorative else str(
      svg.get("aria-label") or text_of(svg)
    )
    if not decorative:
      problems.append("inline SVG is not rasterized or OCRed")
    svg.replace_with(description)

  return images, list(dict.fromkeys(problems))[:20]


def prepare_html(raw: bytes) -> HTMLContent:
  from bs4 import Comment, UnicodeDammit

  if len(raw) > HTML_DOCUMENT_LIMIT:
    raise LimitReached("HTML exceeds static-processing input limit")

  decoded = UnicodeDammit(raw, is_html=True)
  require(
    decoded.unicode_markup is not None
    and not decoded.contains_replacement_characters,
    "HTML encoding could not be decoded without replacement",
  )
  page = soup_html(_mermaid_markup(decoded.unicode_markup))
  title = text_of(page.title)
  headings = {
    text_of(node).casefold() for node in page.select("h1, h2, h3, h4, h5, h6")
  }

  for node in list(page.select("head, style, template")):
    if node.parent is not None:
      node.decompose()

  for script in list(page.find_all("script")):
    if str(script.get("type", "")).lower() != "text/vnd.mermaid":
      script.decompose()

  for node in list(page.find_all(string=lambda value: isinstance(value, Comment))):
    node.extract()

  problems = _html_media(page)
  literals = _protect_blocks(page)
  _html_blocks(page)
  images, image_problems = _html_images(page)
  problems.extend(image_problems)

  for node in list(page.select("input, button, link, meta, base, source")):
    if node.parent is not None:
      node.decompose()

  for anchor in list(page.find_all("a")):
    href = str(anchor.get("href", "")).strip()

    if (
      href.startswith("#")
      and text_of(anchor) in {"¶", "#"}
      and anchor.find_parent(re.compile(r"^h[1-6]$")) is not None
    ):
      anchor.decompose()
      continue

    if re.match(r"(?i)^(?:data|javascript|vbscript):", href):
      anchor.unwrap()

  for node in page.find_all(True):
    node.attrs = {
      name: value
      for name, value in node.attrs.items()
      if name in {"href", "colspan", "rowspan"}
    }

  _protect_tables(page, literals)
  content = page.body or page

  if title and title.casefold() not in headings:
    heading = page.new_tag("h1")
    heading.string = title
    content.insert(0, heading)

  markup = str(content).encode()
  if len(markup) > HTML_DOCUMENT_LIMIT:
    raise LimitReached("prepared HTML exceeds static-processing input limit")

  return HTMLContent(
    markup,
    literals,
    images,
    list(dict.fromkeys(problems))[:20],
  )


def embedded_raster(uri: str) -> bytes:
  header, separator, payload = uri.partition(",")
  fields = header.lower().split(";")

  if (
    not separator
    or fields[0].removeprefix("data:") not in RASTER_MIMES
    or not fields[0].startswith("data:")
    or fields[1:] != ["base64"]
  ):
    raise LimitReached("unsupported embedded HTML image format or encoding")

  if len(payload) > FILE_LIMIT * 4 // 3 + 8:
    raise LimitReached("embedded HTML image exceeds size limit")

  try:
    raw = base64.b64decode("".join(payload.split()), validate=True)
  except (ValueError, UnicodeError):
    raise LimitReached("malformed embedded HTML image") from None

  if len(raw) > FILE_LIMIT:
    raise LimitReached("embedded HTML image exceeds size limit")

  return raw


class Engines:
  def __init__(self):
    self.converter = None
    self.ocr = None

  def markdown(
    self,
    path: Path,
    kind: str,
    raw: bytes | None = None,
  ) -> str:
    from markitdown import MarkItDown, StreamInfo

    if self.converter is None:
      self.converter = MarkItDown(enable_plugins=False)

    source = io.BytesIO(raw) if raw is not None else decoder_input(path)
    info = StreamInfo(
      extension=kind,
      mimetype="text/html" if kind == ".html" else None,
      charset="utf-8" if raw is not None else None,
    )

    with source as stream:
      result = self.converter.convert_stream(
        stream,
        stream_info=info,
        keep_data_uris=False,
      )

    return result.text_content

  def recognize(self, image: Any) -> str:
    if self.ocr is None:
      import tesserocr

      distribution = importlib.metadata.distribution("tessdata-fast-eng")
      candidates = [
        distribution.locate_file(path).resolve()
        for path in distribution.files or []
        if str(path).endswith("/eng.traineddata")
      ]
      require(len(candidates) == 1, "English OCR model not found")
      self.ocr = tesserocr.PyTessBaseAPI(
        path=str(candidates[0].parent),
        lang="eng",
        oem=tesserocr.OEM.LSTM_ONLY,
        psm=tesserocr.PSM.AUTO,
      )

    self.ocr.SetImage(image)
    return self.ocr.GetUTF8Text().strip()

  def close(self) -> None:
    if self.ocr is not None:
      engine = self.ocr
      self.ocr = None
      engine.End()


class FileProcessor:
  def __init__(
    self,
    source: Path,
    output: Output,
    folder: str,
    node: dict[str, Any],
    step: str,
    settings: dict[str, Any],
    engines: Engines,
    cached: dict[str, Any] | None = None,
    force_visuals: bool = False,
  ):
    require(applicable(node["kind"], step), "inapplicable processing operation")
    self.source = source
    self.output = output
    self.folder = folder
    self.node = node
    self.step = step
    self.settings = settings
    self.engines = engines
    self.cached = cached
    self.force_visuals = force_visuals
    self.markdown = Markdown(output)
    self.images: list[dict[str, Any]] = []
    self.problems: list[str] = []
    self.visual_problems: list[str] = []
    self.retry = False
    self.visual_retry = False
    self.visuals = None
    self.pdf_render: dict[int, bool] = {}
    self.image_section = False
    self.summary: dict[str, Any] = {}
    self.html: HTMLContent | None = None

  def path(self, name: str) -> str:
    return join_path(self.folder, name)

  def limitation(self, message: str, visual: bool = False) -> None:
    message = display(message)

    if message not in self.problems and len(self.problems) < 20:
      self.problems.append(message)

    if visual and message not in self.visual_problems:
      if len(self.visual_problems) < 20:
        self.visual_problems.append(message)

  def coverage(self, message: str) -> None:
    messages = self.summary.setdefault("coverage", [])
    if message not in messages:
      messages.append(message)
    self.limitation(message)

  def issue(
    self,
    error: BaseException,
    visual: bool = False,
    context: str = "",
  ) -> None:
    error = processing_error(error)
    message = explanation(error)
    self.limitation(f"{context}: {message}" if context else message, visual)
    retry = not isinstance(error, _PackageError) and retryable(error)
    self.retry |= retry
    if visual:
      self.visual_retry |= retry

  def office_check(self) -> None:
    with decoder_input(self.source) as source, zipfile.ZipFile(source) as archive:
      _office_members(archive)

  def _pdf_needs_render(self, text: str) -> bool:
    return (
      len(text.strip()) < self.settings["visuals"]["sparse_text_threshold"]
      or "\ufffd" in text
    )

  def native_pdf(self) -> None:
    import pypdfium2 as pdfium

    suspect_pages = set()
    suspect_codes = Counter()
    annotated_pages = set()
    annotation_types = set()
    annotation_failures = set()
    text_failures = set()
    vector_pages = set()
    vector_failures = set()

    with (
      decoder_input(self.source) as source,
      pdfium.PdfDocument(source) as document,
    ):
      if len(document) > PAGE_LIMIT:
        raise LimitReached("PDF exceeds page limit")

      self.summary.update(
        pages=len(document),
        native_text_pages=0,
        native_text_characters=0,
      )

      for number in range(len(document)):
        text = ""
        try:
          with closing(document[number]) as page:
            try:
              kinds = _annotations(page)
              if kinds:
                annotated_pages.add(number + 1)
                annotation_types.update(kinds)
            except (StorageFailure, ProtectionFailure, Cancelled):
              raise
            except Exception as error:
              annotation_failures.add(number + 1)
              self.retry |= retryable(error)

            try:
              with closing(page.get_textpage()) as textpage:
                text = textpage.get_text_range()
            except (StorageFailure, ProtectionFailure, Cancelled):
              raise
            except Exception as error:
              text_failures.add(number + 1)
              self.retry |= retryable(error)

            if not self._pdf_needs_render(text):
              try:
                if _pdf_vectors(page):
                  vector_pages.add(number + 1)
              except (StorageFailure, ProtectionFailure, Cancelled):
                raise
              except Exception as error:
                vector_failures.add(number + 1)
                self.retry |= retryable(error)

        except (StorageFailure, ProtectionFailure, Cancelled):
          raise
        except Exception as error:
          text_failures.add(number + 1)
          annotation_failures.add(number + 1)
          self.retry |= retryable(error)

        self.pdf_render[number] = self._pdf_needs_render(text)
        codes = _suspect_codes(text)
        if codes:
          suspect_pages.add(number + 1)
          suspect_codes.update(codes)

        text = text.strip()
        if not text:
          continue

        try:
          self.markdown.add(f"## Page {number + 1}", "", text, "")
        except LimitReached as error:
          self.issue(error, context=f"Page {number + 1}")
          break

        self.summary["native_text_pages"] += 1
        self.summary["native_text_characters"] += len(text)

    if suspect_codes:
      counts = ", ".join(
        f"U+{code:04X}: {count}" for code, count in sorted(suspect_codes.items())
      )
      self.coverage(
        f"Native PDF text contains suspect code points ({counts}); "
        f"pages {_page_ranges(suspect_pages)}. Native text is unchanged."
      )

    if annotated_pages:
      self.coverage(
        f"PDF pages {_page_ranges(annotated_pages)} contain "
        + ", ".join(sorted(annotation_types))
        + " annotations not represented as native body text."
      )

    if vector_pages:
      self.coverage(
        f"PDF pages {_page_ranges(vector_pages)} contain vector graphics; "
        "these pages are not fully rendered. "
        "Flattened markings and other visual details may be omitted."
      )

    if text_failures:
      self.coverage(
        "Native PDF text unavailable on pages " + _page_ranges(text_failures)
      )

    if annotation_failures:
      self.coverage(
        "PDF annotation inspection incomplete on pages "
        + _page_ranges(annotation_failures)
      )

    if vector_failures:
      self.coverage(
        "PDF vector inspection incomplete on pages "
        + _page_ranges(vector_failures)
      )

  def image_reference(self, record: dict[str, Any], vector: bool = False) -> None:
    if self.step != "convert":
      return

    link = link_to(record["path"], self.path("text.md"))
    title = label(record["label"])
    parts = []

    if not self.image_section:
      parts += ["## Extracted images", ""]

    if vector:
      parts += [f"- [Vector image: {title}]({link})", ""]
    else:
      parts += [f"### {title}", "", f"![{title}]({link})", ""]

    self.markdown.add(*parts, appendix=True)
    self.image_section = True

  def accept_image(
    self, image: Any, title: str, page: int | None = None
  ) -> None:
    if len(self.images) >= IMAGE_LIMIT:
      raise LimitReached("document visual-reference limit reached")

    previous = set(self.output.claimed)
    previous_images = set(self.output.image_paths)

    try:
      normalized = normalize_image(image)
      try:
        blank = all(low == high for low, high in normalized.getextrema())
        path = self.output.asset(
          self.folder,
          ".png",
          lambda stream: normalized.save(
            stream, format="PNG", compress_level=3
          ),
        )
      finally:
        normalized.close()

      record = {
        "path": path,
        "key": Path(path).stem,
        "blank": blank,
        "label": display(title, 256),
        "page": page,
      }
      self.image_reference(record)
      self.images.append(record)

    except BaseException:
      self.output.rollback(previous, previous_images)
      raise

  def accept_cached(self, image: dict[str, Any]) -> None:
    if len(self.images) >= IMAGE_LIMIT:
      raise LimitReached("document visual-reference limit reached")

    previous = set(self.output.claimed)
    previous_images = set(self.output.image_paths)

    try:
      self.output.claim(image["path"], image=True)
      self.image_reference(image)
      self.images.append(copy_image(image))

    except BaseException:
      self.output.rollback(previous, previous_images)
      raise

  def _bitmap(self, bitmap: Any, title: str, page: int) -> None:
    require(bitmap is not None, "PDF bitmap unavailable")
    bitmap.warn_on_close = False

    with closing(bitmap), closing(bitmap.to_pil()) as image:
      self.accept_image(image, title, page)

  def _pdf_page_visuals(self, page: Any, number: int, render: bool) -> None:
    import pypdfium2 as pdfium

    if render:
      import math

      scale = self.settings["visuals"]["dpi"] / 72
      width, height = page.get_size()
      if (
        width <= 0
        or height <= 0
        or math.ceil(width * scale) * math.ceil(height * scale) > PIXEL_LIMIT
      ):
        raise LimitReached("rendered PDF page exceeds pixel limit")

      self._bitmap(
        page.render(scale=scale),
        f"Page {number + 1}",
        number + 1,
      )
      return

    for index, obj in enumerate(
      page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]), 1
    ):
      if len(self.images) >= IMAGE_LIMIT:
        raise LimitReached("document visual-reference limit reached")

      title = f"Page {number + 1}, image {index}"
      try:
        metadata = obj.get_metadata()
        if metadata.width * metadata.height > PIXEL_LIMIT:
          raise LimitReached("embedded PDF image exceeds pixel limit")

        self._bitmap(obj.get_bitmap(), title, number + 1)

      except (StorageFailure, ProtectionFailure, Cancelled, BudgetReached):
        raise
      except Exception as error:
        self.issue(error, visual=True, context=title)

  def pdf_visuals(self) -> None:
    import pypdfium2 as pdfium

    with (
      decoder_input(self.source) as source,
      pdfium.PdfDocument(source) as document,
    ):
      if len(document) > PAGE_LIMIT:
        raise LimitReached("PDF exceeds page limit")

      self.summary["pages"] = len(document)

      for number in range(len(document)):
        if len(self.images) >= IMAGE_LIMIT:
          raise LimitReached("document visual-reference limit reached")

        try:
          with closing(document[number]) as page:
            render = self.pdf_render.get(number)

            if render is None:
              try:
                with closing(page.get_textpage()) as textpage:
                  render = self._pdf_needs_render(textpage.get_text_range())
              except (StorageFailure, ProtectionFailure, Cancelled):
                raise
              except Exception:
                render = True

            self._pdf_page_visuals(page, number, render)

        except (StorageFailure, ProtectionFailure, Cancelled, BudgetReached):
          raise
        except Exception as error:
          self.issue(
            error,
            visual=True,
            context=f"Page {number + 1}: visual discovery incomplete",
          )

  def _frames(self, image: Any, title: str) -> None:
    count = getattr(image, "n_frames", 1)
    if count > IMAGE_LIMIT or len(self.images) + count > IMAGE_LIMIT:
      raise LimitReached("document visual-reference limit reached")

    for number in range(count):
      image.seek(number)
      caption = title if count == 1 else f"{title}, frame {number + 1}"
      self.accept_image(image, caption)

  def raster_visuals(self) -> None:
    from PIL import Image

    with decoder_input(self.source) as source, Image.open(source) as image:
      self._frames(image, "Image")

  def _vector(self, raw: bytes, suffix: str, title: str) -> None:
    previous = set(self.output.claimed)
    previous_images = set(self.output.image_paths)

    try:
      path = self.output.asset(
        self.folder, suffix, lambda stream: stream.write(raw)
      )
      self.image_reference({"path": path, "label": title}, vector=True)
      self.summary["vectors"] = self.summary.get("vectors", 0) + 1
      self.limitation(
        "vector images are preserved but not rasterized or OCRed",
        visual=True,
      )

    except BaseException:
      self.output.rollback(previous, previous_images)
      raise

  def office_visuals(self) -> None:
    from PIL import Image

    references = 0

    with decoder_input(self.source) as source, zipfile.ZipFile(source) as archive:
      package = _Package(archive)

      for member in package.members:
        suffix = Path(member.filename).suffix.lower()
        if member.is_dir() or "/media/" not in member.filename:
          continue
        if suffix not in IMAGE_SUFFIXES | VECTOR_SUFFIXES:
          continue

        references += 1
        if references > IMAGE_LIMIT:
          raise LimitReached("Office image-reference limit reached")

        try:
          raw = package.read(member.filename)
          title = "Embedded image: " + display(member.filename, 200)

          if suffix in VECTOR_SUFFIXES:
            self._vector(raw, suffix, title)
          else:
            with Image.open(io.BytesIO(raw)) as image:
              self._frames(image, title)

        except (StorageFailure, ProtectionFailure, Cancelled, BudgetReached):
          raise
        except Exception as error:
          self.issue(error, visual=True)

  def recover_pptx(self, package: _Package) -> None:
    slides, namespaces = _pptx_order(package)
    self.coverage(
      "PPTX fallback reads slide paragraphs and tables in presentation order. "
      "Shape order may differ from visual order. Notes, charts, SmartArt, "
      "master/layout text, styling and merged-cell geometry are not recovered."
    )
    recovered = 0

    for number, name in enumerate(slides, 1):
      try:
        slide = package.xml(name)
        self.slide_coverage(slide, number)
        blocks = iter(_slide_blocks(slide, namespaces))
        first = next(blocks, None)
        if first is None:
          continue

        self.markdown.add(f"## Slide {number}", "", first, "")
        for block in blocks:
          self.markdown.add(block, "")
        recovered += 1

      except (StorageFailure, ProtectionFailure, Cancelled, LimitReached):
        raise
      except _PackageError as error:
        self.issue(error, context=f"Slide {number}")

    self.summary["recovered_slides"] = recovered
    if not recovered:
      self.limitation("PPTX fallback recovered no supported slide text")

  def office_failure(self, error: BaseException) -> None:
    if isinstance(error, LimitReached):
      self.issue(error)
      return

    integrity = _integrity_failure(error)
    pptx = self.node["kind"] == ".pptx"

    if not integrity:
      self.issue(error)
      if not pptx:
        return

    confirmed = False

    try:
      with decoder_input(self.source) as source, zipfile.ZipFile(source) as archive:
        package = _Package(archive)
        failures = package.verify()
        confirmed = bool(failures)

        if confirmed:
          for failure in failures:
            self.issue(failure)

          if pptx:
            self.recover_pptx(package)
          else:
            self.limitation(
              "Native Office text unavailable after a verified "
              "ZIP integrity failure"
            )

    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except Exception as inspection_error:
      self.issue(
        inspection_error,
        context="PPTX recovery" if pptx else "Office integrity inspection",
      )

    if integrity and not confirmed:
      self.issue(error)

  def html_content(self) -> HTMLContent:
    if self.html is None:
      with storage_io():
        info = self.source.lstat()
        checked_file(info)

        if info.st_size > HTML_DOCUMENT_LIMIT:
          raise LimitReached("HTML exceeds static-processing input limit")

        raw = read_private(self.source, HTML_DOCUMENT_LIMIT)

      self.html = prepare_html(raw)

    return self.html

  def html_visuals(self) -> None:
    from PIL import Image

    content = self.html_content()
    for message in content.problems:
      self.coverage(message)
      self.limitation(message, visual=True)

    for uri, title in content.images:
      try:
        raw = embedded_raster(uri)
        with Image.open(io.BytesIO(raw)) as image:
          self._frames(image, title)

      except (StorageFailure, ProtectionFailure, Cancelled, BudgetReached):
        raise
      except Exception as error:
        self.issue(error, visual=True)
        if len(self.images) >= IMAGE_LIMIT:
          break

  def extract_visuals(self) -> None:
    reusable = bool(
      not self.force_visuals
      and self.cached
      and self.cached["settings"] == self.settings["visuals"]
      and self.cached["status"] == "complete"
    )
    previous_text = self.output.text_bytes

    if self.step == "ocr":
      self.output.text_bytes += min(64 * 1024, self.output.available)

    try:
      if reusable:
        for image in self.cached["images"]:
          self.accept_cached(image)
      elif self.node["kind"] == ".pdf":
        self.pdf_visuals()
      elif self.node["kind"] in IMAGE_SUFFIXES:
        self.raster_visuals()
      elif self.node["kind"] in OFFICE_SUFFIXES:
        self.office_visuals()
      elif self.node["kind"] == ".html":
        self.html_visuals()

    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except Exception as error:
      self.issue(error, visual=True)
    finally:
      if self.step == "ocr":
        self.output.text_bytes = previous_text

    self.summary["images"] = len(self.images)
    self.visuals = outcome(
      self.settings["visuals"],
      outputs={image["path"] for image in self.images},
      problems=self.visual_problems,
      retry=self.visual_retry,
    ) | {"images": [copy_image(image) for image in self.images]}

  def html_convert(self) -> None:
    content = self.html_content()
    for message in content.problems:
      self.coverage(message)

    text = self.engines.markdown(self.source, ".html", content.markup)

    for key, value in reversed(list(content.literals.items())):
      require(
        text.count(key) == 1,
        "HTML source block was lost during conversion",
      )
      text = text.replace(key, value)

    if text.strip():
      self.markdown.add(text, "")

    self.extract_visuals()

    if not text.strip() and not self.images:
      self.limitation("HTML contains no extractable static text or raster images")

  def smartart_coverage(self, message: str) -> None:
    findings = self.summary.setdefault("coverage", [])
    marker = "Additional SmartArt coverage findings omitted"
    if message in findings or marker in findings:
      return
    if len(findings) >= 19:
      del findings[19:]
      message = marker
    self.coverage(message)

  def slide_coverage(self, slide: Any, number: int) -> None:
    try:
      for finding in _smartart_findings(slide):
        self.smartart_coverage(f"Slide {number}: {finding}")
    except LimitReached as error:
      self.smartart_coverage(
        f"Slide {number}: SmartArt inspection incomplete: {error.message}"
      )

  def pptx_coverage(self) -> None:
    with decoder_input(self.source) as source, zipfile.ZipFile(source) as archive:
      package = _Package(archive)
      try:
        slides, _ = _pptx_order(package)
      except (_PackageError, LimitReached) as error:
        self.smartart_coverage("SmartArt inspection incomplete: " + explanation(error))
        return
      for number, part in enumerate(slides, 1):
        try:
          slide = package.xml(part)
          self.slide_coverage(slide, number)
        except (_PackageError, LimitReached) as error:
          self.smartart_coverage(
            f"Slide {number}: SmartArt inspection incomplete: {explanation(error)}"
          )

  def office_convert(self) -> None:
    try:
      text = self.engines.markdown(self.source, self.node["kind"])

      if len(text.encode()) > TEXT_LIMIT - 8192:
        raise LimitReached("Office Markdown exceeds text limit")

      if self.node["kind"] == ".pptx":
        text = _pptx_breaks(text)
      text = image_descriptions(text)
      if text.strip():
        self.markdown.add(text, "")
      if self.node["kind"] == ".pptx":
        self.pptx_coverage()

    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except Exception as error:
      self.office_failure(error)

    self.extract_visuals()

  def recognize(self) -> None:
    from PIL import Image

    summary = {
      "images": len(self.images),
      "text": 0,
      "blank": 0,
      "duplicates": 0,
    }
    seen = set()

    for image in self.images:
      if image["key"] in seen:
        summary["duplicates"] += 1
        continue

      seen.add(image["key"])

      try:
        if image["blank"]:
          summary["blank"] += 1
          continue

        path = self.output.resolve(image["path"]).path
        with decoder_input(path) as stream, Image.open(stream) as source:
          if source.width * source.height > PIXEL_LIMIT:
            raise LimitReached("cached image exceeds pixel limit")
          text = self.engines.recognize(source)

        if text:
          self.markdown.add(f"## {label(image['label'])}", "", text, "")
          summary["text"] += 1

      except (StorageFailure, ProtectionFailure, Cancelled):
        raise
      except Exception as error:
        self.issue(error)
        if isinstance(error, LimitReached):
          break

    self.summary.update(summary)

  def _run(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
    kind = self.node["kind"]

    try:
      if kind in OFFICE_SUFFIXES:
        self.office_check()

      if self.step == "ocr":
        self.extract_visuals()
        self.recognize()
      elif kind == ".pdf":
        self.native_pdf()
        self.extract_visuals()
      elif kind in IMAGE_SUFFIXES:
        self.extract_visuals()
      elif kind == ".html":
        self.html_convert()
      else:
        self.office_convert()

    except (StorageFailure, ProtectionFailure, Cancelled):
      raise
    except Exception as error:
      self.issue(error)

    markdown = None
    content = b""
    raw = self.markdown.bytes()

    if raw:
      relative = self.path("text.md" if self.step == "convert" else "ocr.md")

      try:
        self.output.write_text(relative, raw)
        markdown = relative
        content = self.markdown.content()

      except (StorageFailure, ProtectionFailure, Cancelled):
        raise
      except Exception as error:
        self.issue(error)

    product = outcome(
      self.settings[self.step],
      outputs=set(self.output.claimed),
      markdown=markdown,
      problems=self.problems,
      retry=self.retry,
      summary=self.summary,
      failed=bool(self.problems and self.retry and markdown is None),
      content_bytes=len(content),
      content_sha256=digest(content),
    )
    return product, self.visuals

  def run(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from PIL import Image

    with storage_io():
      checked_file(self.source.lstat())

    previous_limit = Image.MAX_IMAGE_PIXELS

    try:
      Image.MAX_IMAGE_PIXELS = PIXEL_LIMIT
      with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return self._run()
    finally:
      Image.MAX_IMAGE_PIXELS = previous_limit


def copy_image(image: dict[str, Any]) -> dict[str, Any]:
  return {
    key: image[key]
    for key in ("path", "key", "blank", "label", "page")
  }
