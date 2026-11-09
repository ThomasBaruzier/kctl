import copy

import pytest

import kctl.records as records

from kctl.core import (
  FILE_LIMIT,
  TEXT_LIMIT,
  BudgetReached,
  Failure,
  LimitReached,
  digest,
  identity,
)
from kctl.records import (
  COVERAGE_OVERFLOW,
  EMPTY_SHA256,
  SCHEMA,
  applicable,
  attempt_record,
  content_files,
  content_images,
  document_files,
  error_outcome,
  file_parents,
  new_manifest,
  node_index,
  node_problems,
  outcome,
  processing_settings,
  product_title,
  ready,
  result_problems,
  step_findings,
  validate_asset,
  validate_document,
  validate_item,
  validate_manifest,
  validate_result_data,
  validate_run,
  validate_span,
  validate_status,
)


STAMP = "2026-11-04T00:00:00+00:00"
SETTINGS = {"version": 1}


def make_item():
  body = "Source content.\n".encode()
  return {
    "title": "Assignment",
    "path": "documents/Course/assignment.md",
    "folder": "documents/Course",
    "parent": "course/course-1",
    "status": "archived",
    "header_bytes": 12,
    "content_bytes": len(body),
    "content_sha256": digest(body),
    "documents": [],
    "related": [],
    "problems": [],
    "empty_listing": False,
  }


def make_course():
  return {
    "title": "Course",
    "path": "documents/Course/index.md",
    "folder": "documents/Course",
    "parent": "index",
    "status": "archived",
    "course_id": "course-1",
    "include_submissions": True,
    "children": ["assignment"],
  }


def make_document():
  raw = b"%PDF-example"
  owner = "assignment"
  url = "https://ctl.kmu.ac.kr/fileDownServlet?id=1"
  doc = {
    "owner": owner,
    "url": url,
    "name": "original.pdf",
    "folder": "documents/Course/Document",
    "original": "original.pdf",
    "role": "attachment",
    "bytes": len(raw),
    "sha256": digest(raw),
    "signature": {
      "size": len(raw),
      "mtime": 1,
      "ctime": 2,
      "inode": 3,
      "device": 4,
    },
    "result": None,
    "attempts": {},
  }
  return identity(owner, url), doc


def make_node(name, kind, original, raw):
  return {
    "name": name,
    "kind": kind,
    "original": original,
    "bytes": len(raw),
    "sha256": digest(raw),
    "retry": False,
    "children": [],
    "problems": [],
    "visuals": None,
    "products": {},
    "attempts": {},
  }


def make_result(doc):
  node = make_node(
    doc["name"], ".pdf", doc["original"], b"%PDF-example"
  )
  return {
    "source": doc["sha256"],
    "settings": SETTINGS,
    "nodes": {"": node},
    "files": {},
    "finished": STAMP,
  }


def add_product(result, step="convert", body=b"Extracted text.\n"):
  path = "text.md" if step == "convert" else "ocr.md"
  product = outcome(
    SETTINGS,
    outputs={path},
    markdown=path,
    content_bytes=len(body),
    content_sha256=digest(body),
  )
  product["header_bytes"] = 20
  result["nodes"][""]["products"][step] = product
  result["files"][path] = 20 + len(body) + 10
  return product


def make_manifest():
  data = new_manifest()
  key, doc = make_document()
  item = make_item()
  item["documents"] = [key]
  data["items"] = {
    "index": {
      "title": "Archive",
      "path": "documents/index.md",
      "folder": "documents",
      "status": "archived",
      "children": ["course/course-1"],
    },
    "course/course-1": make_course(),
    "assignment": item,
  }
  data["documents"][key] = doc
  return data, key


def test_new_manifest_is_valid_and_independent():
  first = new_manifest()
  second = new_manifest()
  validate_manifest(first)

  first["items"]["x"] = {}
  assert second["items"] == {}
  assert first["application"] == "kctl"
  assert first["schema"] == SCHEMA


@pytest.mark.parametrize("field", ["application", "schema", "items", "documents"])
def test_invalid_manifest_header_is_rejected(field):
  data = new_manifest()
  data[field] = "invalid"

  with pytest.raises(Failure):
    validate_manifest(data)


def test_original_script_manifest_is_not_accepted():
  data = new_manifest()
  data["application"] = "ctl"

  with pytest.raises(Failure, match="fresh archive"):
    validate_manifest(data)


def test_manifest_with_course_page_and_attachment_is_valid():
  data, _ = make_manifest()
  validate_manifest(data)


