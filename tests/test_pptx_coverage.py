import copy
import io
import zipfile

from xml.etree import ElementTree

import pytest

import kctl.conversion as conversion

from kctl.content import ContentWalker
from kctl.conversion import Engines, FileProcessor, _Package, _pptx_order
from kctl.conversion import _smartart_findings
from kctl.core import Cancelled, LimitReached, ProtectionFailure, StorageFailure
from kctl.records import attempt_record, outcome, processing_settings, ready
from test_conversion import (
  archived_document,
  body,
  export_and_reuse,
  factory,
  no_network,
  pptx_bytes,
  publish_document,
  rewrite_package,
)


DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
RELATIONSHIP = (
  "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


def smartart_bytes(case="normal"):
  raw = pptx_bytes()
  slide_path = "ppt/slides/slide2.xml"
  rels_path = "ppt/slides/_rels/slide2.xml.rels"
  with zipfile.ZipFile(io.BytesIO(raw)) as source:
    entries = {name: source.read(name) for name in source.namelist()}
  slide = ElementTree.fromstring(entries[slide_path])
  graphic = slide.find(f".//{{{DRAWING}}}graphicData")
  graphic.clear()
  graphic.set("uri", DIAGRAM)
  ElementTree.SubElement(graphic, f"{{{DIAGRAM}}}relIds", {
    f"{{{RELATIONSHIP}}}dm": "diagram",
  })
  rels = ElementTree.fromstring(entries[rels_path])
  relation = ElementTree.SubElement(rels, f"{{{conversion.PACKAGE_NS}}}Relationship", {
    "Id": "diagram", "Type": RELATIONSHIP + "/diagramData",
    "Target": "../diagrams/data1.xml",
  })
  if case == "external":
    relation.set("TargetMode", "External")
    relation.set("Target", "https://example.com/diagram.xml")
  elif case == "escaping":
    relation.set("Target", "../../../outside.xml")
  elif case == "ambiguous":
    rels.append(copy.deepcopy(relation))
  elif case == "missing":
    rels.remove(relation)
  entries[slide_path] = ElementTree.tostring(slide)
  entries[rels_path] = ElementTree.tostring(rels)
  diagram = (
    f'<dgm:dataModel xmlns:dgm="{DIAGRAM}" xmlns:a="{DRAWING}">'
    '<dgm:ptLst><dgm:pt><dgm:t><a:p><a:r><a:t>Répété 한국어 → α</a:t>'
    '</a:r><a:br/><a:r><a:t>    indented</a:t></a:r></a:p>'
    '<a:p><a:r><a:t>Répété 한국어 → α</a:t></a:r></a:p>'
    '</dgm:t></dgm:pt></dgm:ptLst></dgm:dataModel>'
  ).encode()
  entries["ppt/diagrams/data1.xml"] = (
    b"<malformed" if case == "malformed" else diagram
  )
  entries["ppt/diagrams/drawing1.xml"] = diagram
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as target:
    for name, value in entries.items():
      target.writestr(name, value)
  return stream.getvalue()


@pytest.mark.parametrize("case", [
  "normal", "missing", "malformed", "ambiguous", "external", "escaping",
])
def test_smartart_is_reported_without_following_diagram_relationships(
  factory, monkeypatch, case
):
  no_network(monkeypatch)
  engines = Engines()
  raw = smartart_bytes(case)
  processor, output = factory(".pptx", raw, engines=engines)
  original_xml = _Package.xml

  def xml(package, name):
    assert "/diagrams/" not in name
    return original_xml(package, name)

  monkeypatch.setattr(_Package, "xml", xml)
  try:
    product, _ = processor.run()
  finally:
    engines.close()
  text = body(output, product).decode()
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert product["summary"]["coverage"] == [
    "Slide 2: SmartArt frame 1: diagram text is not extracted; "
    "diagram geometry and reading order are not reconstructed",
  ]
  assert "Répété" not in text
  assert text.index("First source") < text.index("Second source")
  assert text.index("Second source") < text.index("Third source")
  assert "\v" not in text
  assert processor.source.read_bytes() == raw


def test_smartart_coverage_uses_presentation_order(factory):
  raw = smartart_bytes()
  with zipfile.ZipFile(io.BytesIO(raw)) as source:
    root = ElementTree.fromstring(source.read("ppt/presentation.xml"))
  listing = next(node for node in root if node.tag.endswith("}sldIdLst"))
  listing[:] = [listing[1], listing[0], listing[2]]
  raw = rewrite_package(raw, {
    "ppt/presentation.xml": ElementTree.tostring(root),
  })
  engines = Engines()
  processor, _ = factory(".pptx", raw, engines=engines)
  try:
    product, _ = processor.run()
  finally:
    engines.close()
  assert product["summary"]["coverage"][0].startswith("Slide 1: SmartArt frame 1:")


def test_smartart_crc_failure_is_not_accepted_as_clean(factory):
  raw = rewrite_package(smartart_bytes(), corrupt=["ppt/slides/slide2.xml"])
  engines = Engines()
  processor, _ = factory(".pptx", raw, engines=engines)
  try:
    product, _ = processor.run()
  finally:
    engines.close()
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert any("ZIP integrity" in finding for finding in product["problems"])


@pytest.mark.parametrize("attribute", ["ENTRY_LIMIT", "DEPTH_LIMIT"])
def test_smartart_inspection_is_bounded(monkeypatch, attribute):
  with zipfile.ZipFile(io.BytesIO(smartart_bytes())) as source:
    package = _Package(source)
    slides, _ = _pptx_order(package)
    slide = package.xml(slides[1])
  monkeypatch.setattr(conversion, attribute, 0)
  with pytest.raises(LimitReached, match="SmartArt inspection"):
    list(_smartart_findings(slide))


@pytest.mark.parametrize("error", [
  StorageFailure("storage failed"), ProtectionFailure("unsafe input"), Cancelled(),
])
def test_smartart_inspection_preserves_fatal_errors(factory, monkeypatch, error):
  engines = Engines()
  processor, _ = factory(".pptx", smartart_bytes(), engines=engines)

  def fail(slide):
    raise error

  monkeypatch.setattr(conversion, "_smartart_findings", fail)
  try:
    with pytest.raises(type(error)) as captured:
      processor.run()
    assert captured.value is error
  finally:
    engines.close()


@pytest.mark.parametrize("failure", ["xml", "inspection"])
def test_many_smartart_inspection_failures_keep_text_and_bounded_findings(
  factory, monkeypatch, failure
):
  processor, output = factory(".pptx", pptx_bytes())
  original_xml = _Package.xml

  def xml(package, name):
    if failure == "xml" and "/slides/slide" in name:
      raise conversion._PackageError("unreadable slide")
    return original_xml(package, name)

  def inspect(slide):
    raise LimitReached("inspection limit")

  monkeypatch.setattr(conversion, "_pptx_order", lambda package: (
    ["ppt/slides/slide1.xml"] * 30, {}
  ))
  monkeypatch.setattr(_Package, "xml", xml)
  if failure == "inspection":
    monkeypatch.setattr(conversion, "_smartart_findings", inspect)

  product, _ = processor.run()

  findings = product["summary"]["coverage"]
  assert len(findings) == 20
  assert findings[-1] == "Additional SmartArt coverage findings omitted"
  assert findings.count(findings[-1]) == 1
  assert "Slide 1: SmartArt inspection incomplete:" in findings[0]
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert body(output, product) == b"Native source\n"


@pytest.mark.parametrize("failure", ["order", "xml", "inspection"])
def test_full_smartart_findings_followed_by_failure_are_bounded(
  factory, monkeypatch, failure
):
  processor, output = factory(".pptx", pptx_bytes())
  for number in range(20):
    processor.coverage(f"Existing finding {number}")

  def fail(*args):
    raise LimitReached("inspection limit")

  if failure == "order":
    monkeypatch.setattr(conversion, "_pptx_order", fail)
  elif failure == "xml":
    monkeypatch.setattr(_Package, "xml", fail)
  else:
    monkeypatch.setattr(conversion, "_smartart_findings", fail)

  product, _ = processor.run()

  findings = product["summary"]["coverage"]
  assert findings == [f"Existing finding {number}" for number in range(19)] + [
    "Additional SmartArt coverage findings omitted",
  ]
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert body(output, product) == b"Native source\n"


def test_smartart_fingerprint_changes_conversion_only():
  current = processing_settings()
  previous = copy.deepcopy(current)
  previous["convert"].pop("pptx_smartart")
  node = {"products": {}, "attempts": {}}
  for step in ("convert", "ocr"):
    product = outcome(previous[step])
    node["products"][step] = product
    node["attempts"][step] = attempt_record(product, retained=False)
  assert not ready(node, "convert", current["convert"])
  assert ready(node, "ocr", current["ocr"])
  assert all(previous[key] == current[key] for key in ("walk", "visuals", "ocr"))
  assert previous["convert"]["version"] == current["convert"]["version"]


def test_smartart_coverage_survives_retention_export_and_reuse(
  private_dir, monkeypatch
):
  no_network(monkeypatch)
  with archived_document(private_dir, ".pptx", smartart_bytes()) as (archive, key):
    current = copy.deepcopy(archive.settings)
    archive.settings["convert"].pop("pptx_smartart")
    with monkeypatch.context() as patch:
      patch.setattr(conversion, "_smartart_findings", lambda slide: iter(()))
      previous = copy.deepcopy(publish_document(archive, key))
    assert previous["products"]["convert"]["status"] == "complete"
    archive.settings = current

    def unexpected(*args, **kwargs):
      pytest.fail("coverage update must reuse discovery, visuals and OCR")

    with monkeypatch.context() as patch:
      patch.setattr(ContentWalker, "walk", unexpected)
      patch.setattr(FileProcessor, "office_visuals", unexpected)
      patch.setattr(Engines, "recognize", unexpected)
      node = publish_document(archive, key)
    assert node["attempts"]["convert"]["retained"] is True
    assert node["attempts"]["convert"]["retry"] is False
    product = node["products"]["convert"]
    old_product = previous["products"]["convert"]
    for field in ("content_sha256", "content_bytes", "settings", "status"):
      assert product[field] == old_product[field]
    assert node["products"]["ocr"] == previous["products"]["ocr"]
    exported = export_and_reuse(archive, key, 3)
    assert b"latest attempt: Slide 2: SmartArt frame 1" in exported
    assert b"Previous result retained" in exported
    assert b"R\xc3\xa9p\xc3\xa9t\xc3\xa9" not in exported
