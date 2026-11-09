from __future__ import annotations

import copy
import importlib.metadata
import sys

from typing import Any

from .core import (
  APPLICATION,
  ASSET_LIMIT,
  CONTENT_KINDS,
  CONTENT_LIMIT,
  CONTAINER_KINDS,
  CONVERT_KINDS,
  DEPTH_LIMIT,
  DOCUMENT_RESERVED,
  DOCUMENTS,
  ENTRY_LIMIT,
  FILE_LIMIT,
  HASH,
  IMAGE_LIMIT,
  PAGE_LIMIT,
  PIXEL_LIMIT,
  REPORT_LIMIT,
  TEXT_KINDS,
  TEXT_LIMIT,
  TOKEN,
  LimitReached,
  bounded_text,
  digest,
  display,
  explanation,
  identity,
  join_path,
  now,
  parent_path,
  public_path,
  publication_path,
  reader_url,
  relative_parts,
  require,
  retryable,
  valid_header,
  valid_id,
)


SCHEMA = 1
PROCESSING_VERSION = 2

STEPS = frozenset({"convert", "ocr"})
ATTACHMENT_ROLES = frozenset({"attachment", "submission"})
ITEM_STATUSES = frozenset({
  "archived", "empty", "absent", "partial", "failed", "unsupported",
})
RUN_STATUSES = frozenset({
  "running", "complete", "partial", "failed", "interrupted",
})
PRODUCT_STATUSES = frozenset({"complete", "partial", "failed"})
EMPTY_SHA256 = digest(b"")
COVERAGE_OVERFLOW = "Additional retained coverage findings omitted."


def new_manifest() -> dict[str, Any]:
  return {
    "application": APPLICATION,
    "schema": SCHEMA,
    "account": None,
    "items": {},
    "documents": {},
    "run": None,
  }


def package_version(name: str) -> str:
  try:
    return importlib.metadata.version(name)
  except importlib.metadata.PackageNotFoundError:
    return "missing"


def processing_settings() -> dict[str, Any]:
  visuals = {
    "version": PROCESSING_VERSION,
    "office_integrity": 1,
    "pdf_visuals": 2,
    "html_media": 1,
    "dpi": 144,
    "sparse_text_threshold": 40,
    "pixels": PIXEL_LIMIT,
    "images": IMAGE_LIMIT,
    "pages": PAGE_LIMIT,
    "pillow": package_version("Pillow"),
    "pdfium": package_version("pypdfium2"),
    "beautifulsoup": package_version("beautifulsoup4"),
  }
  return {
    "walk": {
      "version": PROCESSING_VERSION,
      "integrity": 1,
      "depth": DEPTH_LIMIT,
      "entries": ENTRY_LIMIT,
      "bytes": ASSET_LIMIT,
      "file_bytes": FILE_LIMIT,
      "text_bytes": TEXT_LIMIT,
      "text_kinds": sorted(TEXT_KINDS),
      "python": list(sys.version_info[:2]),
      "pdfium": package_version("pypdfium2"),
      "lz4": package_version("lz4"),
      "zstandard": package_version("zstandard"),
    },
    "visuals": visuals,
    "convert": {
      "version": PROCESSING_VERSION,
      "office_integrity": 1,
      "pdf_coverage": 2,
      "pdf_vectors": 1,
      "pptx_recovery": 1,
      "pptx_breaks": 1,
      "pptx_smartart": 2,
      "html_tables": 1,
      "html_source": 3,
      "visuals": visuals,
      "markitdown": package_version("markitdown"),
      "markdownify": package_version("markdownify"),
      "defusedxml": package_version("defusedxml"),
    },
    "ocr": {
      "version": PROCESSING_VERSION,
      "visuals": visuals,
      "engine": package_version("tesserocr"),
      "model": package_version("tessdata-fast-eng"),
      "language": "eng",
      "psm": 3,
    },
  }


def applicable(kind: str, step: str) -> bool:
  return step in STEPS and kind in CONVERT_KINDS


