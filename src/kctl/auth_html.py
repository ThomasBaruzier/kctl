from __future__ import annotations

import codecs
import re
import warnings

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

from .core import HTML_LIMIT, SESSION_PATH, Failure, require


def soup_html(text: str | bytes) -> Any:
  from bs4 import BeautifulSoup

  with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    return BeautifulSoup(text, "html.parser")


def text_of(node: Any) -> str:
  if node is None:
    return ""
  return " ".join(node.get_text(" ", strip=True).split())


def approved_url(candidate: str, base: str, expected: str) -> str:
  require(
    isinstance(candidate, str)
    and 0 < len(candidate) <= 4096
    and candidate == candidate.strip()
    and "\\" not in candidate
    and all(32 < ord(character) < 127 for character in candidate),
    "unapproved authentication navigation",
  )

  try:
    actual = urlsplit(urljoin(base, candidate))
    wanted = urlsplit(expected)

    require(
      actual.scheme == "https"
      and actual.hostname == wanted.hostname
      and actual.username is None
      and actual.password is None
      and actual.port in {None, 443}
      and not actual.fragment
      and SESSION_PATH.sub("", actual.path) == wanted.path,
      "unapproved authentication target",
    )

    fields = parse_qsl(
      actual.query,
      keep_blank_values=True,
      strict_parsing=True,
      max_num_fields=8,
    )
    target = parse_qsl(
      wanted.query,
      keep_blank_values=True,
      strict_parsing=True,
    )

    require(
      len({name for name, _ in fields}) == len(fields)
      and sorted(fields) == sorted(target),
      "unapproved authentication parameters",
    )
  except (ValueError, UnicodeError):
    raise Failure("malformed authentication URL") from None

  return expected


class FormInventory(HTMLParser):
  def __init__(self, text: str):
    super().__init__(convert_charrefs=True)
    self.records = []
    self.stack = []
    self.associations = set()

    self.feed(text)
    self.close()

    for index in self.stack:
      self.records[index]["bad"] = True

  def handle_starttag(
    self, tag: str, attrs: list[tuple[str, str | None]]
  ) -> None:
    if tag == "form":
      nested = bool(self.stack)

      for index in self.stack:
        self.records[index]["bad"] = True

      self.records.append({"attrs": attrs, "bad": nested})
      self.stack.append(len(self.records) - 1)

    if tag not in {"form", "input", "select", "textarea", "button"}:
      return

    names = [name for name, _ in attrs]

    if len(names) != len(set(names)):
      for index in self.stack:
        self.records[index]["bad"] = True

    self.associations.update(
      value
      for name, value in attrs
      if name == "form" and value is not None
    )

  def handle_endtag(self, tag: str) -> None:
    if tag == "form" and self.stack:
      self.stack.pop()

  def handle_startendtag(
    self, tag: str, attrs: list[tuple[str, str | None]]
  ) -> None:
    self.handle_starttag(tag, attrs)

    if tag == "form":
      self.records[self.stack[-1]]["bad"] = True
      self.handle_endtag(tag)

  def validate(self, index: int) -> None:
    record = self.records[index]
    identifiers = {
      value
      for name, value in record["attrs"]
      if name in {"id", "name"}
    }

    require(
      not record["bad"]
      and not identifiers.intersection(self.associations),
      "malformed or explicitly associated authentication form",
    )


@dataclass
class AuthDocument:
  soup: Any
  encoding: str
  inventory: FormInventory

  def form(self, name: str) -> Any:
    matches = [
      index
      for index, record in enumerate(self.inventory.records)
      if any(
        key in {"name", "id"} and value == name
        for key, value in record["attrs"]
      )
    ]

    require(
      len(matches) == 1,
      "missing or ambiguous authentication form",
    )

    index = matches[0]
    self.inventory.validate(index)
    form = self.soup.find_all("form")[index]

    require(
      form.get("name") == name and form.find("form") is None,
      "unexpected authentication form structure",
    )

    return form


