from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import secrets

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


APPLICATION = "kctl"
DOCUMENTS = "documents"
EXTRACTED = "extracted"

CTL_HOST = "ctl.kmu.ac.kr"
SSO_HOST = "sso.kmu.ac.kr"
CTL = f"https://{CTL_HOST}"
SSO = f"https://{SSO_HOST}"

INDEX_URL = f"{CTL}/index.jsp"
HOME_URL = f"{CTL}/Main.do?cmd=viewHome"
LOGIN_FORM_URL = (
  f"{SSO}/kmusso/ext/ctl/login_form.do?Return_Url={INDEX_URL}"
)
LOGIN_POST_URL = f"{SSO}/kmusso/ext/ctl/login_process.do"
CTL_LOGOUT_URL = f"{CTL}/User.do?cmd=logoutUser"
LOGOUT_BRIDGE_URL = f"{CTL}/ssoLogout.jsp"
SSO_LOGOUT_URL = (
  f"{SSO}/kmusso/logout_process.do?Return_Url={INDEX_URL}"
)

HTML_LIMIT = 2 * 1024 * 1024
HTML_DOCUMENT_LIMIT = 32 * 1024 * 1024
FILE_LIMIT = 100 * 1024 * 1024
TEXT_LIMIT = 32 * 1024 * 1024
MANIFEST_LIMIT = 64 * 1024 * 1024
JOB_LIMIT = 16 * 1024 * 1024
ASSET_LIMIT = 500 * 1024 * 1024
REPORT_LIMIT = 8 * 1024 * 1024
CONTENT_LIMIT = ASSET_LIMIT - REPORT_LIMIT
DOWNLOAD_LIMIT = 2 * 1024 * 1024 * 1024
EXPORT_LIMIT = 256 * 1024 * 1024

PIXEL_LIMIT = 40_000_000
IMAGE_LIMIT = 1000
PAGE_LIMIT = 500
ENTRY_LIMIT = 500
DEPTH_LIMIT = 4
NETWORK_TIMEOUT = 30

HASH = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[0-9a-f]{32}")
COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
SESSION_PATH = re.compile(
  r";jsessionid=[A-Za-z0-9._~-]{1,256}(?=/|$)", re.I
)

IMAGE_SUFFIXES = frozenset({
  ".png", ".jpg", ".jpeg", ".gif", ".webp",
  ".bmp", ".tif", ".tiff",
})
OFFICE_SUFFIXES = frozenset({".pptx", ".docx", ".xlsx"})
HTML_SUFFIXES = frozenset({".html", ".htm"})

TEXT_LANGUAGES = {
  ".txt": "text",
  ".md": "",
  ".markdown": "",
  ".rst": "rst",
  ".py": "python",
  ".pyi": "python",
  ".js": "javascript",
  ".mjs": "javascript",
  ".cjs": "javascript",
  ".jsx": "jsx",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".java": "java",
  ".c": "c",
  ".h": "c",
  ".cc": "cpp",
  ".cpp": "cpp",
  ".cxx": "cpp",
  ".hh": "cpp",
  ".hpp": "cpp",
  ".cs": "csharp",
  ".go": "go",
  ".rs": "rust",
  ".rb": "ruby",
  ".php": "php",
  ".sh": "sh",
  ".bash": "bash",
  ".zsh": "zsh",
  ".ps1": "powershell",
  ".bat": "bat",
  ".cmd": "bat",
  ".sql": "sql",
  ".r": "r",
  ".swift": "swift",
  ".kt": "kotlin",
  ".kts": "kotlin",
  ".scala": "scala",
  ".lua": "lua",
  ".pl": "perl",
  ".m": "matlab",
  ".f": "fortran",
  ".f90": "fortran",
  ".jl": "julia",
  ".json": "json",
  ".jsonl": "json",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".toml": "toml",
  ".ini": "ini",
  ".cfg": "ini",
  ".conf": "text",
  ".csv": "csv",
  ".tsv": "tsv",
  ".xml": "xml",
  ".puml": "plantuml",
  ".plantuml": "plantuml",
  ".css": "css",
  ".scss": "scss",
  ".less": "less",
  ".tex": "latex",
  ".log": "text",
  ".sol": "solidity",
}
TEXT_BASENAMES = {
  "makefile": "makefile",
  "dockerfile": "dockerfile",
  "license": "text",
  "licence": "text",
  "readme": "text",
  ".gitignore": "text",
  ".dockerignore": "text",
  ".env.example": "text",
}
TEXT_SUFFIXES = frozenset(TEXT_LANGUAGES)
TEXT_KINDS = TEXT_SUFFIXES | {"text"}
CONVERT_KINDS = IMAGE_SUFFIXES | OFFICE_SUFFIXES | {".pdf", ".html"}