def ready(
  node: dict[str, Any], step: str, settings: dict[str, Any]
) -> bool:
  attempt = node["attempts"].get(step)

  if attempt is not None and attempt["settings"] == settings:
    return not attempt["retry"]

  product = node["products"].get(step)
  return bool(
    product
    and product["settings"] == settings
    and not product["retry"]
  )


def outcome(
  settings: dict[str, Any],
  outputs: set[str] | None = None,
  markdown: str | None = None,
  problems: list[str] | None = None,
  retry: bool = False,
  summary: dict[str, Any] | None = None,
  failed: bool = False,
  content_bytes: int = 0,
  content_sha256: str = EMPTY_SHA256,
) -> dict[str, Any]:
  details = {} if summary is None else summary
  require(isinstance(details, dict), "invalid product summary")
  coverage = details.get("coverage", [])
  validate_problems(coverage)
  messages = list(dict.fromkeys(
    display(value) for value in [*coverage, *(problems or [])]
  ))[:20]

  return {
    "settings": settings,
    "status": "failed" if failed else "partial" if messages else "complete",
    "retry": retry,
    "outputs": sorted(outputs or set()),
    "markdown": markdown,
    "header_bytes": 0,
    "content_bytes": content_bytes,
    "content_sha256": content_sha256,
    "summary": details,
    "problems": messages,
    "finished": now(),
  }


def error_outcome(
  settings: dict[str, Any], error: BaseException
) -> dict[str, Any]:
  return outcome(
    settings,
    problems=[explanation(error)],
    retry=retryable(error),
    failed=not isinstance(error, LimitReached),
  )