def test_missing_page_references_remain_representable():
  data, _ = make_manifest()
  data["items"]["course/course-1"]["children"].append("missing-page")
  validate_manifest(data)


def test_document_owner_must_exist():
  data, key = make_manifest()
  doc = data["documents"].pop(key)
  doc["owner"] = "missing-page"
  key = identity(doc["owner"], doc["url"])
  data["documents"][key] = doc

  with pytest.raises(Failure, match="owner"):
    validate_manifest(data)


def test_document_must_be_inside_its_owner_folder():
  data, key = make_manifest()
  data["documents"][key]["folder"] = "documents/Elsewhere/Document"

  with pytest.raises(Failure, match="owner"):
    validate_manifest(data)


def test_duplicate_course_identity_is_rejected():
  data, _ = make_manifest()
  other = make_course()
  other["path"] = "documents/Other/index.md"
  other["folder"] = "documents/Other"
  data["items"]["other-course"] = other

  with pytest.raises(Failure, match="course identity"):
    validate_manifest(data)


def test_duplicate_page_path_is_rejected():
  data, _ = make_manifest()
  data["items"]["another-page"] = copy.deepcopy(data["items"]["assignment"])

  with pytest.raises(Failure, match="page path"):
    validate_manifest(data)


def test_page_cannot_be_inside_a_document_bundle():
  data, _ = make_manifest()
  item = make_item()
  item["path"] = "documents/Course/Document/notes.md"
  item["folder"] = "documents/Course/Document"
  data["items"]["nested-page"] = item

  with pytest.raises(Failure, match="collision"):
    validate_manifest(data)


def test_document_folder_cannot_replace_a_page():
  data, key = make_manifest()
  data["documents"][key]["folder"] = "documents/Course/assignment.md"

  with pytest.raises(Failure, match="collision"):
    validate_manifest(data)


def test_duplicate_document_folder_is_rejected():
  data, key = make_manifest()
  other = copy.deepcopy(data["documents"][key])
  other["url"] += "&copy=2"
  data["documents"][identity(other["owner"], other["url"])] = other

  with pytest.raises(Failure, match="duplicate document folder"):
    validate_manifest(data)


@pytest.mark.parametrize("role", ["attachment", "submission"])
def test_document_attachment_roles(role):
  key, doc = make_document()
  doc["role"] = role
  validate_document(key, doc)


@pytest.mark.parametrize(
  ("field", "value"),
  [
    ("role", "unknown"),
    ("role", None),
    ("bytes", True),
    ("bytes", 0),
    ("bytes", FILE_LIMIT + 1),
    ("sha256", "invalid"),
    ("original", "text.md"),
    ("original", "../original.pdf"),
    ("folder", "extracted/Document"),
    ("etag", "value\r\nInjected: yes"),
  ],
)
def test_malformed_document_fields_are_rejected(field, value):
  key, doc = make_document()
  doc[field] = value

  with pytest.raises(Failure):
    validate_document(key, doc)


def test_signature_size_must_match_original():
  key, doc = make_document()
  doc["signature"]["size"] += 1

  with pytest.raises(Failure, match="signature"):
    validate_document(key, doc)


def test_document_key_binds_owner_and_url():
  key, doc = make_document()
  doc["url"] += "&changed=1"

  with pytest.raises(Failure):
    validate_document(key, doc)


def test_submission_policy_requires_a_course_root():
  item = make_item()
  item["include_submissions"] = False

  with pytest.raises(Failure, match="course root"):
    validate_item(item)


def test_course_requires_explicit_submission_policy():
  course = make_course()
  del course["include_submissions"]

  with pytest.raises(Failure, match="course root"):
    validate_item(course)


def test_page_body_and_generated_index_are_mutually_exclusive():
  item = make_item()
  item["children"] = []

  with pytest.raises(Failure, match="page content"):
    validate_item(item)


def test_empty_receipt_is_distinct_from_empty_listing():
  receipt = make_item()
  receipt["status"] = "empty"
  receipt["empty_listing"] = False
  validate_item(receipt)

  listing = copy.deepcopy(receipt)
  listing["empty_listing"] = True
  validate_item(listing)


@pytest.mark.parametrize(
  ("field", "value"),
  [
    ("header_bytes", -1),
    ("header_bytes", True),
    ("content_bytes", -1),
    ("content_bytes", TEXT_LIMIT),
    ("content_sha256", "invalid"),
  ],
)
def test_invalid_content_spans_are_rejected(field, value):
  item = make_item()
  item[field] = value

  with pytest.raises(Failure):
    validate_span(item, 100)