def auth_document(headers: Any, raw: bytes) -> AuthDocument:
  require(len(raw) <= HTML_LIMIT, "authentication HTML exceeds size limit")
  require(
    len(headers.get_all("Content-Type", [])) == 1
    and headers.get_content_type() == "text/html",
    "expected unambiguous authentication HTML",
  )

  charsets = [
    value
    for name, value in (headers.get_params() or [])[1:]
    if name.lower() == "charset"
  ]

  require(
    len(charsets) == 1
    and isinstance(charsets[0], str)
    and charsets[0],
    "missing or ambiguous authentication charset",
  )

  try:
    encoding = codecs.lookup(charsets[0]).name
    text = raw.decode(encoding)
  except (LookupError, UnicodeError, ValueError):
    raise Failure("unsupported authentication encoding") from None

  require("\x00" not in text, "malformed authentication HTML")

  try:
    inventory = FormInventory(text)
    page = soup_html(text)
  except (AssertionError, ValueError, RecursionError):
    raise Failure("malformed authentication HTML") from None

  require(
    len(page.find_all("form")) == len(inventory.records),
    "inconsistent authentication form parsing",
  )
  require(
    "an error has occurred while processing" not in text_of(page).lower(),
    "authentication application error",
  )

  return AuthDocument(page, encoding, inventory)


def mask_script(source: str) -> tuple[str, dict[int, str | None]]:
  masked = list(source)
  literals = {}
  position = 0

  while position < len(source):
    start = position

    if source.startswith(("<!--", "-->", "//"), position):
      end = source.find("\n", position)
      end = len(source) if end < 0 else end
      masked[start:end] = " " * (end - start)
      position = end
      continue

    if source.startswith("/*", position):
      end = source.find("*/", position + 2)
      require(end >= 0, "unterminated navigation comment")
      end += 2
      masked[start:end] = " " * (end - start)
      position = end
      continue

    quote = source[position]

    if quote not in {"'", '"', "`"}:
      position += 1
      continue

    position += 1
    plain = quote != "`"

    while position < len(source) and source[position] != quote:
      require(
        quote == "`" or source[position] not in "\r\n",
        "unsupported navigation string",
      )

      if source[position] == "\\":
        plain = False
        position += 2
      else:
        position += 1

    require(position < len(source), "unterminated navigation string")
    literals[start] = source[start + 1:position] if plain else None
    position += 1

    masked[start:position] = " " * (position - start)
    masked[start] = "@"

  return "".join(masked), literals


def navigation_script(
  page: Any, markers: tuple[str, ...]
) -> tuple[str, dict[int, str | None]]:
  parts = []
  literals = {}
  offset = 0

  for script in page.find_all("script"):
    source = script.string if script.string is not None else script.get_text()

    if not any(marker in source for marker in markers):
      continue

    require(
      not script.has_attr("src")
      and str(script.get("type", "")).lower() in {
        "", "text/javascript", "application/javascript",
      },
      "unsupported navigation script",
    )

    masked, found = mask_script(source)
    literals.update({
      offset + position: value for position, value in found.items()
    })
    parts.append(masked + "\n;\n")
    offset += len(masked) + 3

  require(parts, "missing navigation script")
  return "".join(parts), literals


def declared_literal(
  code: str, literals: dict[int, str | None], name: str
) -> str:
  escaped = re.escape(name)
  declarations = list(re.finditer(
    r"(?<![\w$.])var\s+" + escaped + r"\b", code
  ))
  writes = (
    r"\s*(?:=(?!=)|(?:\*\*|>>>|<<|>>|&&|\|\||\?\?|[+*/%&|^\-])=|\+\+|--)"
  )
  assignments = list(re.finditer(
    r"(?<![\w$.])" + escaped + writes, code
  ))

  require(
    len(declarations) == len(assignments) == 1
    and re.search(r"(?:\+\+|--)\s*" + escaped + r"\b", code) is None,
    "ambiguous navigation literal",
  )

  match = re.compile(r"\s*=\s*(?P<literal>@)\s*;").match(
    code, declarations[0].end()
  )
  require(match is not None, "expected plain navigation literal")

  value = literals.get(match.start("literal"))
  require(isinstance(value, str), "unsupported navigation literal")
  return value


def function_body(code: str, name: str) -> tuple[str, int]:
  declaration = r"(?<![\w$.])function\s+" + re.escape(name) + r"\b"

  require(
    len(list(re.finditer(declaration, code))) == 1,
    "missing or ambiguous navigation function",
  )

  match = re.search(declaration + r"\s*\(\s*\)\s*\{", code)
  require(
    match is not None,
    "unsupported navigation function signature",
  )

  start = match.end()
  depth = 1

  for position in range(start, len(code)):
    depth += (code[position] == "{") - (code[position] == "}")

    if depth == 0:
      return code[start:position], start

  raise Failure("unterminated navigation function")