TAR_SUFFIXES = (
  ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.zstd",
  ".tar.lz4", ".tgz", ".tbz", ".tbz2", ".txz", ".tzst", ".tar",
)
CONTAINER_KINDS = frozenset({"zip", "tar", "tar-zstd", "tar-lz4"})
CONTENT_KINDS = (
  TEXT_KINDS | CONVERT_KINDS | CONTAINER_KINDS | {"unsupported"}
)
DOCUMENT_RESERVED = frozenset({
  "text.md", "ocr.md", "index.md", "images", "contents",
})


class Failure(Exception):
  def __init__(self, message: str, code: int = 2):
    super().__init__(message)
    self.message = message
    self.code = code


class ProtectionFailure(Failure):
  pass


class StorageFailure(Failure):
  pass


class SourceProblem(Failure):
  pass


class WorkerResultFailure(Failure):
  pass


class LimitReached(Failure):
  pass


class BudgetReached(LimitReached):
  pass


class Cancelled(Exception):
  pass


def require(condition: Any, message: str, code: int = 2) -> None:
  if not condition:
    raise Failure(message, code)


def now() -> str:
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token() -> str:
  return secrets.token_hex(16)


def digest(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def identity(*values: str) -> str:
  return digest(json.dumps(values, ensure_ascii=False).encode())


def terminal(value: Any) -> str:
  return "".join(
    character if character.isprintable() else " "
    for character in str(value)
  )


def display(value: Any, limit: int = 2048) -> str:
  text = str(value).encode("utf-8", "replace").decode()
  return terminal(text)[:limit]


def cancelled_exception(error: BaseException) -> bool:
  return isinstance(error, (KeyboardInterrupt, EOFError, Cancelled))


def explanation(error: BaseException) -> str:
  if isinstance(error, Failure):
    return error.message

  if cancelled_exception(error):
    return "interrupted"

  if isinstance(error, OSError):
    detail = (
      os.strerror(error.errno).lower()
      if error.errno is not None else type(error).__name__
    )
    if error.filename:
      name = Path(os.fsdecode(error.filename)).name
      return f"{name}: {detail}"
    return detail

  return f"unexpected {type(error).__name__}"


def retryable(error: BaseException) -> bool:
  return (
    isinstance(error, BudgetReached)
    or not isinstance(error, LimitReached)
  )


def label(value: str) -> str:
  return re.sub(
    r"([\\\[\]`*_<>])",
    r"\\\1",
    " ".join(value.split()),
  )


def safe_name(value: str, limit: int = 120) -> str:
  require(type(limit) is int and limit > 0, "invalid filename limit")
  value = re.sub(r"[^\w .()\[\]-]", "-", value)
  value = re.sub(r"\s+", " ", value).strip(" .-")
  value = value.encode()[:limit].decode("utf-8", "ignore").rstrip(" .-")
  return value or "untitled"[:limit]


def safe_filename(value: str) -> str:
  value = value.replace("\\", "/").rsplit("/", 1)[-1]
  suffix = Path(value).suffix.lower()

  if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
    suffix = ""

  stem = value[:-len(suffix)] if suffix else value
  return safe_name(stem, 140) + suffix


def numbered(value: str, number: int, file: bool = False) -> str:
  if number == 1:
    return value

  suffix = Path(value).suffix if file else ""
  stem = value[:-len(suffix)] if suffix else value
  return f"{stem} ({number}){suffix}"


def original_name(value: str) -> str:
  name = safe_filename(value)

  if name.casefold() in DOCUMENT_RESERVED:
    name = numbered(name, 2, file=True)

  return name


def bundle_name(value: str, limit: int = 120) -> str:
  value = value.replace("\\", "/").rsplit("/", 1)[-1]

  for suffix in TAR_SUFFIXES:
    if value.lower().endswith(suffix):
      return safe_name(value[:-len(suffix)], limit)

  return safe_name(Path(value).stem or value, limit)


def join_path(parent: str, name: str) -> str:
  return f"{parent}/{name}" if parent else name


def parent_path(value: str) -> str:
  parent = str(Path(value).parent)
  return "" if parent == "." else parent


def link_to(target: str, origin: str) -> str:
  relative = os.path.relpath(target, str(Path(origin).parent))
  return quote(relative)


def bounded_text(value: Any, limit: int) -> bool:
  return (
    isinstance(value, str)
    and len(value) <= limit
    and all(ord(character) >= 32 and ord(character) != 127 for character in value)
  )


def valid_id(value: Any) -> bool:
  return (
    isinstance(value, str)
    and 0 < len(value) <= 128
    and value == value.strip()
    and value.isprintable()
  )


def valid_header(value: Any) -> bool:
  return (
    bounded_text(value, 4096)
    and all(ord(character) <= 255 for character in value)
  )


def json_bytes(value: Any) -> bytes:
  return json.dumps(
    value, ensure_ascii=False, indent=2, allow_nan=False
  ).encode()


def unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
  result = {}

  for key, value in pairs:
    require(key not in result, "duplicate JSON field")
    result[key] = value

  return result


def reject_constant(value: str) -> Any:
  raise Failure("nonfinite JSON value")


def parse_json(raw: bytes) -> Any:
  try:
    return json.loads(
      raw.decode(),
      object_pairs_hook=unique_json,
      parse_constant=reject_constant,
    )
  except (ValueError, UnicodeError, RecursionError):
    raise Failure("malformed JSON") from None


def relative_parts(value: str) -> tuple[str, ...]:
  require(
    isinstance(value, str)
    and value
    and "\\" not in value
    and all(ord(character) >= 32 and ord(character) != 127 for character in value),
    "invalid managed path",
  )
  parts = value.split("/")
  require(
    all(part not in {"", ".", ".."} for part in parts),
    "unsafe path",
  )
  return tuple(parts)


def public_path(value: str) -> None:
  require(
    not any(part.startswith(".") for part in relative_parts(value)),
    "invalid public archive path",
  )


def publication_path(value: str) -> None:
  public_path(value)
  require(
    value.startswith(DOCUMENTS + "/"),
    "publication is outside the documents directory",
  )


def reader_url(value: str) -> str:
  require(
    isinstance(value, str)
    and 0 < len(value) <= 16384
    and "\\" not in value
    and all(ord(character) >= 32 and ord(character) != 127 for character in value),
    "invalid CTL URL",
  )

  try:
    parts = urlsplit(urljoin(CTL + "/", value))
  except ValueError:
    raise Failure("malformed CTL URL") from None

  require(
    parts.scheme == "https" and parts.netloc == CTL_HOST,
    "reader URL leaves CTL HTTPS origin",
  )
  path = re.sub(r";jsessionid=[^/?;]*", "", parts.path, flags=re.I)
  decoded = unquote(path)

  require(
    "\\" not in decoded
    and all(ord(character) >= 32 and ord(character) != 127 for character in decoded)
    and not any(part in {".", ".."} for part in decoded.split("/")),
    "unsupported CTL path",
  )

  return urlunsplit(("https", CTL_HOST, path, parts.query, ""))


def named_kind(name: str) -> str:
  basename = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
  suffix = Path(basename).suffix

  if suffix in HTML_SUFFIXES:
    return ".html"

  if suffix in CONVERT_KINDS | TEXT_SUFFIXES:
    return suffix

  if basename in TEXT_BASENAMES:
    return "text"

  if suffix == ".zip":
    return "zip"

  if basename.endswith((".tar.zst", ".tar.zstd", ".tzst")):
    return "tar-zstd"

  if basename.endswith(".tar.lz4"):
    return "tar-lz4"

  if basename.endswith(TAR_SUFFIXES):
    return "tar"

  return "unsupported"


def text_language(kind: str, name: str = "") -> str | None:
  require(kind in TEXT_KINDS, "not a passthrough text format")

  if kind == "text":
    basename = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return TEXT_BASENAMES.get(basename, "text")

  return TEXT_LANGUAGES[kind] or None


def decode_text(raw: bytes) -> str:
  if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
    encoding = "utf-32"
  elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
    encoding = "utf-16"
  else:
    encoding = "utf-8-sig"

  try:
    text = raw.decode(encoding)
  except UnicodeError:
    raise Failure(
      "text is not valid UTF-8 or BOM-marked Unicode"
    ) from None

  require("\x00" not in text, "text contains NUL bytes")
  return text.replace("\r\n", "\n").replace("\r", "\n")


def fenced(text: str, language: str = "") -> str:
  require(
    isinstance(language, str)
    and re.fullmatch(r"[A-Za-z0-9_+.-]{0,32}", language),
    "invalid code-fence language",
  )
  longest = max(
    (match.end() - match.start() for match in re.finditer(r"`+", text)),
    default=0,
  )
  fence = "`" * max(3, longest + 1)
  ending = "" if text.endswith("\n") else "\n"
  return f"{fence}{language}\n{text}{ending}{fence}\n"