def test_empty_content_has_a_defined_hash():
  product = outcome(SETTINGS)
  validate_span(product, 0)

  product["content_sha256"] = digest(b"not empty")
  with pytest.raises(Failure, match="empty-content"):
    validate_span(product, 0)


def test_page_hash_is_required_with_boundaries():
  item = make_item()
  del item["content_sha256"]

  with pytest.raises(Failure, match="hash"):
    validate_item(item)


def test_status_outcomes_and_attempts():
  product = outcome(SETTINGS, problems=["same", "same", "other"])
  assert product["status"] == "partial"
  assert product["problems"] == ["same", "other"]
  validate_status(product)

  attempt = attempt_record(product, retained=True)
  attempt["settings"]["version"] = 2

  assert product["settings"]["version"] == 1
  assert attempt["retained"] is True


def test_complete_status_cannot_hide_problems_or_retry():
  product = outcome(SETTINGS)
  product["retry"] = True

  with pytest.raises(Failure, match="complete"):
    validate_status(product)

  product["retry"] = False
  product["problems"] = ["omitted content"]

  with pytest.raises(Failure, match="complete"):
    validate_status(product)


def test_fixed_and_remaining_budgets_have_different_retry_policy():
  fixed = error_outcome(SETTINGS, LimitReached("fixed limit"))
  remaining = error_outcome(SETTINGS, BudgetReached("remaining budget"))
  failed = error_outcome(SETTINGS, Failure("decoder failed"))

  assert fixed["status"] == "partial"
  assert fixed["retry"] is False
  assert remaining["status"] == "partial"
  assert remaining["retry"] is True
  assert failed["status"] == "failed"
  assert failed["retry"] is True


def test_passthrough_formats_do_not_require_processing_products():
  for kind in (".txt", ".md", ".py", "text"):
    assert not applicable(kind, "convert")
    assert not applicable(kind, "ocr")

  assert applicable(".html", "convert")
  assert applicable(".html", "ocr")
  assert not applicable(".pdf", "unknown")


def test_readiness_respects_latest_matching_attempt():
  node = make_node("x.pdf", ".pdf", "x.pdf", b"x")
  node["products"]["convert"] = outcome(SETTINGS)
  assert ready(node, "convert", SETTINGS)

  attempt = outcome(SETTINGS, problems=["failed"], retry=True, failed=True)
  node["attempts"]["convert"] = attempt_record(attempt, retained=True)
  assert not ready(node, "convert", SETTINGS)

  attempt["retry"] = False
  node["attempts"]["convert"] = attempt_record(attempt, retained=True)
  assert ready(node, "convert", SETTINGS)
  assert not ready(node, "convert", {"version": 2})


def test_result_without_products_is_valid():
  _, doc = make_document()
  result = make_result(doc)
  validate_result_data(result, doc)


def test_product_boundaries_allow_a_generated_appendix():
  _, doc = make_document()
  result = make_result(doc)
  product = add_product(result)

  validate_result_data(result, doc)
  assert product["header_bytes"] + product["content_bytes"] < (
    result["files"]["text.md"]
  )


def test_product_outputs_must_exist_in_inventory():
  _, doc = make_document()
  result = make_result(doc)
  add_product(result)
  del result["files"]["text.md"]

  with pytest.raises(Failure, match="missing"):
    validate_result_data(result, doc)


def test_untracked_output_is_rejected():
  _, doc = make_document()
  result = make_result(doc)
  result["files"]["unexpected.txt"] = 1

  with pytest.raises(Failure, match="inventory"):
    validate_result_data(result, doc)


def test_root_identity_must_match_document():
  _, doc = make_document()
  result = make_result(doc)
  result["nodes"][""]["sha256"] = digest(b"other")

  with pytest.raises(Failure, match="root content"):
    validate_result_data(result, doc)


def test_retained_attempt_requires_its_product():
  _, doc = make_document()
  result = make_result(doc)
  attempt = outcome(
    SETTINGS, problems=["failed"], retry=True, failed=True
  )
  result["nodes"][""]["attempts"]["convert"] = attempt_record(
    attempt, retained=True
  )
  result["files"]["index.md"] = 20

  with pytest.raises(Failure, match="retained product missing"):
    validate_result_data(result, doc)