def attempt_record(
  product: dict[str, Any],
  retained: bool,
  *,
  previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
  fields = (
    "settings", "status", "retry", "problems", "finished",
  )
  record = {
    name: copy.deepcopy(product[name]) for name in fields
  } | {"retained": retained}
  coverage = list(product.get("summary", {}).get("coverage", []))

  if retained and previous is not None:
    coverage = previous.get("summary", {}).get("coverage", []) + coverage

  coverage = list(dict.fromkeys(coverage))
  if COVERAGE_OVERFLOW in coverage or len(coverage) > 20:
    coverage = [
      message for message in coverage if message != COVERAGE_OVERFLOW
    ][:19] + [COVERAGE_OVERFLOW]

  if coverage:
    record["summary"] = {"coverage": coverage}

  return record


def step_findings(node: dict[str, Any], step: str) -> dict[str, list[str]]:
  product = node["products"].get(step, {})
  attempt = node["attempts"].get(step, {})
  published = (
    product.get("summary", {}).get("coverage", [])
    + product.get("problems", [])
  )
  latest = attempt.get("problems", [])
  coverage = attempt.get("summary", {}).get("coverage", [])
  retained = []

  if attempt.get("retained"):
    known = set(published) | set(latest)
    retained = [message for message in coverage if message not in known]
  else:
    latest = coverage + latest

  findings: dict[str, list[str]] = {}

  for origin, messages in (
    ("published result", published),
    ("latest attempt", latest),
    ("retained coverage, attempt unknown", retained),
  ):
    for message in dict.fromkeys(messages):
      findings.setdefault(message, []).append(origin)

  return findings


def node_problems(node: dict[str, Any]) -> list[str]:
  messages = list(node["problems"])

  for step in ("convert", "ocr"):
    messages.extend(
      f"{step}, {' / '.join(origins)}: {message}"
      for message, origins in step_findings(node, step).items()
    )

  return list(dict.fromkeys(messages))


def node_index(node: dict[str, Any]) -> bool:
  return bool(
    node["kind"] in CONTAINER_KINDS
    or node["children"]
    or node_problems(node)
  )


def result_problems(result: dict[str, Any]) -> list[str]:
  return [
    f"{folder or node['name']}: {problem}"
    for folder, node in result["nodes"].items()
    for problem in node_problems(node)
  ]


def node_files(folder: str, node: dict[str, Any]) -> set[str]:
  paths = {join_path(folder, node["original"])} if folder else set()
  paths.update((node.get("visuals") or {}).get("outputs", []))

  for product in node["products"].values():
    paths.update(product["outputs"])

  return paths


def content_files(nodes: dict[str, Any]) -> set[str]:
  return {
    path
    for folder, node in nodes.items()
    for path in node_files(folder, node)
  }


def content_images(nodes: dict[str, Any]) -> set[str]:
  paths = set()

  for node in nodes.values():
    paths.update((node.get("visuals") or {}).get("outputs", []))

    for product in node["products"].values():
      paths.update(
        path
        for path in product["outputs"]
        if path != product["markdown"]
      )

  return paths


def file_parents(files: set[str]) -> set[str]:
  parents = set()

  for path in files:
    parts = relative_parts(path)
    parents.update(
      "/".join(parts[:index]) for index in range(1, len(parts))
    )

  return parents


def document_files(record: dict[str, Any]) -> set[str]:
  return {record["original"]} | set(
    (record.get("result") or {}).get("files", {})
  )


def product_title(
  node: dict[str, Any],
  step: str,
  settings: dict[str, Any],
  title: str,
) -> str:
  product = node["products"].get(step)
  attempt = node["attempts"].get(step)

  if attempt and attempt["retained"]:
    return title + " (retained)"

  if product and product["settings"] != settings[step]:
    return title + " (older settings)"

  if product and product["status"] != "complete":
    return title + " (partial)"

  return title


def _member(value: Any, choices: frozenset[str], message: str) -> None:
  require(isinstance(value, str) and value in choices, message)


def _hash(value: Any, message: str) -> None:
  require(isinstance(value, str) and HASH.fullmatch(value), message)


def _timestamp(value: Any) -> None:
  require(
    bounded_text(value, 64) and value,
    "invalid record timestamp",
  )


def _strings(
  value: Any,
  message: str,
  limit: int = 100000,
  unique: bool = False,
) -> None:
  require(
    isinstance(value, list)
    and len(value) <= limit
    and all(isinstance(item, str) for item in value),
    message,
  )
  require(not unique or len(value) == len(set(value)), message)


def validate_problems(value: Any, limit: int = 20) -> None:
  _strings(value, "invalid diagnostics", limit)
  require(
    all(bounded_text(item, 2048) for item in value),
    "invalid diagnostics",
  )


def validate_span(record: dict[str, Any], size: int) -> None:
  header = record.get("header_bytes")
  content = record.get("content_bytes")

  require(
    type(size) is int
    and 0 <= size <= TEXT_LIMIT
    and type(header) is int
    and type(content) is int
    and 0 <= header <= size
    and 0 <= content <= size - header,
    "invalid source-content boundaries",
  )
  _hash(record.get("content_sha256"), "invalid source-content hash")
  require(
    content != 0 or record["content_sha256"] == EMPTY_SHA256,
    "invalid empty-content hash",
  )


def validate_status(value: Any) -> None:
  require(
    isinstance(value, dict)
    and isinstance(value.get("settings"), dict)
    and type(value.get("retry")) is bool,
    "invalid processing status",
  )
  _member(value.get("status"), PRODUCT_STATUSES, "invalid processing status")
  _timestamp(value.get("finished"))
  validate_problems(value.get("problems"))
  summary = value.get("summary", {})
  require(isinstance(summary, dict), "invalid processing summary")
  coverage = summary.get("coverage", [])
  validate_problems(coverage)

  if value["status"] == "complete":
    require(
      not value["retry"] and not value["problems"] and not coverage,
      "complete processing result contains unresolved problems",
    )


def validate_asset(path: str, folder: str) -> None:
  public_path(path)
  prefix = join_path(folder, "images") + "/"

  require(
    path.startswith(prefix)
    and re_asset(path[len(prefix):]),
    "invalid generated image path",
  )


def re_asset(name: str) -> bool:
  stem, separator, suffix = name.rpartition(".")
  return bool(
    separator
    and HASH.fullmatch(stem)
    and suffix in {"png", "svg", "wmf", "emf"}
  )


def _original(value: Any) -> None:
  public_path(value)
  require(
    len(relative_parts(value)) == 1
    and len(value.encode()) <= 160
    and value.casefold() not in DOCUMENT_RESERVED,
    "invalid original filename",
  )


def _folder(value: Any) -> None:
  require(isinstance(value, str), "invalid content folder")

  if not value:
    return

  public_path(value)
  parts = relative_parts(value)
  require(
    len(parts) % 2 == 0
    and len(parts) <= DEPTH_LIMIT * 2
    and all(
      parts[index] == "contents"
      for index in range(0, len(parts), 2)
    )
    and all(len(part.encode()) <= 80 for part in parts),
    "invalid content folder",
  )


def _visuals(value: Any, folder: str) -> None:
  validate_status(value)
  require(
    isinstance(value.get("images"), list)
    and len(value["images"]) <= IMAGE_LIMIT,
    "invalid visual inventory",
  )
  _strings(
    value.get("outputs"),
    "invalid visual outputs",
    IMAGE_LIMIT,
    unique=True,
  )
  paths = set()

  for image in value["images"]:
    require(
      isinstance(image, dict)
      and type(image.get("blank")) is bool
      and bounded_text(image.get("label"), 256),
      "invalid visual reference",
    )
    _hash(image.get("key"), "invalid visual identity")
    page = image.get("page")
    require(
      page is None or type(page) is int and 1 <= page <= PAGE_LIMIT,
      "invalid visual page",
    )
    validate_asset(image.get("path"), folder)
    require(
      image["path"].rsplit("/", 1)[-1] == image["key"] + ".png",
      "visual reference is not its normalized raster",
    )
    paths.add(image["path"])

  require(set(value["outputs"]) == paths, "visual inventory mismatch")


def _product(
  step: str,
  product: Any,
  folder: str,
  files: dict[str, int],
) -> int:
  validate_status(product)
  _strings(
    product.get("outputs"),
    "invalid product outputs",
    IMAGE_LIMIT + 1,
    unique=True,
  )
  require(
    isinstance(product.get("summary"), dict),
    "invalid product summary",
  )

  markdown = product.get("markdown")
  expected = join_path(
    folder, "text.md" if step == "convert" else "ocr.md"
  )
  require(
    markdown is None
    or markdown == expected and markdown in product["outputs"],
    "invalid product Markdown",
  )

  if markdown is None:
    validate_span(product, 0)
  else:
    require(markdown in files, "product Markdown missing")
    validate_span(product, files[markdown])

  for path in product["outputs"]:
    if path != markdown:
      validate_asset(path, folder)

  return product["header_bytes"]


def _node(node: Any) -> None:
  require(
    isinstance(node, dict)
    and bounded_text(node.get("name"), 2048)
    and type(node.get("bytes")) is int
    and 0 <= node["bytes"] <= FILE_LIMIT
    and type(node.get("retry")) is bool
    and isinstance(node.get("products"), dict)
    and set(node["products"]) <= STEPS
    and isinstance(node.get("attempts"), dict)
    and set(node["attempts"]) <= STEPS,
    "malformed content node",
  )
  _member(node.get("kind"), CONTENT_KINDS, "invalid content kind")
  _hash(node.get("sha256"), "invalid content identity")
  _original(node.get("original"))
  _strings(
    node.get("children"),
    "invalid content children",
    ENTRY_LIMIT,
    unique=True,
  )
  validate_problems(node.get("problems"), ENTRY_LIMIT + 20)


def validate_result_data(result: Any, doc: dict[str, Any]) -> None:
  require(
    isinstance(result, dict)
    and result.get("source") == doc["sha256"]
    and isinstance(result.get("settings"), dict)
    and isinstance(result.get("nodes"), dict)
    and "" in result["nodes"]
    and len(result["nodes"]) <= ENTRY_LIMIT + 1
    and isinstance(result.get("files"), dict)
    and len(result["files"]) <= 5000,
    "malformed local result",
  )
  _timestamp(result.get("finished"))
  files = result["files"]

  for path, size in files.items():
    public_path(path)
    require(
      type(size) is int and 0 <= size <= FILE_LIMIT,
      "invalid output size",
    )

  require(
    not set(files).intersection(file_parents(set(files))),
    "output file and directory collision",
  )

  references = []
  reports = set()
  header_bytes = 0

  for folder, node in result["nodes"].items():
    _folder(folder)
    _node(node)

    if folder:
      require(
        files.get(join_path(folder, node["original"])) == node["bytes"],
        "content original missing",
      )
    else:
      require(
        node["original"] == doc["original"]
        and node["sha256"] == doc["sha256"]
        and node["bytes"] == doc["bytes"],
        "root content identity mismatch",
      )

    for child in node["children"]:
      require(
        child in result["nodes"]
        and parent_path(child) == join_path(folder, "contents"),
        "invalid child reference",
      )
      references.append(child)

    if node.get("visuals") is not None:
      _visuals(node["visuals"], folder)

    for step, product in node["products"].items():
      require(applicable(node["kind"], step), "inapplicable product")
      header_bytes += _product(step, product, folder, files)

    for step, attempt in node["attempts"].items():
      require(applicable(node["kind"], step), "inapplicable attempt")
      validate_status(attempt)
      require(
        type(attempt.get("retained")) is bool,
        "invalid retained marker",
      )
      require(
        not attempt["retained"] or step in node["products"],
        "retained product missing",
      )

    if node_index(node):
      reports.add(join_path(folder, "index.md"))

  require(
    len(references) == len(set(references))
    and set(references) == set(result["nodes"]) - {""},
    "disconnected or duplicate content nodes",
  )
  require(
    content_files(result["nodes"]) | reports == set(files),
    "local output inventory mismatch",
  )
  require(
    len(content_images(result["nodes"])) <= IMAGE_LIMIT,
    "too many generated images",
  )

  report_bytes = sum(files[path] for path in reports) + header_bytes
  total = sum(files.values())
  require(
    report_bytes <= REPORT_LIMIT
    and total - report_bytes <= CONTENT_LIMIT
    and total <= ASSET_LIMIT,
    "local output budget exceeded",
  )


def validate_signature(value: Any, size: int) -> None:
  require(
    isinstance(value, dict)
    and set(value) == {"size", "mtime", "ctime", "inode", "device"}
    and all(type(number) is int for number in value.values())
    and value["size"] == size,
    "invalid source signature",
  )


def validate_document(key: str, record: Any) -> None:
  _hash(key, "invalid document key")
  require(
    isinstance(record, dict)
    and isinstance(record.get("owner"), str)
    and record["owner"]
    and isinstance(record.get("url"), str)
    and identity(record["owner"], record["url"]) == key
    and reader_url(record["url"]) == record["url"]
    and isinstance(record.get("name"), str)
    and type(record.get("bytes")) is int
    and 0 < record["bytes"] <= FILE_LIMIT
    and isinstance(record.get("attempts"), dict)
    and set(record["attempts"]) <= STEPS,
    "malformed document",
  )
  _hash(record.get("sha256"), "invalid document identity")
  _member(record.get("role"), ATTACHMENT_ROLES, "invalid attachment role")
  publication_path(record.get("folder"))
  _original(record.get("original"))
  validate_signature(record.get("signature"), record["bytes"])

  if record.get("result") is not None:
    validate_result_data(record["result"], record)

  for attempt in record["attempts"].values():
    require(
      isinstance(attempt, dict)
      and bounded_text(attempt.get("error"), 2048)
      and attempt["error"],
      "malformed root processing attempt",
    )
    _timestamp(attempt.get("finished"))

  for name in ("etag", "last_modified"):
    require(
      name not in record or valid_header(record[name]),
      "invalid HTTP validator",
    )


def validate_item(record: Any) -> None:
  require(
    isinstance(record, dict)
    and isinstance(record.get("title"), str),
    "malformed archive item",
  )
  _member(record.get("status"), ITEM_STATUSES, "invalid item status")

  if record.get("path"):
    publication_path(record["path"])
    require(record["path"].endswith(".md"), "invalid page path")
    require(
      record.get("folder") == parent_path(record["path"]),
      "page folder mismatch",
    )

  if record.get("folder"):
    public_path(record["folder"])
    require(
      record["folder"] == DOCUMENTS
      or record["folder"].startswith(DOCUMENTS + "/"),
      "page folder outside documents",
    )

  span_fields = {"header_bytes", "content_bytes", "content_sha256"}
  if span_fields.intersection(record):
    validate_span(record, TEXT_LIMIT)
    require(
      record["content_bytes"] > 0
      and bool(record.get("path"))
      and "children" not in record,
      "invalid page content",
    )

  for name in ("children", "related", "documents"):
    if name in record:
      _strings(record[name], "invalid item references", unique=True)

  if "problems" in record:
    validate_problems(record["problems"], 100000)

  require(
    record.get("parent") is None
    or isinstance(record.get("parent"), str),
    "invalid parent reference",
  )
  require(
    "empty_listing" not in record
    or type(record["empty_listing"]) is bool,
    "invalid empty-listing marker",
  )

  if "course_id" in record:
    require(
      valid_id(record["course_id"])
      and record.get("parent") == "index"
      and isinstance(record.get("folder"), str)
      and parent_path(record["folder"]) == DOCUMENTS
      and record.get("path") == join_path(record["folder"], "index.md")
      and type(record.get("include_submissions")) is bool,
      "invalid course root",
    )
  else:
    require(
      "include_submissions" not in record,
      "submission policy belongs to a course root",
    )

  for name in ("source", "message", "fetched", "checked", "run"):
    require(
      name not in record or isinstance(record[name], str),
      "invalid item metadata",
    )


def validate_run(run: Any) -> None:
  if run is None:
    return

  require(
    isinstance(run, dict)
    and isinstance(run.get("id"), str)
    and TOKEN.fullmatch(run["id"])
    and isinstance(run.get("command"), str)
    and run["command"] in {"sync", "process"},
    "malformed run record",
  )
  _member(run.get("status"), RUN_STATUSES, "invalid run status")
  _timestamp(run.get("started"))

  if run["status"] != "running":
    _timestamp(run.get("finished"))


def validate_manifest(data: Any) -> None:
  require(
    isinstance(data, dict)
    and data.get("application") == APPLICATION
    and type(data.get("schema")) is int
    and data["schema"] == SCHEMA
    and (data.get("account") is None or valid_id(data["account"])),
    "unsupported archive format; choose a fresh archive directory",
  )

  for section in ("items", "documents"):
    require(
      isinstance(data.get(section), dict)
      and len(data[section]) <= 100000,
      "malformed archive inventory",
    )

  validate_run(data.get("run"))
  pages = set()
  bundles = set()
  courses = set()

  for key, item in data["items"].items():
    require(isinstance(key, str) and key, "invalid item key")
    validate_item(item)

    if item.get("path"):
      require(item["path"] not in pages, "duplicate page path")
      pages.add(item["path"])

    if "course_id" in item:
      course_id = item["course_id"]
      require(course_id not in courses, "duplicate course identity")
      courses.add(course_id)

  for key, doc in data["documents"].items():
    validate_document(key, doc)
    owner = data["items"].get(doc["owner"])
    require(
      owner is not None
      and owner.get("path")
      and parent_path(doc["folder"]) == owner["folder"],
      "invalid document owner",
    )
    require(doc["folder"] not in bundles, "duplicate document folder")
    bundles.add(doc["folder"])

  page_parents = file_parents(pages)
  bundle_parents = file_parents(bundles)

  require(
    not pages.intersection(page_parents),
    "page file and directory collision",
  )
  require(
    not bundles.intersection(bundle_parents),
    "nested document bundles",
  )
  require(
    not pages.intersection(bundles | bundle_parents)
    and not bundles.intersection(page_parents),
    "document and page path collision",
  )