def test_problem_index_and_retained_product_are_accounted_for():
  _, doc = make_document()
  result = make_result(doc)
  add_product(result)
  attempt = outcome(
    SETTINGS, problems=["decoder failed"], retry=True, failed=True
  )
  node = result["nodes"][""]
  node["attempts"]["convert"] = attempt_record(attempt, retained=True)
  result["files"]["index.md"] = 80

  validate_result_data(result, doc)
  assert node_index(node)
  assert "convert, latest attempt: decoder failed" in node_problems(node)
  assert result_problems(result)
  assert product_title(
    node, "convert", {"convert": SETTINGS}, "text"
  ) == "text (retained)"


def test_nested_originals_keep_source_names():
  _, doc = make_document()
  result = make_result(doc)
  child = "contents/main"
  result["nodes"][""]["children"] = [child]
  result["nodes"][child] = make_node(
    "project/src/main.py", ".py", "main.py", b"print('hello')\n"
  )
  result["files"][child + "/main.py"] = result["nodes"][child]["bytes"]
  result["files"]["index.md"] = 30

  validate_result_data(result, doc)
  assert result["nodes"][child]["name"] == "project/src/main.py"


def test_disconnected_content_node_is_rejected():
  _, doc = make_document()
  result = make_result(doc)
  result["nodes"]["contents/orphan"] = make_node(
    "orphan.txt", ".txt", "orphan.txt", b"x"
  )
  result["files"]["contents/orphan/orphan.txt"] = 1

  with pytest.raises(Failure, match="disconnected"):
    validate_result_data(result, doc)


def test_visual_inventory_uses_content_addressed_rasters():
  _, doc = make_document()
  result = make_result(doc)
  key = digest(b"image")
  path = f"images/{key}.png"
  visuals = outcome(SETTINGS, outputs={path}) | {
    "images": [{
      "path": path,
      "key": key,
      "blank": False,
      "label": "Page 1",
      "page": 1,
    }],
  }
  result["nodes"][""]["visuals"] = visuals
  result["files"][path] = 5

  validate_result_data(result, doc)
  assert content_images(result["nodes"]) == {path}
  assert content_files(result["nodes"]) == {path}

  visuals["images"][0]["key"] = digest(b"other")
  with pytest.raises(Failure, match="normalized raster"):
    validate_result_data(result, doc)


@pytest.mark.parametrize(
  "path",
  [
    "images/not-a-hash.png",
    "other/" + "a" * 64 + ".png",
    "images/" + "a" * 64 + ".jpg",
    "../images/" + "a" * 64 + ".png",
  ],
)
def test_invalid_generated_asset_paths(path):
  with pytest.raises(Failure):
    validate_asset(path, "")


def test_inventory_helpers():
  key, doc = make_document()
  result = make_result(doc)
  add_product(result)
  doc["result"] = result

  validate_document(key, doc)
  assert document_files(doc) == {"original.pdf", "text.md"}
  assert file_parents({"a/b/c.txt", "a/d.txt"}) == {"a", "a/b"}


def test_run_records_do_not_include_export_runs():
  run = {
    "id": "a" * 32,
    "command": "sync",
    "status": "running",
    "started": STAMP,
  }
  validate_run(run)

  run["status"] = "complete"
  with pytest.raises(Failure, match="timestamp"):
    validate_run(run)

  run["finished"] = STAMP
  validate_run(run)

  run["command"] = "export"
  with pytest.raises(Failure, match="run record"):
    validate_run(run)


def test_settings_include_relevant_dependency_versions(monkeypatch):
  monkeypatch.setattr(
    records, "package_version", lambda name: "version:" + name
  )
  settings = processing_settings()

  assert settings["convert"]["markitdown"] == "version:markitdown"
  assert settings["convert"]["defusedxml"] == "version:defusedxml"
  assert settings["ocr"]["engine"] == "version:tesserocr"
  assert settings["ocr"]["language"] == "eng"
  assert settings["walk"]["zstandard"] == "version:zstandard"
  assert settings["convert"]["visuals"] == settings["visuals"]
  assert settings["ocr"]["visuals"] == settings["visuals"]
  assert outcome(SETTINGS)["content_sha256"] == EMPTY_SHA256


def test_fidelity_settings_identify_the_changed_processing_boundaries():
  settings = processing_settings()

  assert settings["walk"]["integrity"] == 1
  assert settings["visuals"]["office_integrity"] == 1
  assert settings["visuals"]["pdf_visuals"] == 2
  assert settings["visuals"]["html_media"] == 1
  assert settings["convert"]["office_integrity"] == 1
  assert settings["convert"]["pdf_coverage"] == 2
  assert settings["convert"]["pdf_vectors"] == 1
  assert settings["convert"]["pptx_recovery"] == 1
  assert settings["convert"]["html_source"] == 3
  assert "pdf_coverage" not in settings["walk"]
  assert "pdf_coverage" not in settings["ocr"]
  assert "pptx_recovery" not in settings["walk"]
  assert "pptx_recovery" not in settings["ocr"]
  assert "office_integrity" not in settings["walk"]
  assert "office_integrity" not in settings["ocr"]
  assert "html_source" not in settings["walk"]
  assert "html_source" not in settings["visuals"]
  assert "html_source" not in settings["ocr"]


def test_office_failure_correction_invalidates_conversion_only():
  settings = processing_settings()
  previous = copy.deepcopy(settings)
  previous["convert"].pop("office_integrity")
  node = make_node("source.docx", ".docx", "source.docx", b"source")

  for step in ("convert", "ocr"):
    product = outcome(previous[step], problems=["Known limitation"])
    node["products"][step] = product
    node["attempts"][step] = attempt_record(product, retained=False)

  assert not ready(node, "convert", settings["convert"])
  assert ready(node, "ocr", settings["ocr"])
  assert previous["walk"] == settings["walk"]
  assert previous["visuals"] == settings["visuals"]
  assert previous["ocr"] == settings["ocr"]
  assert previous["convert"]["version"] == settings["convert"]["version"]


def test_html_boundary_update_invalidates_conversion_only():
  settings = processing_settings()
  previous = copy.deepcopy(settings)
  previous["convert"]["html_source"] -= 1
  node = make_node("source.html", ".html", "source.html", b"source")

  for step in ("convert", "ocr"):
    product = outcome(previous[step], problems=["Known limitation"])
    node["products"][step] = product
    node["attempts"][step] = attempt_record(product, retained=False)

  assert not ready(node, "convert", settings["convert"])
  assert ready(node, "ocr", settings["ocr"])
  assert previous["walk"] == settings["walk"]
  assert previous["visuals"] == settings["visuals"]
  assert previous["ocr"] == settings["ocr"]
  changed = {
    key for key in settings["convert"]
    if previous["convert"][key] != settings["convert"][key]
  }
  assert changed == {"html_source"}


def test_retained_attempt_copies_coverage_but_not_unrelated_product_statistics():
  warning = "PDF pages 1-3 contain annotations not represented as native text"
  product = outcome(
    SETTINGS,
    problems=[warning],
    summary={
      "coverage": [warning],
      "pages": 3,
      "images": 10,
      "native_text_characters": 1000,
    },
  )

  attempt = attempt_record(product, retained=True)

  assert attempt["summary"] == {"coverage": [warning]}
  assert attempt["problems"] == [warning]
  assert attempt["retained"] is True
  assert attempt["retry"] is False
  attempt["summary"]["coverage"].append("Another finding")
  assert product["summary"]["coverage"] == [warning]


def test_attempt_without_coverage_does_not_gain_a_summary():
  product = outcome(SETTINGS, summary={"images": 2})

  attempt = attempt_record(product, retained=False)

  assert "summary" not in attempt
  validate_status(attempt)


def test_coverage_is_not_reported_as_complete_processing():
  warning = "Native text contains U+FFFE on page 1"
  product = outcome(
    SETTINGS,
    problems=[warning],
    summary={"coverage": [warning]},
  )

  assert product["status"] == "partial"
  assert product["retry"] is False
  validate_status(product)
  product["status"] = "complete"

  with pytest.raises(Failure, match="complete"):
    validate_status(product)


@pytest.mark.parametrize("attempt", [False, True])
@pytest.mark.parametrize(
  "coverage",
  [
    None,
    "not a list",
    {"message": "not a list"},
    [1],
    ["unsafe\nmessage"],
    ["unsafe\x00message"],
    ["x" * 2049],
    ["message"] * 21,
  ],
)
def test_coverage_records_are_bounded_and_validated(coverage, attempt):
  value = outcome(SETTINGS, problems=["known limitation"])
  if attempt:
    value = attempt_record(value, retained=True)
  value["summary"] = {"coverage": coverage}

  with pytest.raises(Failure):
    validate_status(value)


@pytest.mark.parametrize("summary", [None, [], "invalid"])
def test_attempt_summary_must_be_a_mapping_when_present(summary):
  value = attempt_record(
    outcome(SETTINGS, problems=["known limitation"]), retained=True
  )
  value["summary"] = summary

  with pytest.raises(Failure):
    validate_status(value)


def test_coverage_survives_document_validation_with_a_retained_product():
  key, doc = make_document()
  result = make_result(doc)
  product = add_product(result)
  warning = "Native text contains U+FFFD on page 2"
  latest = outcome(
    SETTINGS,
    problems=[warning],
    summary={"coverage": [warning]},
  )
  node = result["nodes"][""]
  node["attempts"]["convert"] = attempt_record(latest, retained=True)
  result["files"]["index.md"] = 80
  doc["result"] = result

  validate_document(key, doc)

  assert product["status"] == "complete"
  assert node["attempts"]["convert"]["summary"]["coverage"] == [warning]
  assert warning in node["attempts"]["convert"]["problems"]
  assert node_index(node)
  assert ready(node, "convert", SETTINGS)


def test_existing_results_without_coverage_remain_valid():
  data, key = make_manifest()
  doc = data["documents"][key]
  result = make_result(doc)
  product = add_product(result)
  result["nodes"][""]["attempts"]["convert"] = attempt_record(
    product, retained=False
  )
  doc["result"] = result

  validate_manifest(data)

  assert "coverage" not in product["summary"]
  assert "summary" not in result["nodes"][""]["attempts"]["convert"]
  assert data["schema"] == SCHEMA


def test_retained_attempt_merges_distinct_findings_without_mutating_inputs():
  previous = attempt_record(
    outcome(
      SETTINGS,
      summary={"coverage": ["Earlier text concern"]},
      problems=["Earlier processing failure"],
    ),
    retained=True,
  )
  current = outcome(
    {"version": 2},
    summary={
      "coverage": ["New annotation concern", "Earlier text concern"],
      "images": 7,
    },
    problems=["Latest processing failure"],
    retry=True,
  )
  before_previous = copy.deepcopy(previous)
  before_current = copy.deepcopy(current)

  attempt = attempt_record(current, retained=True, previous=previous)

  assert attempt["summary"] == {
    "coverage": ["Earlier text concern", "New annotation concern"],
  }
  assert attempt["problems"] == current["problems"]
  assert "Earlier processing failure" not in attempt["problems"]
  assert attempt["settings"] == {"version": 2}
  assert attempt["retry"] is True
  assert attempt["finished"] == current["finished"]
  assert previous == before_previous
  assert current == before_current
  validate_status(attempt)

  attempt["summary"]["coverage"].append("Changed returned list")
  assert previous == before_previous
  assert current == before_current


def test_failed_attempt_without_new_coverage_keeps_retained_findings():
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": ["Known coverage concern"]}),
    retained=True,
  )
  current = error_outcome(SETTINGS, Failure("Decoder failed"))

  attempt = attempt_record(current, retained=True, previous=previous)

  assert attempt["summary"] == {"coverage": ["Known coverage concern"]}
  assert attempt["problems"] == ["Decoder failed"]
  assert attempt["status"] == "failed"
  assert attempt["retry"] is True
  validate_status(attempt)


def test_coverage_accumulation_keeps_first_seen_order_without_duplicate_growth():
  first = outcome(
    SETTINGS, summary={"coverage": ["First finding", "Second finding"]}
  )
  previous = attempt_record(first, retained=True)
  current = outcome(
    SETTINGS, summary={"coverage": ["Second finding", "Third finding"]}
  )

  for _ in range(5):
    previous = attempt_record(current, retained=True, previous=previous)

  assert previous["summary"]["coverage"] == [
    "First finding", "Second finding", "Third finding",
  ]
  assert previous["retry"] is False
  validate_status(previous)


def test_exact_coverage_bound_does_not_add_an_overflow_marker():
  findings = [f"Coverage finding {index}" for index in range(20)]
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": findings[:10]}),
    retained=True,
  )
  current = outcome(
    SETTINGS, summary={"coverage": findings[10:] + findings[:5]}
  )

  attempt = attempt_record(current, retained=True, previous=previous)

  assert attempt["summary"]["coverage"] == findings
  assert COVERAGE_OVERFLOW not in attempt["summary"]["coverage"]
  validate_status(attempt)


def test_coverage_overflow_is_explicit_and_stays_with_retained_output():
  findings = [f"Coverage finding {index}" for index in range(20)]
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": findings}), retained=True
  )
  current = outcome(SETTINGS, summary={"coverage": ["Additional finding"]})
  expected = findings[:19] + [COVERAGE_OVERFLOW]

  attempt = attempt_record(current, retained=True, previous=previous)

  assert attempt["summary"]["coverage"] == expected
  assert len(attempt["summary"]["coverage"]) == 20
  assert COVERAGE_OVERFLOW.isascii()
  validate_status(attempt)

  for latest in (
    error_outcome(SETTINGS, Failure("Later decoder failure")),
    outcome(SETTINGS, summary={"coverage": [findings[0]]}),
    outcome(SETTINGS, summary={"coverage": ["Another new finding"]}),
  ):
    attempt = attempt_record(latest, retained=True, previous=attempt)
    assert attempt["summary"]["coverage"] == expected
    assert attempt["summary"]["coverage"].count(COVERAGE_OVERFLOW) == 1
    validate_status(attempt)


@pytest.mark.parametrize("partial", [False, True])
def test_accepted_replacement_does_not_inherit_old_retained_coverage(partial):
  previous = attempt_record(
    outcome(
      SETTINGS,
      summary={"coverage": [f"Old finding {index}" for index in range(20)]},
    ),
    retained=True,
  )
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": ["Overflow finding"]}),
    retained=True,
    previous=previous,
  )
  assert COVERAGE_OVERFLOW in previous["summary"]["coverage"]
  current = outcome(
    SETTINGS,
    summary={"coverage": ["Current finding"]} if partial else {},
  )

  attempt = attempt_record(current, retained=False, previous=previous)

  assert attempt["retained"] is False
  if partial:
    assert attempt["summary"] == {"coverage": ["Current finding"]}
    assert attempt["status"] == "partial"
  else:
    assert "summary" not in attempt
    assert attempt["status"] == "complete"
  validate_status(attempt)


def test_accumulated_findings_remain_visible_to_indexes_and_reuse_checks():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["products"]["convert"] = outcome(SETTINGS)
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": ["Earlier finding"]}),
    retained=True,
  )
  latest = outcome(SETTINGS, summary={"coverage": ["Latest finding"]})
  node["attempts"]["convert"] = attempt_record(
    latest, retained=True, previous=previous
  )

  assert node_index(node)
  assert node_problems(node) == [
    "convert, latest attempt: Latest finding",
    "convert, retained coverage, attempt unknown: Earlier finding",
  ]
  assert ready(node, "convert", SETTINGS)


@pytest.mark.parametrize("step", ["convert", "ocr"])
def test_step_findings_combine_exact_duplicates_and_preserve_attribution(step):
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["products"][step] = outcome(
    SETTINGS,
    problems=["Shared finding", "Published issue"],
    summary={"coverage": ["Shared finding"]},
  )
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": ["Accumulated finding"]}),
    retained=True,
  )
  latest = outcome(
    SETTINGS,
    problems=["Shared finding", "Current issue"],
    summary={"coverage": ["Shared finding"]},
  )
  node["attempts"][step] = attempt_record(
    latest, retained=True, previous=previous
  )
  before = copy.deepcopy(node)

  findings = step_findings(node, step)

  assert findings == {
    "Shared finding": ["published result", "latest attempt"],
    "Published issue": ["published result"],
    "Current issue": ["latest attempt"],
    "Accumulated finding": ["retained coverage, attempt unknown"],
  }
  assert list(findings) == [
    "Shared finding", "Published issue", "Current issue", "Accumulated finding",
  ]
  assert node_problems(node) == [
    f"{step}, published result / latest attempt: Shared finding",
    f"{step}, published result: Published issue",
    f"{step}, latest attempt: Current issue",
    f"{step}, retained coverage, attempt unknown: Accumulated finding",
  ]
  assert node == before
  assert ready(node, step, SETTINGS)
  findings["Shared finding"].append("Changed returned attribution")
  assert node == before


def test_published_finding_is_not_relabelled_as_an_unknown_attempt():
  warning = "Published annotation finding"
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["products"]["convert"] = outcome(
    SETTINGS, summary={"coverage": [warning]}
  )
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": [warning]}),
    retained=True,
  )
  node["attempts"]["convert"] = attempt_record(
    error_outcome(SETTINGS, Failure("Latest decoder failure")),
    retained=True,
    previous=previous,
  )

  assert step_findings(node, "convert") == {
    warning: ["published result"],
    "Latest decoder failure": ["latest attempt"],
  }
  assert not ready(node, "convert", SETTINGS)


def test_retained_summary_only_finding_does_not_claim_historical_or_latest_origin():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["products"]["convert"] = outcome(SETTINGS)
  candidate = error_outcome(SETTINGS, BudgetReached("Root budget reached"))
  candidate["summary"]["coverage"] = ["Candidate coverage finding"]
  node["attempts"]["convert"] = attempt_record(candidate, retained=True)

  assert step_findings(node, "convert") == {
    "Root budget reached": ["latest attempt"],
    "Candidate coverage finding": ["retained coverage, attempt unknown"],
  }
  validate_status(node["attempts"]["convert"])


def test_nonretained_summary_only_finding_belongs_to_that_attempt():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  candidate = error_outcome(SETTINGS, BudgetReached("Root budget reached"))
  candidate["summary"]["coverage"] = ["Candidate coverage finding"]
  node["attempts"]["convert"] = attempt_record(candidate, retained=False)

  assert step_findings(node, "convert") == {
    "Candidate coverage finding": ["latest attempt"],
    "Root budget reached": ["latest attempt"],
  }


def test_accepted_product_and_attempt_share_one_finding_with_both_labels():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  product = outcome(
    SETTINGS,
    problems=["Same finding"],
    summary={"coverage": ["Same finding"]},
  )
  node["products"]["convert"] = product
  node["attempts"]["convert"] = attempt_record(product, retained=False)

  assert step_findings(node, "convert") == {
    "Same finding": ["published result", "latest attempt"],
  }
  assert node_problems(node) == [
    "convert, published result / latest attempt: Same finding",
  ]


def test_attribution_does_not_merge_similar_warning_text():
  published = "Native text contains U+FFFE: 2 on pages 1-3"
  current = "Native text contains U+0004: 1, U+FFFE: 2 on pages 1-3"
  accumulated = "Native text contains U+FFFE: 1 on page 1"
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["products"]["convert"] = outcome(
    SETTINGS, summary={"coverage": [published]}
  )
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": [accumulated]}),
    retained=True,
  )
  node["attempts"]["convert"] = attempt_record(
    outcome(SETTINGS, summary={"coverage": [current]}),
    retained=True,
    previous=previous,
  )

  assert step_findings(node, "convert") == {
    published: ["published result"],
    current: ["latest attempt"],
    accumulated: ["retained coverage, attempt unknown"],
  }


def test_attribution_preserves_overflow_and_latest_findings_outside_accumulation():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["products"]["convert"] = outcome(SETTINGS)
  old = [f"Old finding {number}" for number in range(20)]
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": old}), retained=True
  )
  previous = attempt_record(
    outcome(SETTINGS, summary={"coverage": ["Overflow finding"]}),
    retained=True,
    previous=previous,
  )
  latest = outcome(SETTINGS, summary={"coverage": ["Latest finding"]})
  node["attempts"]["convert"] = attempt_record(
    latest, retained=True, previous=previous
  )
  before = copy.deepcopy(node)

  findings = step_findings(node, "convert")

  assert findings["Latest finding"] == ["latest attempt"]
  assert findings[COVERAGE_OVERFLOW] == ["retained coverage, attempt unknown"]
  assert len(findings) == 21
  assert set(old[:19]) <= findings.keys()
  assert node == before
  assert ready(node, "convert", SETTINGS)


def test_findings_remain_step_local_and_do_not_absorb_discovery_problems():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  node["problems"] = ["Discovery limitation"]
  node["products"]["convert"] = outcome(SETTINGS, problems=["Same text"])
  node["products"]["ocr"] = outcome(SETTINGS, problems=["Same text"])

  assert step_findings(node, "convert") == {
    "Same text": ["published result"],
  }
  assert step_findings(node, "ocr") == {
    "Same text": ["published result"],
  }
  assert node_problems(node) == [
    "Discovery limitation",
    "convert, published result: Same text",
    "ocr, published result: Same text",
  ]


def test_clean_or_unprocessed_steps_do_not_gain_findings():
  node = make_node("source.pdf", ".pdf", "source.pdf", b"source")
  assert step_findings(node, "convert") == {}

  product = outcome(SETTINGS)
  node["products"]["convert"] = product
  node["attempts"]["convert"] = attempt_record(product, retained=False)

  assert step_findings(node, "convert") == {}
  assert node_problems(node) == []
  assert not node_index(node)
  assert ready(node, "convert", SETTINGS)
