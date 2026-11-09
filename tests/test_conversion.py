import base64
import copy
import io
import socket
import sys
import zipfile

from contextlib import closing, contextmanager
from html import escape
from pathlib import Path
from types import ModuleType, SimpleNamespace
from xml.etree import ElementTree

import pytest

import kctl.conversion as conversion

from PIL import Image

from kctl.archive import Archive
from kctl.auth_html import soup_html
from kctl.content import ContentWalker, Output
from kctl.conversion import (
  Engines,
  FileProcessor,
  _Package,
  _PackageError,
  _annotations,
  _integrity_failure,
  _mermaid_markup,
  _package_target,
  _page_ranges,
  _pdf_vectors,
  _pptx_order,
  _suspect_codes,
  embedded_raster,
  image_descriptions,
  normalize_image,
  prepare_html,
  processing_error,
)
from kctl.core import (
  BudgetReached,
  Cancelled,
  Failure,
  LimitReached,
  ProtectionFailure,
  StorageFailure,
  digest,
  identity,
)
from kctl.exports import export_courses
from kctl.processing import DocumentJob, Processor
from kctl.records import EMPTY_SHA256, processing_settings, validate_span
from kctl.storage import signature, write_new


def raster_bytes(color="white", *, nonblank=False, size=(12, 10)):
  stream = io.BytesIO()
  with Image.new("RGB", size, color) as image:
    if nonblank:
      image.putpixel((0, 0), (0, 0, 0))
    image.save(stream, format="PNG")
  return stream.getvalue()


def data_uri(raw, mime="image/png"):
  return f"data:{mime};base64," + base64.b64encode(raw).decode()


def office_bytes(entries=()):
  stream = io.BytesIO()
  with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("[Content_Types].xml", "<Types/>")
    archive.writestr("word/document.xml", "<document/>")
    for name, raw in entries:
      archive.writestr(name, raw)
  return stream.getvalue()


class FakeEngines:
  def __init__(self, markdown="Native source\n", recognized="Recognized text"):
    self.text = markdown
    self.recognized = recognized
    self.markdown_calls = []
    self.ocr_calls = []
    self.error = None

  def markdown(self, path, kind, raw=None):
    self.markdown_calls.append((path, kind, raw))
    if self.error is not None:
      raise self.error
    return self.text

  def recognize(self, image):
    self.ocr_calls.append(image.size)
    if self.error is not None:
      raise self.error
    return self.recognized


@pytest.fixture
def factory(private_dir):
  count = 0

  def create(
    kind,
    raw,
    *,
    step="convert",
    engines=None,
    cached=None,
    existing=None,
    protected=None,
    protected_images=None,
    force=False,
    folder="",
  ):
    nonlocal count
    count += 1
    root = private_dir / str(count)
    root.mkdir(mode=0o700)
    source = root / ("original" + kind)
    write_new(source, raw)
    destination = root / "candidate"
    destination.mkdir(mode=0o700)
    output = Output(
      destination,
      existing=existing,
      protected=protected,
      protected_images=protected_images,
    )
    node = {
      "name": source.name,
      "original": source.name,
      "kind": kind,
      "bytes": len(raw),
      "sha256": digest(raw),
    }
    processor = FileProcessor(
      source,
      output,
      folder,
      node,
      step,
      processing_settings(),
      engines or FakeEngines(),
      cached,
      force,
    )
    return processor, output

  return create


def body(output, product):
  if product["markdown"] is None:
    return b""
  raw = output.resolve(product["markdown"]).path.read_bytes()
  start = product["header_bytes"]
  return raw[start:start + product["content_bytes"]]


def complete_bytes(output, product):
  return output.resolve(product["markdown"]).path.read_bytes()


def no_network(monkeypatch):
  def fail(*args, **kwargs):
    raise AssertionError("conversion attempted network access")

  import urllib.request
  import requests

  monkeypatch.setattr(socket, "create_connection", fail)
  monkeypatch.setattr(urllib.request, "urlopen", fail)
  monkeypatch.setattr(requests.sessions.Session, "request", fail)


def test_normalization_composites_transparency_onto_white():
  with Image.new("RGBA", (2, 1), (255, 0, 0, 0)) as image:
    image.putpixel((1, 0), (0, 0, 0, 255))
    image.info["description"] = "unnecessary metadata"
    normalized = normalize_image(image)

    try:
      assert normalized.mode == "RGB"
      assert normalized.getpixel((0, 0)) == (255, 255, 255)
      assert normalized.getpixel((1, 0)) == (0, 0, 0)
      assert normalized.info == {}
    finally:
      normalized.close()


def test_normalization_applies_exif_orientation():
  with Image.new("RGB", (2, 3), "white") as image:
    image.getexif()[274] = 6
    normalized = normalize_image(image)

    try:
      assert normalized.size == (3, 2)
      assert normalized.info == {}
    finally:
      normalized.close()

    assert image.size == (2, 3)


def test_normalization_checks_pixels_before_conversion(monkeypatch):
  monkeypatch.setattr(conversion, "PIXEL_LIMIT", 3)

  with Image.new("RGB", (2, 2)) as image:
    with pytest.raises(LimitReached, match="pixel limit"):
      normalize_image(image)


def test_decoder_bomb_errors_become_nonretryable_limits():
  for error in (
    Image.DecompressionBombWarning("too large"),
    Image.DecompressionBombError("too large"),
  ):
    assert isinstance(processing_error(error), LimitReached)

  ordinary = ValueError("failure")
  assert processing_error(ordinary) is ordinary


def test_markdown_image_descriptions_preserve_alt_text_and_code_examples():
  text = (
    "Before ![A diagram](images/a.png) after\n"
    "![Decorative]()\n"
    "`![literal](data:image/png;base64,AAAA)`\n"
    "```markdown\n"
    "![code example](images/example.png)\n"
    "```\n"
    "![Another](<images/a b.png>)\n"
  )
  result = image_descriptions(text)

  assert "Before A diagram after" in result
  assert "Decorative" in result
  assert "`![literal](data:image/png;base64,AAAA)`" in result
  assert "![code example](images/example.png)" in result
  assert "Another\n" in result
  assert "images/a.png" not in result
  assert "images/a b.png" not in result


def test_markdown_image_cleanup_handles_tilde_fences_and_escaped_labels():
  text = (
    "~~~markdown\n![example](image.png)\n~~~~\n"
    r"![A \] bracket](image.png)" + "\n"
  )

  assert image_descriptions(text) == (
    "~~~markdown\n![example](image.png)\n~~~~\n"
    r"A \] bracket" + "\n"
  )


def test_html_preparation_removes_scaffolding_but_preserves_title_and_body():
  raw = (
    "<html><head><title>Lecture title</title>"
    "<style>body { color: red }</style>"
    "<script>window.secret = 'script content'</script></head>"
    "<body><h2>Topic</h2><p>Actual lesson</p>"
    "<!-- private comment --></body></html>"
  ).encode()
  prepared = prepare_html(raw)
  markup = prepared.markup.decode()

  assert "Lecture title" in markup
  assert "Actual lesson" in markup
  assert "Topic" in markup
  assert "script content" not in markup
  assert "color: red" not in markup
  assert "private comment" not in markup
  assert prepared.problems == []


def test_html_title_is_not_added_when_already_a_visible_heading():
  prepared = prepare_html(
    b"<html><head><title>Lecture</title></head>"
    b"<body><h1>Lecture</h1></body></html>"
  )

  assert prepared.markup.decode().count("Lecture") == 1


def test_html_code_preserves_indentation_br_elements_and_literal_data_uris():
  raw = (
    '<pre><code class="language-python">'
    '<span>def example():</span><br>'
    '    return "data:image/png;base64,AAAA"\n'
    "</code></pre>"
  ).encode()
  prepared = prepare_html(raw)

  assert prepared.images == []
  assert len(prepared.literals) == 1
  literal = next(iter(prepared.literals.values()))
  assert literal == (
    "```python\n"
    "def example():\n"
    '    return "data:image/png;base64,AAAA"\n'
    "```\n"
  )
  assert "data:image" not in prepared.markup.decode()


@pytest.mark.parametrize(
  "html",
  [
    '<pre class="mermaid">graph TD\n A-->B</pre>',
    '<div class="mermaid">graph TD\n A-->B</div>',
    '<code class="language-mermaid">graph TD\n A-->B</code>',
    '<script type="text/vnd.mermaid">graph TD\n A-->B</script>',
    '<pre><code class="language-mermaid">graph TD\n A-->B</code></pre>',
  ],
)
def test_html_mermaid_source_is_protected_once(html):
  prepared = prepare_html(html.encode())

  assert len(prepared.literals) == 1
  assert next(iter(prepared.literals.values())) == (
    "```mermaid\ngraph TD\n A-->B\n```\n"
  )


def test_html_textarea_content_is_preserved_as_code():
  prepared = prepare_html(b"<textarea>  example\n    line</textarea>")

  assert next(iter(prepared.literals.values())) == (
    "```\n  example\n    line\n```\n"
  )


def test_html_preparation_only_extracts_document_image_elements():
  uri = data_uri(raster_bytes())
  raw = (
    f"<style>.icon {{ background: url({uri}); }}</style>"
    f'<img aria-hidden="true" src="{uri}">'
    f'<img role="presentation" src="{uri}">'
    f'<img src="{uri}" alt="Useful figure">'
  ).encode()
  prepared = prepare_html(raw)

  assert prepared.images == [(uri, "Useful figure")]
  assert b"base64" not in prepared.markup
  assert b"Useful figure" in prepared.markup


def test_html_external_resources_keep_descriptions_without_fetching(monkeypatch):
  no_network(monkeypatch)
  prepared = prepare_html(
    b'<p>Lesson</p><img src="https://example.com/image.png" alt="Diagram">'
    b'<iframe src="https://example.com/" title="Interactive exercise"></iframe>'
    b'<svg aria-label="Vector diagram"><path d="M 0 0"/></svg>'
  )

  markup = prepared.markup.decode()
  assert "Diagram" in markup
  assert "Interactive exercise" in markup
  assert "Vector diagram" in markup
  assert "https://example.com" not in markup
  assert prepared.images == []
  assert len(prepared.problems) == 3


def test_html_preparation_does_not_report_scripts_as_missing_content():
  prepared = prepare_html(
    b"<h1>Complete static lesson</h1>"
    b"<script>initializeDecorativeWidget()</script>"
  )

  assert prepared.problems == []


def test_html_preparation_preserves_useful_links_and_removes_data_targets():
  prepared = prepare_html((
    '<a href="https://example.com/reference">Reference</a>'
    '<a href="data:application/octet-stream;base64,AAAA">Attachment name</a>'
    '<h1>Title<a href="#title">¶</a></h1>'
  ).encode())

  markup = prepared.markup.decode()
  assert "https://example.com/reference" in markup
  assert "Attachment name" in markup
  assert "base64" not in markup
  assert "¶" not in markup


def test_html_input_limit_is_checked_before_parsing(monkeypatch):
  monkeypatch.setattr(conversion, "HTML_DOCUMENT_LIMIT", 8)

  with pytest.raises(LimitReached, match="input limit"):
    prepare_html(b"<p>too large</p>")


def test_embedded_raster_decoding_is_bounded_and_validated(monkeypatch):
  assert embedded_raster(data_uri(b"image")) == b"image"
  assert embedded_raster("data:image/png;base64,aW1h\nZ2U=") == b"image"

  for uri in (
    "data:image/svg+xml;base64,AAAA",
    "data:text/plain;base64,AAAA",
    "data:image/png,percent-encoded",
    "data:image/png;base64,invalid!",
  ):
    with pytest.raises(LimitReached):
      embedded_raster(uri)

  monkeypatch.setattr(conversion, "FILE_LIMIT", 2)
  with pytest.raises(LimitReached, match="size limit"):
    embedded_raster(data_uri(b"too large"))


def test_real_html_conversion_is_local_and_preserves_code_and_mermaid(
  factory, monkeypatch
):
  no_network(monkeypatch)
  raw = (
    "<html><head><title>Security lecture</title></head><body>"
    "<h2>Source material</h2><p>Read the table.</p>"
    "<table><tr><th>Topic</th><th>Value</th></tr>"
    "<tr><td>Integrity</td><td>Required</td></tr></table>"
    '<pre><code class="language-python">'
    "def check():\n    return True\n"
    "</code></pre>"
    '<div class="mermaid">graph TD\n A-->B</div>'
    '<img src="https://example.com/image.png" alt="Remote figure">'
    "<script>document.write('dynamic-only')</script>"
    "</body></html>"
  ).encode()
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert "Security lecture" in text
  assert "Integrity" in text
  assert "Required" in text
  assert "def check():\n    return True" in text
  assert "```python" in text
  assert "```mermaid" in text
  assert "graph TD\n A-->B" in text
  assert "Remote figure" in text
  assert "dynamic-only" not in text
  assert "KCTLHTML" not in text
  assert "https://example.com/image.png" not in text
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert product["content_sha256"] == digest(body(output, product))
  validate_span(product, len(complete_bytes(output, product)))


def test_html_declared_non_utf8_encoding_survives_conversion(factory):
  raw = (
    '<html><head><meta charset="euc-kr"><title>강의</title></head>'
    "<body><p>자료 내용</p></body></html>"
  ).encode("euc-kr")
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert "강의" in text
  assert "자료 내용" in text
  assert product["status"] == "complete"


def test_html_embedded_images_are_assets_not_base64_transcripts(factory):
  raw = (
    "<h1>Lesson</h1><p>Important source.</p>"
    f'<img alt="Result diagram" src="{data_uri(raster_bytes(nonblank=True))}">'
  ).encode()
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  raw_markdown = complete_bytes(output, product).decode()
  assert "Important source." in text
  assert "Result diagram" in text
  assert "base64" not in raw_markdown
  assert "## Extracted images" not in text
  assert "## Extracted images" in raw_markdown
  assert len(visuals["images"]) == 1
  image = visuals["images"][0]
  assert image["key"] == Path(image["path"]).stem
  assert image["path"] in product["outputs"]
  assert product["status"] == "complete"


def test_image_only_html_has_no_native_text_but_keeps_visuals(factory):
  raw = f'<img src="{data_uri(raster_bytes(nonblank=True))}">'.encode()
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  assert product["content_bytes"] == 0
  assert product["content_sha256"] == EMPTY_SHA256
  assert product["status"] == "complete"
  assert len(visuals["images"]) == 1
  assert "## Extracted images" in complete_bytes(output, product).decode()


def test_dynamic_only_html_is_a_nonretryable_static_extraction_limitation(factory):
  engines = Engines()
  processor, output = factory(
    ".html", b"<script>document.write('hidden')</script>", engines=engines
  )

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert product["markdown"] is None
  assert product["content_bytes"] == 0
  assert visuals["images"] == []
  assert any(
    "no extractable static text" in value for value in product["problems"]
  )


def test_unsupported_embedded_html_image_does_not_retry_forever(factory):
  raw = b'<p>Lesson</p><img alt="Vector" src="data:image/svg+xml;base64,AAAA">'
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert visuals["retry"] is False
  assert "Vector" in body(output, product).decode()
  assert any("unsupported embedded" in value for value in product["problems"])


def test_html_image_limit_is_visible_in_ocr_outcome(factory, monkeypatch):
  monkeypatch.setattr(conversion, "IMAGE_LIMIT", 1)
  uri = data_uri(raster_bytes(nonblank=True))
  raw = f'<img src="{uri}"><img src="{uri}">'.encode()
  processor, output = factory(".html", raw, step="ocr")

  product, visuals = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert any("reference limit" in value for value in product["problems"])
  assert len(visuals["images"]) == 1


def test_html_code_placeholder_loss_is_reported_not_silently_accepted(factory):
  processor, output = factory(
    ".html",
    b"<pre>important source code</pre>",
    engines=FakeEngines(markdown="Code was lost"),
  )

  product, _ = processor.run()

  assert product["status"] == "failed"
  assert product["retry"] is True
  assert product["markdown"] is None
  assert any("source block was lost" in value for value in product["problems"])


def test_html_tables_keep_heading_associations_and_nested_code(factory):
  raw = (
    "<h2>Assignments</h2>"
    "<table><tr><th>Task</th><th>Deadline</th></tr>"
    "<tr><td>Report</td><td>Friday</td></tr></table>"
    "<h2>Schedule</h2>"
    "<table><tr><th>Week</th><th>Topic</th></tr>"
    "<tr><td>One</td><td>Introduction</td></tr></table>"
    "<h2>Code example</h2><table><tr><td>"
    '<pre><code class="language-python">def example():\n  return 1\n'
    "</code></pre></td></tr></table>"
  ).encode()
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert product["status"] == "complete"
  assert text.index("Assignments") < text.index("Friday") < text.index("Schedule")
  assert text.index("Schedule") < text.index("Introduction")
  assert "def example():\n  return 1" in text
  assert "KCTLHTML" not in text
  assert text.count("Friday") == 1


def test_raster_conversion_gallery_is_not_native_text(factory):
  processor, output = factory(".png", raster_bytes(nonblank=True))

  product, visuals = processor.run()

  assert product["status"] == "complete"
  assert product["content_bytes"] == 0
  assert product["content_sha256"] == EMPTY_SHA256
  assert visuals["images"][0]["blank"] is False
  assert "## Extracted images" in complete_bytes(output, product).decode()


def test_blank_raster_ocr_does_not_start_tesseract_or_write_fake_text(factory):
  engines = FakeEngines()
  processor, output = factory(
    ".png", raster_bytes(), step="ocr", engines=engines
  )

  product, visuals = processor.run()

  assert product["status"] == "complete"
  assert product["markdown"] is None
  assert product["content_bytes"] == 0
  assert product["summary"]["blank"] == 1
  assert product["summary"]["text"] == 0
  assert engines.ocr_calls == []
  assert visuals["images"][0]["blank"] is True


def test_nonempty_ocr_body_has_no_generated_image_links(factory):
  engines = FakeEngines(recognized="OCR source text")
  processor, output = factory(
    ".png", raster_bytes(nonblank=True), step="ocr", engines=engines
  )

  product, _ = processor.run()

  assert engines.ocr_calls == [(12, 10)]
  assert b"OCR source text" in body(output, product)
  assert b"[Image]" not in body(output, product)
  assert b"images/" not in body(output, product)
  assert product["content_sha256"] == digest(body(output, product))
  assert product["summary"]["text"] == 1


def test_empty_ocr_result_is_successful_without_markdown(factory):
  processor, output = factory(
    ".png",
    raster_bytes(nonblank=True),
    step="ocr",
    engines=FakeEngines(recognized=""),
  )

  product, visuals = processor.run()

  assert product["status"] == "complete"
  assert product["markdown"] is None
  assert product["outputs"] == visuals["outputs"]
  assert product["summary"]["text"] == 0
  assert product["summary"]["blank"] == 0


def test_cached_visuals_are_reused_without_reextracting(factory, monkeypatch):
  raw = raster_bytes(nonblank=True)
  first, original_output = factory(".png", raw)
  _, visuals = first.run()
  protected = {
    path: original_output.resolve(path) for path in visuals["outputs"]
  }
  engines = FakeEngines(recognized="Cached image text")
  second, output = factory(
    ".png",
    raw,
    step="ocr",
    engines=engines,
    cached=visuals,
    existing=protected,
    protected=protected,
    protected_images=set(protected),
  )

  def unexpected():
    raise AssertionError("cached visual must not be regenerated")

  monkeypatch.setattr(second, "raster_visuals", unexpected)
  product, reused = second.run()

  assert reused["images"] == visuals["images"]
  assert reused["images"] is not visuals["images"]
  assert b"Cached image text" in body(output, product)
  assert not any(path.endswith(".png") for path in output.files)


def test_force_visuals_bypasses_a_complete_cache(factory, monkeypatch):
  raw = raster_bytes(nonblank=True)
  first, original_output = factory(".png", raw)
  _, visuals = first.run()
  existing = {
    path: original_output.resolve(path) for path in visuals["outputs"]
  }
  second, output = factory(
    ".png", raw, cached=visuals, existing=existing, force=True
  )
  calls = []
  original = second.raster_visuals

  def extract():
    calls.append(True)
    original()

  monkeypatch.setattr(second, "raster_visuals", extract)

  product, _ = second.run()

  assert calls == [True]
  assert product["status"] == "complete"


def test_duplicate_visuals_are_recognized_once(factory):
  raw = raster_bytes(nonblank=True)
  first, original_output = factory(".png", raw)
  _, visuals = first.run()
  image = visuals["images"][0]
  visuals["images"].append({**image, "label": "Second occurrence"})
  existing = {
    path: original_output.resolve(path) for path in visuals["outputs"]
  }
  engines = FakeEngines()
  processor, output = factory(
    ".png",
    raw,
    step="ocr",
    engines=engines,
    cached=visuals,
    existing=existing,
  )

  product, _ = processor.run()

  assert len(engines.ocr_calls) == 1
  assert product["summary"]["images"] == 2
  assert product["summary"]["duplicates"] == 1
  assert body(output, product).count(b"Recognized text") == 1


def test_image_write_rollback_removes_assets_when_gallery_exceeds_budget(
  factory, monkeypatch
):
  processor, output = factory(".png", raster_bytes(nonblank=True))

  def fail(*args, **kwargs):
    raise BudgetReached("gallery budget reached")

  monkeypatch.setattr(processor.markdown, "add", fail)

  product, visuals = processor.run()

  assert output.files == {}
  assert output.claimed == {}
  assert product["retry"] is True
  assert visuals["retry"] is True
  assert visuals["images"] == []


@pytest.mark.parametrize(
  "error",
  [StorageFailure("disk failed"), ProtectionFailure("unsafe output")],
)
def test_local_storage_failures_are_not_recorded_as_decoder_limitations(
  factory, monkeypatch, error
):
  processor, output = factory(".png", raster_bytes(nonblank=True))

  def fail(*args, **kwargs):
    raise error

  monkeypatch.setattr(output, "asset", fail)

  with pytest.raises(type(error), match=error.message):
    processor.run()


def test_pixel_limit_is_recorded_as_nonretryable_and_restored_after_run(
  factory, monkeypatch
):
  processor, output = factory(".png", raster_bytes(size=(12, 10)))
  previous = Image.MAX_IMAGE_PIXELS
  monkeypatch.setattr(conversion, "PIXEL_LIMIT", 10)

  product, visuals = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["retry"] is False
  assert Image.MAX_IMAGE_PIXELS == previous


def test_source_permissions_are_checked_before_decoding(factory):
  processor, _ = factory(".png", raster_bytes())
  processor.source.chmod(0o644)

  with pytest.raises(ProtectionFailure):
    processor.run()


def test_text_formats_are_not_sent_through_conversion(factory):
  with pytest.raises(Failure, match="inapplicable"):
    factory(".txt", b"passthrough source")


def test_office_conversion_and_ocr_share_package_image_discovery(factory):
  raw = office_bytes([
    ("word/media/image1.png", raster_bytes(nonblank=True)),
    ("word/media/image2.png", raster_bytes(nonblank=True)),
  ])
  engines = FakeEngines(markdown="Source ![Figure](image.png)\n")
  processor, output = factory(".docx", raw, engines=engines)

  product, visuals = processor.run()

  assert body(output, product) == b"Source Figure\n"
  assert len(visuals["images"]) == 2
  assert len(visuals["outputs"]) == 1
  assert "## Extracted images" not in body(output, product).decode()
  assert product["status"] == "complete"


def test_office_vectors_are_preserved_but_reported_as_non_ocr_content(factory):
  svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Vector</text></svg>'
  processor, output = factory(
    ".docx", office_bytes([("word/media/diagram.svg", svg)])
  )

  product, visuals = processor.run()

  path = "images/" + digest(svg) + ".svg"
  assert output.resolve(path).path.read_bytes() == svg
  assert path in product["outputs"]
  assert path not in visuals["outputs"]
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert any("not rasterized" in value for value in product["problems"])


def test_oversized_office_package_stops_before_markitdown(factory, monkeypatch):
  raw = office_bytes([("word/media/large.bin", b"x" * 100)])
  engines = FakeEngines()
  processor, output = factory(".docx", raw, engines=engines)
  monkeypatch.setattr(conversion, "ASSET_LIMIT", 20)

  product, visuals = processor.run()

  assert engines.markdown_calls == []
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals is None
  assert output.files == {}


@pytest.mark.parametrize("kind", [".docx", ".xlsx", ".pptx"])
def test_office_native_failure_does_not_prevent_healthy_media_capture(
  factory, kind
):
  raw = office_bytes([("word/media/image.png", raster_bytes(nonblank=True))])
  engines = FakeEngines()
  engines.error = ValueError("native converter failed")
  processor, output = factory(kind, raw, engines=engines)

  product, visuals = processor.run()

  assert product["retry"] is True
  assert product["status"] == "partial"
  assert product["content_bytes"] == 0
  assert len(visuals["images"]) == 1
  assert visuals["status"] == "complete"


def test_office_storage_failure_does_not_start_recovery_or_media(
  factory, monkeypatch
):
  engines = FakeEngines()
  engines.error = StorageFailure("input device failed")
  processor, _ = factory(".pptx", office_bytes(), engines=engines)

  def unexpected(*args):
    pytest.fail("storage failure must stop processing")

  monkeypatch.setattr(processor, "recover_pptx", unexpected)
  monkeypatch.setattr(processor, "extract_visuals", unexpected)

  with pytest.raises(StorageFailure):
    processor.run()


def test_processing_cancellation_is_not_a_decoder_problem(factory):
  engines = FakeEngines()
  engines.error = Cancelled()
  processor, _ = factory(".docx", office_bytes(), engines=engines)

  with pytest.raises(Cancelled):
    processor.run()


def test_real_powerpoint_conversion_has_no_data_uri_or_gallery_in_source(
  factory, monkeypatch
):
  from pptx import Presentation
  from pptx.util import Inches

  presentation = Presentation()
  slide = presentation.slides.add_slide(presentation.slide_layouts[6])
  box = slide.shapes.add_textbox(
    Inches(1), Inches(1), Inches(5), Inches(1)
  )
  box.text_frame.text = "Source lesson text"
  slide.shapes.add_picture(
    io.BytesIO(raster_bytes(nonblank=True)), Inches(1), Inches(2)
  )
  stream = io.BytesIO()
  presentation.save(stream)
  no_network(monkeypatch)
  engines = Engines()
  processor, output = factory(".pptx", stream.getvalue(), engines=engines)

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert "Source lesson text" in text
  assert "base64" not in text
  assert "## Extracted images" not in text
  assert "Image extracted below" not in text
  assert len(visuals["images"]) == 1


def test_real_pptx_soft_breaks_preserve_text_and_table_structure(factory):
  from pptx import Presentation
  from pptx.util import Inches

  presentation = Presentation()
  slide = presentation.slides.add_slide(presentation.slide_layouts[5])
  slide.shapes.title.text = "Décision\v한국어 → ∑"
  box = slide.shapes.add_textbox(
    Inches(1), Inches(2), Inches(5), Inches(1)
  )
  box.text = "Ordinary\vsecond line\n    indented UML A --> B"
  table = slide.shapes.add_table(
    2, 2, Inches(1), Inches(3), Inches(5), Inches(2)
  ).table
  table.cell(0, 0).text = "En-tête\v다음"
  table.cell(0, 1).text = "Unchanged"
  table.cell(1, 0).text = "α ≤ β\vA → B"
  table.cell(1, 1).text = "Café"
  slide.notes_slide.notes_text_frame.text = "Notes\v    preserved"
  stream = io.BytesIO()
  presentation.save(stream)
  engines = Engines()
  processor, output = factory(".pptx", stream.getvalue(), engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert "\v" not in text
  assert "# Décision\n한국어 → ∑" in text
  assert "Ordinary\nsecond line\n    indented UML A --> B" in text
  assert "Notes\n    preserved" in text
  rows = [line for line in text.split("\n") if line.startswith("|")]
  assert len(rows) == 3
  assert all(line.count("|") == 3 for line in rows)
  assert "En-tête<br>다음" in rows[0]
  assert "α ≤ β<br>A → B" in rows[2]
  assert "Café" in rows[2]
  assert text.index("Ordinary") < text.index("En-tête") < text.index("Notes")


def test_pptx_break_normalization_preserves_fenced_code():
  text = "```uml\n    | A\v      B → C\n```\n| X\vY | Z |"
  assert conversion._pptx_breaks(text) == (
    "```uml\n    | A\n      B → C\n```\n| X<br>Y | Z |"
  )


def pptx_bytes():
  from pptx import Presentation
  from pptx.util import Inches

  presentation = Presentation()

  for title in ("First source", "Second source", "Third source"):
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    box = group.shapes.add_textbox(
      Inches(1), Inches(1), Inches(4), Inches(1)
    )
    box.text_frame.text = title + "\vExplicit second line"

  slide = presentation.slides[1]
  table = slide.shapes.add_table(
    2, 2, Inches(1), Inches(2), Inches(5), Inches(2)
  ).table
  table.cell(0, 0).text = "Deadline"
  table.cell(0, 1).text = "Friday"
  table.cell(1, 0).text = "Limit"
  table.cell(1, 1).text = "10 units"
  slide.shapes.add_picture(
    io.BytesIO(raster_bytes(nonblank=True)), Inches(1), Inches(4)
  )
  presentation.slides[2].shapes.add_picture(
    io.BytesIO(raster_bytes("blue", nonblank=True)), Inches(1), Inches(4)
  )
  stream = io.BytesIO()
  presentation.save(stream)
  return stream.getvalue()


def rewrite_package(raw, changes=None, corrupt=()):
  changes = changes or {}
  result = io.BytesIO()

  with zipfile.ZipFile(io.BytesIO(raw)) as source:
    with zipfile.ZipFile(result, "w", zipfile.ZIP_STORED) as target:
      for member in source.infolist():
        value = changes.get(member.filename, source.read(member))
        if value is not None:
          target.writestr(member.filename, value)

  output = bytearray(result.getvalue())
  with zipfile.ZipFile(result) as package:
    for name in corrupt:
      member = package.getinfo(name)
      start = member.header_offset
      name_bytes = int.from_bytes(output[start + 26:start + 28], "little")
      extra_bytes = int.from_bytes(output[start + 28:start + 30], "little")
      offset = start + 30 + name_bytes + extra_bytes
      output[offset] ^= 1

  return bytes(output)


def test_real_pptx_crc_failure_recovers_text_tables_and_healthy_media(
  factory, monkeypatch
):
  raw = rewrite_package(
    pptx_bytes(), corrupt=["ppt/media/image1.png"]
  )
  engines = Engines()
  no_network(monkeypatch)
  processor, output = factory(".pptx", raw, engines=engines)

  try:
    product, visuals = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert processor.source.read_bytes() == raw
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert product["summary"]["recovered_slides"] == 3
  assert text.index("First source") < text.index("Second source")
  assert text.index("Second source") < text.index("Third source")
  assert "First source\nExplicit second line" in text
  assert "Deadline | Friday" in text
  assert "Limit | 10 units" in text
  assert len(visuals["images"]) == 1
  assert "image2.png" in visuals["images"][0]["label"]
  assert any("image1.png" in message for message in product["problems"])
  assert product["summary"]["coverage"]
  validate_span(product, len(complete_bytes(output, product)))


def test_pptx_recovery_uses_relationship_order_not_member_names(factory):
  raw = pptx_bytes()
  with zipfile.ZipFile(io.BytesIO(raw)) as package:
    root = ElementTree.fromstring(package.read("ppt/presentation.xml"))
  listing = next(node for node in root if node.tag.endswith("}sldIdLst"))
  listing[:] = reversed(list(listing))
  raw = rewrite_package(
    raw,
    {"ppt/presentation.xml": ElementTree.tostring(root)},
    ["ppt/media/image1.png"],
  )
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("native package read failed")
  processor, output = factory(".pptx", raw, engines=engines)

  product, _ = processor.run()

  text = body(output, product).decode()
  assert text.index("Third source") < text.index("Second source")
  assert text.index("Second source") < text.index("First source")
  assert product["retry"] is False


def test_bad_slide_does_not_discard_readable_following_slides(factory):
  raw = rewrite_package(
    pptx_bytes(),
    corrupt=["ppt/slides/slide2.xml"],
  )
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("native package read failed")
  processor, output = factory(".pptx", raw, engines=engines)

  product, _ = processor.run()

  text = body(output, product).decode()
  assert "First source" in text
  assert "Third source" in text
  assert "Second source" not in text
  assert "## Slide 3" in text
  assert product["retry"] is False
  assert any("Slide 2" in message for message in product["problems"])


@pytest.mark.parametrize(
  "missing",
  ["_rels/.rels", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"],
)
def test_missing_ordering_structure_does_not_fall_back_to_filename_order(
  factory, missing
):
  raw = rewrite_package(
    pptx_bytes(), {missing: None}, ["ppt/media/image1.png"]
  )
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("native package read failed")
  processor, output = factory(".pptx", raw, engines=engines)

  product, visuals = processor.run()

  assert body(output, product) == b""
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert len(visuals["images"]) == 1
  assert any("PPTX recovery" in message for message in product["problems"])


def test_clean_package_failure_does_not_start_xml_recovery(factory, monkeypatch):
  engines = FakeEngines()
  engines.error = ValueError("unrelated converter failure")
  processor, _ = factory(".pptx", pptx_bytes(), engines=engines)

  def unexpected(*args):
    pytest.fail("clean package must not trigger fallback extraction")

  monkeypatch.setattr(conversion, "_pptx_order", unexpected)
  product, _ = processor.run()

  assert product["retry"] is True
  assert "recovered_slides" not in product["summary"]


@pytest.mark.parametrize(
  "raw",
  [
    b'<!DOCTYPE x [<!ENTITY value "expanded">]><x>&value;</x>',
    b'<!DOCTYPE x SYSTEM "https://example.com/context"><x/>',
    b"<broken",
  ],
)
def test_recovery_xml_rejects_entities_dtds_and_malformed_input(raw, monkeypatch):
  no_network(monkeypatch)
  packed = office_bytes([("part.xml", raw)])

  with zipfile.ZipFile(io.BytesIO(packed)) as archive:
    with pytest.raises(_PackageError, match="malformed or unsafe"):
      _Package(archive).xml("part.xml")


@pytest.mark.parametrize(
  "target",
  [
    "../../../outside.xml",
    "%2e%2e/%2e%2e/outside.xml",
    "https://example.com/slide.xml",
    "//example.com/slide.xml",
    "file:///tmp/slide.xml",
    "slide.xml?query=1",
    "slide.xml#fragment",
    r"..\slide.xml",
    "slide%00.xml",
    "",
  ],
)
def test_essential_relationship_targets_are_package_local(target):
  with pytest.raises(_PackageError):
    _package_target("ppt/presentation.xml", target)


def test_internal_relationship_resolution_supports_relative_and_absolute_parts():
  assert _package_target(
    "ppt/slides/slide1.xml", "../media/image.png"
  ) == "ppt/media/image.png"
  assert _package_target(
    "ppt/presentation.xml", "/ppt/slides/slide%201.xml"
  ) == "ppt/slides/slide 1.xml"


def test_external_slide_relationship_is_not_followed():
  raw = pptx_bytes()
  path = "ppt/_rels/presentation.xml.rels"

  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    root = ElementTree.fromstring(archive.read(path))

  slide = next(node for node in root if node.get("Type", "").endswith("/slide"))
  slide.set("TargetMode", "External")
  slide.set("Target", "https://example.com/slide.xml")
  raw = rewrite_package(raw, {path: ElementTree.tostring(root)})

  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    with pytest.raises(_PackageError, match="external"):
      _pptx_order(_Package(archive))


def test_ambiguous_slide_relationship_is_rejected():
  raw = pptx_bytes()
  path = "ppt/_rels/presentation.xml.rels"

  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    root = ElementTree.fromstring(archive.read(path))

  root.append(ElementTree.fromstring(ElementTree.tostring(root[0])))
  raw = rewrite_package(raw, {path: ElementTree.tostring(root)})

  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    with pytest.raises(_PackageError, match="ambiguous"):
      _pptx_order(_Package(archive))


def test_duplicate_package_members_are_not_selected_arbitrarily(factory):
  with pytest.warns(UserWarning, match="Duplicate name"):
    raw = office_bytes([
      ("word/media/image.png", b"first"),
      ("word/media/image.png", b"second"),
    ])
  engines = FakeEngines()
  processor, _ = factory(".docx", raw, engines=engines)

  product, visuals = processor.run()

  assert product["retry"] is False
  assert product["problems"]
  assert engines.markdown_calls == []
  assert visuals is None


def test_recovery_xml_limit_is_checked_before_reading(monkeypatch):
  raw = office_bytes([("part.xml", b"<root>large</root>")])

  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    package = _Package(archive)
    monkeypatch.setattr(conversion, "TEXT_LIMIT", 4)

    def unexpected(*args):
      pytest.fail("oversized XML must not be opened")

    monkeypatch.setattr(archive, "open", unexpected)
    with pytest.raises(LimitReached, match="read limit"):
      package.xml("part.xml")


def test_recovery_total_read_budget_counts_repeated_reads(monkeypatch):
  raw = office_bytes([("part.xml", b"<root/>")])

  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    package = _Package(archive)
    monkeypatch.setattr(conversion, "ASSET_LIMIT", 10)
    assert package.read("part.xml") == b"<root/>"
    with pytest.raises(LimitReached, match="total read limit"):
      package.read("part.xml")


def test_recovery_slide_limit_is_checked_before_slide_reads(monkeypatch):
  with zipfile.ZipFile(io.BytesIO(pptx_bytes())) as archive:
    monkeypatch.setattr(conversion, "PAGE_LIMIT", 2)
    with pytest.raises(LimitReached, match="slide recovery limit"):
      _pptx_order(_Package(archive))


def wrapped_conversion_failure(errors):
  from markitdown import FileConversionException

  class ConversionFailure(FileConversionException):
    def __init__(self):
      Exception.__init__(self, "synthetic conversion failure")
      self.attempts = [
        SimpleNamespace(exc_info=(type(error), error, None))
        for error in errors
      ]

  return ConversionFailure()


def test_integrity_classifier_uses_explicit_causes_and_conversion_attempts():
  cause = zipfile.BadZipFile("member integrity failure")
  wrapped = ValueError("explicitly wrapped failure")
  wrapped.__cause__ = cause

  assert _integrity_failure(cause)
  assert _integrity_failure(wrapped)
  assert _integrity_failure(wrapped_conversion_failure([
    zipfile.BadZipFile("first converter"),
    zipfile.BadZipFile("second converter"),
  ]))
  assert _integrity_failure(wrapped_conversion_failure([wrapped]))


def test_integrity_classifier_does_not_use_messages_or_implicit_context():
  implicit = ValueError("unrelated processing failure")
  implicit.__context__ = zipfile.BadZipFile("earlier operation failed")

  assert not _integrity_failure(implicit)
  assert not _integrity_failure(ValueError("Bad CRC-32 in image.png"))
  assert not _integrity_failure(wrapped_conversion_failure([]))
  assert not _integrity_failure(wrapped_conversion_failure([
    zipfile.BadZipFile("bad member"),
    ValueError("independent converter failure"),
  ]))


def test_integrity_classifier_stops_on_cyclic_causes():
  error = ValueError("cycle")
  error.__cause__ = error

  assert not _integrity_failure(error)


def corrupt_office_media(kind):
  if kind == ".pptx":
    return rewrite_package(
      pptx_bytes(), corrupt=["ppt/media/image1.png"]
    )

  raw = office_bytes([
    ("word/media/bad.png", raster_bytes(nonblank=True)),
    ("word/media/good.png", raster_bytes("blue", nonblank=True)),
  ])
  return rewrite_package(raw, corrupt=["word/media/bad.png"])


@pytest.mark.parametrize("kind", [".docx", ".xlsx", ".pptx"])
def test_verified_native_integrity_failure_is_nonretryable_across_office_formats(
  factory, kind
):
  raw = corrupt_office_media(kind)
  engines = FakeEngines()
  engines.error = wrapped_conversion_failure([
    zipfile.BadZipFile("native package read failed"),
  ])
  processor, output = factory(kind, raw, engines=engines)

  product, visuals = processor.run()

  assert processor.source.read_bytes() == raw
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["retry"] is False
  assert len(visuals["images"]) == 1
  assert any("Bad CRC-32" in problem for problem in product["problems"])

  if kind == ".pptx":
    assert "First source" in body(output, product).decode()
    assert product["summary"]["recovered_slides"] == 3
  else:
    assert body(output, product) == b""
    assert "recovered_slides" not in product["summary"]
    assert any(
      "Native Office text unavailable" in problem
      for problem in product["problems"]
    )


@pytest.mark.parametrize("kind", [".docx", ".xlsx", ".pptx"])
def test_unrelated_native_failure_stays_retryable_alongside_corrupt_media(
  factory, kind
):
  engines = FakeEngines()
  engines.error = ValueError("independent conversion failure")
  processor, output = factory(
    kind, corrupt_office_media(kind), engines=engines
  )

  product, visuals = processor.run()

  assert product["retry"] is True
  assert visuals["retry"] is False
  assert len(visuals["images"]) == 1
  assert "unexpected ValueError" in product["problems"]
  assert any("Bad CRC-32" in problem for problem in product["problems"])

  if kind == ".pptx":
    assert "First source" in body(output, product).decode()
  else:
    assert body(output, product) == b""


def test_mixed_conversion_attempts_do_not_become_a_permanent_input_failure(
  factory
):
  engines = FakeEngines()
  engines.error = wrapped_conversion_failure([
    zipfile.BadZipFile("bad member"),
    RuntimeError("unrelated converter problem"),
  ])
  processor, _ = factory(
    ".docx", corrupt_office_media(".docx"), engines=engines
  )

  product, visuals = processor.run()

  assert product["retry"] is True
  assert visuals["retry"] is False
  assert any("ConversionFailure" in message for message in product["problems"])
  assert any("Bad CRC-32" in message for message in product["problems"])


def test_native_integrity_exception_without_verified_defect_remains_retryable(
  factory
):
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("converter reported an integrity failure")
  processor, _ = factory(
    ".docx",
    office_bytes([("word/media/good.png", raster_bytes(nonblank=True))]),
    engines=engines,
  )

  product, visuals = processor.run()

  assert product["retry"] is True
  assert visuals["status"] == "complete"
  assert len(visuals["images"]) == 1
  assert "unexpected BadZipFile" in product["problems"]
  assert not any(
    "verified ZIP integrity failure" in message
    for message in product["problems"]
  )


@pytest.mark.parametrize(
  "error",
  [
    StorageFailure("device failed during verification"),
    ProtectionFailure("unsafe verification input"),
    Cancelled(),
  ],
)
def test_integrity_inspection_does_not_soften_storage_or_cancellation(
  factory, monkeypatch, error
):
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("native package read failed")
  processor, output = factory(
    ".docx", corrupt_office_media(".docx"), engines=engines
  )

  def fail(self):
    raise error

  monkeypatch.setattr(_Package, "verify", fail)

  with pytest.raises(type(error)) as captured:
    processor.run()

  assert captured.value is error
  assert output.files == {}


def test_incomplete_verification_does_not_claim_confirmed_integrity_cause(
  factory, monkeypatch
):
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("native package read failed")
  processor, _ = factory(
    ".docx", corrupt_office_media(".docx"), engines=engines
  )

  def fail(self):
    raise LimitReached("inspection budget reached")

  monkeypatch.setattr(_Package, "verify", fail)

  product, visuals = processor.run()

  assert product["retry"] is True
  assert visuals["retry"] is False
  assert any("inspection budget" in message for message in product["problems"])
  assert not any(
    "Native Office text unavailable after a verified" in message
    for message in product["problems"]
  )


def test_native_output_limit_does_not_trigger_package_integrity_inspection(
  factory, monkeypatch
):
  engines = FakeEngines()
  engines.error = LimitReached("fixed conversion limit")
  processor, _ = factory(
    ".pptx", corrupt_office_media(".pptx"), engines=engines
  )

  def unexpected(self):
    pytest.fail("output limit is not evidence of package corruption")

  monkeypatch.setattr(_Package, "verify", unexpected)

  product, visuals = processor.run()

  assert product["retry"] is False
  assert "fixed conversion limit" in product["problems"]
  assert len(visuals["images"]) == 1
  assert "recovered_slides" not in product["summary"]


def test_confirmed_integrity_failure_does_not_clear_an_existing_retry_flag(factory):
  engines = FakeEngines()
  engines.error = zipfile.BadZipFile("native package read failed")
  processor, _ = factory(
    ".docx", corrupt_office_media(".docx"), engines=engines
  )
  processor.issue(Failure("separate earlier processing failure"))

  product, visuals = processor.run()

  assert product["retry"] is True
  assert "separate earlier processing failure" in product["problems"]
  assert visuals["retry"] is False


def test_real_docx_corrupt_image_is_confirmed_without_disabling_crc(
  factory, monkeypatch
):
  from docx import Document
  from docx.shared import Inches

  document = Document()
  document.add_paragraph("Readable paragraph before damaged media")
  document.add_picture(
    io.BytesIO(raster_bytes(nonblank=True)), width=Inches(1)
  )
  document.add_paragraph("Readable paragraph after damaged media")
  document.add_picture(
    io.BytesIO(raster_bytes("blue", nonblank=True)), width=Inches(1)
  )
  stream = io.BytesIO()
  document.save(stream)
  raw = rewrite_package(
    stream.getvalue(), corrupt=["word/media/image1.png"]
  )
  no_network(monkeypatch)
  engines = Engines()
  processor, output = factory(".docx", raw, engines=engines)

  try:
    with pytest.raises(Exception) as native:
      engines.markdown(processor.source, ".docx")
    assert _integrity_failure(native.value)

    product, visuals = processor.run()
  finally:
    engines.close()

  assert processor.source.read_bytes() == raw
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert body(output, product) == b""
  assert len(visuals["images"]) == 1
  assert "image2.png" in visuals["images"][0]["label"]
  assert visuals["retry"] is False
  assert any(
    "word/media/image1.png" in message and "Bad CRC-32" in message
    for message in product["problems"]
  )
  assert any(
    "Native Office text unavailable" in message
    for message in product["problems"]
  )

  with zipfile.ZipFile(io.BytesIO(processor.source.read_bytes())) as archive:
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
      archive.read("word/media/image1.png")


@pytest.fixture
def pdfium(monkeypatch):
  state = SimpleNamespace(
    pages=[],
    renders=[],
    object_requests=0,
    bitmaps_closed=0,
    pages_closed=0,
    textpages_closed=0,
    documents_closed=0,
    annotations_closed=0,
  )
  module = ModuleType("pypdfium2")

  class Bitmap:
    def __init__(self, color):
      self.color = color

    def to_pil(self):
      image = Image.new("RGB", (12, 10), self.color)
      image.putpixel((0, 0), (0, 0, 0))
      return image

    def close(self):
      state.bitmaps_closed += 1

  class TextPage:
    def __init__(self, value):
      self.value = value

    def get_text_range(self):
      if isinstance(self.value, BaseException):
        raise self.value
      return self.value

    def close(self):
      state.textpages_closed += 1

  class Object:
    def __init__(self, color="blue", dimensions=(12, 10)):
      self.color = color
      self.dimensions = dimensions

    def get_metadata(self):
      return SimpleNamespace(
        width=self.dimensions[0], height=self.dimensions[1]
      )

    def get_bitmap(self):
      return Bitmap(self.color)

  class Page:
    def __init__(self, text="", objects=(), size=(100, 100), annotations=()):
      self.text = text
      self.objects = objects
      self.size = size
      self.annotations = annotations

    def get_textpage(self):
      return TextPage(self.text)

    def get_size(self):
      return self.size

    def render(self, scale):
      state.renders.append(scale)
      return Bitmap("white")

    def get_objects(self, filter):
      state.object_requests += 1
      assert filter == [3]
      for obj in self.objects:
        if isinstance(obj, BaseException):
          raise obj
        yield obj

    def close(self):
      state.pages_closed += 1

  class Document:
    def __init__(self, path):
      self.path = path

    def __enter__(self):
      return self

    def __exit__(self, *args):
      state.documents_closed += 1

    def __len__(self):
      return len(state.pages)

    def __getitem__(self, number):
      page = state.pages[number]
      if isinstance(page, BaseException):
        raise page
      return page

  def close_annotation(annotation):
    state.annotations_closed += 1

  def annotation_type(annotation):
    if isinstance(annotation, BaseException):
      raise annotation
    return annotation

  module.PdfDocument = Document
  module.raw = SimpleNamespace(
    FPDF_PAGEOBJ_IMAGE=3,
    FPDFPage_CountObjects=lambda page: 0,
    FPDFPage_GetAnnotCount=lambda page: len(page.annotations),
    FPDFPage_GetAnnot=lambda page, index: page.annotations[index],
    FPDFAnnot_GetSubtype=annotation_type,
    FPDFPage_CloseAnnot=close_annotation,
  )
  monkeypatch.setitem(sys.modules, "pypdfium2", module)
  state.Page = Page
  state.Object = Object
  return state


def test_pdf_native_text_and_visuals_have_distinct_content_boundaries(
  factory, pdfium
):
  pdfium.pages = [
    pdfium.Page("Native lesson content " * 4, [pdfium.Object()]),
    pdfium.Page(""),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  text = body(output, product).decode()
  assert "## Page 1" in text
  assert "Native lesson content" in text
  assert "## Page 2" not in text
  assert "No native text" not in text
  assert "## Extracted images" not in text
  assert product["status"] == "complete"
  assert product["summary"]["pages"] == 2
  assert product["summary"]["native_text_pages"] == 1
  assert [image["page"] for image in visuals["images"]] == [1, 2]
  assert pdfium.renders == [2]
  assert pdfium.bitmaps_closed == 2
  assert pdfium.textpages_closed == 2
  assert pdfium.documents_closed == 2


def test_scanned_pdf_does_not_manufacture_native_text(factory, pdfium):
  pdfium.pages = [pdfium.Page("")]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert product["content_bytes"] == 0
  assert product["content_sha256"] == EMPTY_SHA256
  assert product["summary"]["native_text_pages"] == 0
  assert len(visuals["images"]) == 1
  assert b"No native text" not in complete_bytes(output, product)


def test_pdf_ocr_uses_page_headings_without_asset_link_noise(factory, pdfium):
  pdfium.pages = [pdfium.Page("")]
  processor, output = factory(
    ".pdf",
    b"%PDF-synthetic",
    step="ocr",
    engines=FakeEngines(recognized="Page contents"),
  )

  product, _ = processor.run()

  assert body(output, product) == b"## Page 1\n\nPage contents\n"
  assert pdfium.documents_closed == 1


def test_pdf_native_text_failure_falls_back_to_visual_capture(factory, pdfium):
  pdfium.pages = [pdfium.Page(ValueError("native extraction failed"))]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert product["retry"] is True
  assert product["status"] == "partial"
  assert product["content_bytes"] == 0
  assert len(visuals["images"]) == 1
  assert pdfium.textpages_closed == 1
  assert "Native PDF text unavailable on pages 1" in product["summary"]["coverage"]


def test_pdf_page_limit_prevents_partial_unbounded_rendering(
  factory, pdfium, monkeypatch
):
  pdfium.pages = [pdfium.Page(""), pdfium.Page("")]
  monkeypatch.setattr(conversion, "PAGE_LIMIT", 1)
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert pdfium.renders == []
  assert visuals is None


def test_pdf_oversized_image_does_not_hide_following_images(factory, pdfium):
  pdfium.pages = [
    pdfium.Page(
      "Native text " * 10,
      [
        pdfium.Object(dimensions=(100000, 100000)),
        pdfium.Object(color="green"),
      ],
    ),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert len(visuals["images"]) == 1
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert any("Page 1, image 1" in message for message in product["problems"])


def test_pdf_suspect_text_is_preserved_and_reported_once_per_document(
  factory, pdfium
):
  source = "safety\ufffecritical A\ufffdack " + "Native source " * 10
  pdfium.pages = [pdfium.Page(source), pdfium.Page(source)]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  expected = source.strip()
  assert body(output, product).decode() == (
    f"## Page 1\n\n{expected}\n\n## Page 2\n\n{expected}\n"
  )
  assert product["status"] == "partial"
  assert product["retry"] is False
  coverage = product["summary"]["coverage"]
  assert len(coverage) == 1
  assert "U+FFFE: 2" in coverage[0]
  assert "U+FFFD: 2" in coverage[0]
  assert "pages 1-2" in coverage[0]
  assert set(coverage) <= set(product["problems"])
  assert visuals["status"] == "complete"
  assert [image["label"] for image in visuals["images"]] == ["Page 1", "Page 2"]
  assert pdfium.renders == [2, 2]
  assert pdfium.object_requests == 0
  assert pdfium.textpages_closed == 2


@pytest.mark.parametrize(
  "code",
  [code for code in range(32) if code not in (9, 10, 13)]
  + [0xFFFD, 0xFDD0, 0xFDEF, 0xFFFE, 0xFFFF, 0x1FFFE, 0x10FFFF],
)
def test_suspect_detector_counts_controls_replacements_and_noncharacters(code):
  assert _suspect_codes(chr(code) * 2) == {code: 2}


def test_suspect_detector_leaves_valid_source_characters_alone():
  assert not _suspect_codes("Korean: 자료\nsoft\u00adhyphen nonbreaking\u2011hyphen")
  assert not _suspect_codes(chr(0xFDCF) + chr(0xFDF0) + chr(0x1FFFD))
  assert not _suspect_codes("\t\n\r ")


def test_pdf_control_diagnostics_do_not_rewrite_text_or_trigger_full_page_ocr(
  factory, pdfium
):
  source = "\x04" + "Equation source " * 8 + "\x0f"
  pdfium.pages = [pdfium.Page(source), pdfium.Page(source)]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert body(output, product).decode() == (
    f"## Page 1\n\n{source}\n\n## Page 2\n\n{source}\n"
  )
  coverage = product["summary"]["coverage"]
  assert len(coverage) == 1
  assert "U+0004: 2" in coverage[0]
  assert "U+000F: 2" in coverage[0]
  assert "pages 1-2" in coverage[0]
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["images"] == []
  assert pdfium.renders == []


def test_pdf_edge_controls_are_reported_before_existing_whitespace_trimming(
  factory, pdfium
):
  source = "\x1c" + "Native text " * 10 + "\x1f"
  pdfium.pages = [pdfium.Page(source)]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, _ = processor.run()

  assert body(output, product).decode() == f"## Page 1\n\n{source.strip()}\n"
  coverage = product["summary"]["coverage"]
  assert "U+001C: 1" in coverage[0]
  assert "U+001F: 1" in coverage[0]
  assert pdfium.renders == []


def test_page_ranges_are_compact_and_bounded():
  assert _page_ranges({1, 2, 3, 5, 7, 8}) == "1-3, 5, 7-8"
  pages = set(range(1, 500, 2))
  result = _page_ranges(pages)
  assert len(result) < 300
  assert "250 pages total" in result


def test_pdf_annotation_notes_do_not_infer_answers(factory, pdfium):
  text = "Quiz body " * 10
  pdfium.pages = [
    pdfium.Page(text, annotations=[2, 9, 15]),
    pdfium.Page(text, annotations=[3]),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, _ = processor.run()

  coverage = product["summary"]["coverage"]
  assert len(coverage) == 1
  assert "pages 1-2" in coverage[0]
  assert "FreeText, Highlight, Ink" in coverage[0]
  assert "selected" not in coverage[0]
  assert "correct" not in coverage[0]
  assert product["retry"] is False
  assert pdfium.annotations_closed == 4
  expected = text.strip()
  assert body(output, product).decode() == (
    f"## Page 1\n\n{expected}\n\n## Page 2\n\n{expected}\n"
  )


def test_pdf_links_do_not_create_annotation_coverage_warnings(factory, pdfium):
  pdfium.pages = [pdfium.Page("Native text " * 10, annotations=[2])]
  processor, _ = factory(".pdf", b"%PDF-synthetic")

  product, _ = processor.run()

  assert product["status"] == "complete"
  assert "coverage" not in product["summary"]
  assert pdfium.annotations_closed == 1


def test_annotation_inspection_failure_closes_handle_and_preserves_text(
  factory, pdfium
):
  pdfium.pages = [
    pdfium.Page("Native text " * 10, annotations=[ValueError("inspection failed")])
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, _ = processor.run()

  assert "Native text" in body(output, product).decode()
  assert pdfium.annotations_closed == 1
  assert product["retry"] is True
  assert "PDF annotation inspection incomplete on pages 1" in (
    product["summary"]["coverage"]
  )


def test_annotation_limit_is_nonretryable_without_claiming_full_inspection(
  factory, pdfium, monkeypatch
):
  monkeypatch.setattr(conversion, "ANNOTATION_LIMIT", 1)
  pdfium.pages = [
    pdfium.Page("Native text " * 10, annotations=[9, 15]),
    pdfium.Page("Other text " * 10, annotations=[3]),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, _ = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert pdfium.annotations_closed == 1
  assert "Other text" in body(output, product).decode()
  assert "PDF annotation inspection incomplete on pages 1" in (
    product["summary"]["coverage"]
  )
  assert any("FreeText" in note for note in product["summary"]["coverage"])


def test_pdf_page_failure_does_not_discard_following_pages(factory, pdfium):
  pdfium.pages = [
    pdfium.Page("First page " * 10),
    ValueError("page unavailable"),
    pdfium.Page("Third page " * 10),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, _ = processor.run()

  text = body(output, product).decode()
  assert "## Page 1" in text
  assert "## Page 3" in text
  assert "Third page" in text
  assert product["retry"] is True
  assert "Native PDF text unavailable on pages 2" in (
    product["summary"]["coverage"]
  )


def test_image_enumerator_failure_keeps_prior_images_and_continues_pages(
  factory, pdfium
):
  pdfium.pages = [
    pdfium.Page(
      "First page " * 10,
      [pdfium.Object(), ValueError("object enumeration failed"), pdfium.Object()],
    ),
    pdfium.Page("Second page " * 10, [pdfium.Object(color="green")]),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert [image["page"] for image in visuals["images"]] == [1, 2]
  assert "Second page" in body(output, product).decode()
  assert any(
    "Page 1: visual discovery incomplete" in message
    for message in product["problems"]
  )
  assert product["retry"] is True


@pytest.mark.parametrize("step", ["convert", "ocr"])
@pytest.mark.parametrize(
  ("text", "render"),
  [
    ("", True),
    ("x" * 39, True),
    ("\n" + "x" * 39 + "\n", True),
    ("x" * 40, False),
    ("Native source " * 10 + "\ufffd", True),
    ("Native source " * 10 + "\ufffe", False),
    ("Native source " * 10 + "\x04", False),
    ("Native source " * 10 + "\x0f", False),
  ],
)
def test_pdf_conversion_and_standalone_ocr_share_render_decisions(
  factory, pdfium, step, text, render
):
  pdfium.pages = [pdfium.Page(text, [pdfium.Object()])]
  engines = FakeEngines(recognized="Supplemental transcription")
  processor, output = factory(
    ".pdf", b"%PDF-synthetic", step=step, engines=engines
  )

  product, visuals = processor.run()

  assert pdfium.renders == ([2] if render else [])
  assert pdfium.object_requests == int(not render)
  assert pdfium.textpages_closed == 1
  assert len(visuals["images"]) == 1
  assert visuals["images"][0]["label"] == (
    "Page 1" if render else "Page 1, image 1"
  )
  assert visuals["status"] == "complete"
  if step == "convert":
    expected = f"## Page 1\n\n{text.strip()}\n" if text.strip() else ""
    assert body(output, product).decode() == expected
    assert engines.ocr_calls == []
  else:
    assert engines.ocr_calls == [(12, 10)]
    assert b"Supplemental transcription" in body(output, product)
    assert "Native source" not in body(output, product).decode()


@pytest.mark.parametrize("step", ["convert", "ocr"])
def test_pdf_supplemental_rendering_respects_pixel_limits(factory, pdfium, step):
  source = "Native source " * 10 + "\ufffd"
  pdfium.pages = [pdfium.Page(source, size=(100000, 100000))]
  processor, output = factory(".pdf", b"%PDF-synthetic", step=step)

  product, visuals = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert visuals["retry"] is False
  assert visuals["images"] == []
  assert pdfium.renders == []
  assert any(
    "rendered PDF page exceeds pixel limit" in p for p in product["problems"]
  )
  if step == "convert":
    assert source in body(output, product).decode()
  else:
    assert product["markdown"] is None


@pytest.mark.parametrize("step", ["convert", "ocr"])
def test_pdf_supplemental_rendering_respects_image_limits(
  factory, pdfium, monkeypatch, step
):
  monkeypatch.setattr(conversion, "IMAGE_LIMIT", 1)
  pdfium.pages = [
    pdfium.Page("First native source " * 10 + "\ufffd"),
    pdfium.Page("Second native source " * 10 + "\ufffd"),
  ]
  processor, output = factory(".pdf", b"%PDF-synthetic", step=step)

  product, visuals = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert visuals["status"] == "partial"
  assert len(visuals["images"]) == 1
  assert pdfium.renders == [2]
  assert pdfium.object_requests == 0
  if step == "convert":
    assert b"First native source" in body(output, product)
    assert b"Second native source" in body(output, product)


@pytest.mark.parametrize("step", ["convert", "ocr"])
@pytest.mark.parametrize(
  "error",
  [StorageFailure("render failed"), ProtectionFailure("unsafe"), Cancelled()],
)
def test_pdf_supplemental_rendering_propagates_fatal_errors(
  factory, pdfium, monkeypatch, step, error
):
  page = pdfium.Page("Native source " * 10 + "\ufffd")
  pdfium.pages = [page]

  def fail(scale):
    raise error

  monkeypatch.setattr(page, "render", fail)
  processor, _ = factory(".pdf", b"%PDF-synthetic", step=step)

  with pytest.raises(type(error)) as captured:
    processor.run()

  assert captured.value is error


def test_pdf_supplemental_render_failure_preserves_native_text(
  factory, pdfium, monkeypatch
):
  source = "Native source " * 10 + "\ufffd"
  page = pdfium.Page(source)
  pdfium.pages = [page]

  def fail(scale):
    raise ValueError("renderer failed")

  monkeypatch.setattr(page, "render", fail)
  processor, output = factory(".pdf", b"%PDF-synthetic")

  product, visuals = processor.run()

  assert source in body(output, product).decode()
  assert product["retry"] is True
  assert visuals["retry"] is True
  assert visuals["images"] == []
  assert any("visual discovery incomplete" in p for p in product["problems"])


def test_old_complete_visual_cache_does_not_suppress_supplemental_rendering(
  factory, pdfium
):
  pdfium.pages = [pdfium.Page("Native source " * 10 + "\ufffd")]
  cached = {
    "settings": copy.deepcopy(processing_settings()["visuals"]),
    "status": "complete",
    "images": [],
    "outputs": [],
  }
  cached["settings"]["pdf_visuals"] -= 1
  processor, output = factory(
    ".pdf", b"%PDF-synthetic", step="ocr", cached=cached
  )

  product, visuals = processor.run()

  assert pdfium.renders == [2]
  assert len(visuals["images"]) == 1
  assert product["summary"]["text"] == 1
  assert b"## Page 1" in body(output, product)
  assert cached["images"] == []


def test_supplemental_page_visual_is_reused_for_ocr(factory, pdfium, monkeypatch):
  pdfium.pages = [pdfium.Page("Native source " * 10 + "\ufffd")]
  first, original_output = factory(".pdf", b"%PDF-synthetic")
  _, visuals = first.run()
  existing = {
    path: original_output.resolve(path) for path in visuals["outputs"]
  }
  second, output = factory(
    ".pdf",
    b"%PDF-synthetic",
    step="ocr",
    cached=visuals,
    existing=existing,
    protected=existing,
    protected_images=set(existing),
  )

  def unexpected():
    pytest.fail("current page raster must be reused")

  monkeypatch.setattr(second, "pdf_visuals", unexpected)

  product, reused = second.run()

  assert pdfium.renders == [2]
  assert reused["images"] == visuals["images"]
  assert product["summary"]["text"] == 1
  assert body(output, product) == b"## Page 1\n\nRecognized text\n"
  assert not any(path.endswith(".png") for path in output.files)


def test_real_pdf_annotation_api_and_stream_conversion(factory):
  import pypdfium2 as pdfium

  stream = io.BytesIO()
  with pdfium.PdfDocument.new() as document:
    with closing(document.new_page(100, 100)) as page:
      annotation = pdfium.raw.FPDFPage_CreateAnnot(page, 9)
      assert annotation
      try:
        rectangle = pdfium.raw.FS_RECTF(10, 30, 50, 10)
        assert pdfium.raw.FPDFAnnot_SetRect(annotation, rectangle)
      finally:
        pdfium.raw.FPDFPage_CloseAnnot(annotation)

      assert _annotations(page) == {"Highlight"}

    document.save(stream)

  processor, output = factory(".pdf", stream.getvalue())
  product, visuals = processor.run()

  assert product["status"] == "partial"
  assert product["retry"] is False
  assert any("Highlight" in note for note in product["summary"]["coverage"])
  assert len(visuals["images"]) == 1
  image = output.resolve(visuals["images"][0]["path"]).path
  with Image.open(image) as rendered:
    assert rendered.size == (200, 200)


def test_engine_initializes_ocr_once_and_closes_it_once(
  private_dir, monkeypatch
):
  state = SimpleNamespace(initializations=[], images=0, ended=0)
  model = private_dir / "model"
  model.mkdir(mode=0o700)
  traineddata = model / "eng.traineddata"
  write_new(traineddata, b"synthetic model")
  distribution = SimpleNamespace(
    files=[Path("model/eng.traineddata")],
    locate_file=lambda path: private_dir / path,
  )

  class API:
    def __init__(self, **kwargs):
      state.initializations.append(kwargs)

    def SetImage(self, image):
      state.images += 1

    def GetUTF8Text(self):
      return "  recognized source  \n"

    def End(self):
      state.ended += 1

  module = ModuleType("tesserocr")
  module.PyTessBaseAPI = API
  module.OEM = SimpleNamespace(LSTM_ONLY=1)
  module.PSM = SimpleNamespace(AUTO=3)
  monkeypatch.setitem(sys.modules, "tesserocr", module)
  monkeypatch.setattr(
    conversion.importlib.metadata, "distribution", lambda name: distribution
  )
  engines = Engines()

  with Image.new("RGB", (10, 10)) as image:
    assert engines.recognize(image) == "recognized source"
    assert engines.recognize(image) == "recognized source"

  engines.close()
  engines.close()

  assert state.initializations == [{
    "path": str(model),
    "lang": "eng",
    "oem": 1,
    "psm": 3,
  }]
  assert state.images == 2
  assert state.ended == 1
  assert engines.ocr is None


def test_engine_does_not_guess_between_multiple_ocr_models(
  private_dir, monkeypatch
):
  distribution = SimpleNamespace(
    files=[
      Path("one/eng.traineddata"),
      Path("two/eng.traineddata"),
    ],
    locate_file=lambda path: private_dir / path,
  )
  module = ModuleType("tesserocr")
  monkeypatch.setitem(sys.modules, "tesserocr", module)
  monkeypatch.setattr(
    conversion.importlib.metadata, "distribution", lambda name: distribution
  )
  engines = Engines()

  with Image.new("RGB", (10, 10)) as image:
    with pytest.raises(Failure, match="model not found"):
      engines.recognize(image)

  assert engines.ocr is None


@pytest.mark.parametrize(
  "wrapper",
  [
    '<pre class="mermaid">{}</pre>',
    '<div class="mermaid">{}</div>',
    '<code class="language-mermaid">{}</code>',
    '<pre><code class="language-mermaid">{}</code></pre>',
  ],
)
@pytest.mark.parametrize(
  "notation",
  ["&lt;<SimulationRecord>&gt;", "&lt;&lt;SimulationRecord&gt;&gt;"],
)
def test_mermaid_stereotypes_survive_real_conversion(factory, wrapper, notation):
  diagram = "classDiagram\n    class Example {\n        " + notation + "\n    }\n"
  raw = wrapper.format(diagram).encode()
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  assert product["status"] == "complete"
  assert text.count("<<SimulationRecord>>") == 1
  assert "        <<SimulationRecord>>\n" in text
  assert "```mermaid" in text
  assert "KCTLHTML" not in text


@pytest.mark.parametrize(
  "source",
  [
    '<pre>&lt;<Example>&gt;</pre>',
    '<pre><code class="language-python">&lt;<Example>&gt;</code></pre>',
    '<script>const value = "&lt;<Example>&gt;";</script>',
    '<script type="text/vnd.mermaid">&lt;<Example>&gt;</script>',
    '<div class="mermaid" title="&lt;<Example>&gt;">Source</div>',
    '<div class="mermaid"><!-- &lt;<Example>&gt; --></div>',
    '<div class="mermaid">&lt;<Example value="x">&gt;</div>',
    '<div class="mermaid"></div>&lt;<Example>&gt;',
  ],
)
def test_mermaid_preservation_does_not_rewrite_other_source(source):
  assert _mermaid_markup(source) == source


def test_mermaid_preservation_keeps_offsets_spelling_and_multiple_blocks():
  source = (
    '<p>자료\r\nprefix</p>\n<div class="mermaid">\n'
    "&lt;<SensorA>&gt;<br>&lt;<SensorB>&gt;\n</div>"
    '<div class="mermaid">&lt;<SensorC>&gt;</div>'
    "<p>&lt;<Untouched>&gt;</p>"
  )
  expected = source
  for name in ("SensorA", "SensorB", "SensorC"):
    expected = expected.replace(
      f"&lt;<{name}>&gt;", f"&lt;&lt;{name}&gt;&gt;"
    )

  assert _mermaid_markup(source) == expected
  assert _mermaid_markup(expected) == expected


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "cp949"])
def test_mermaid_preservation_uses_the_document_encoding(encoding):
  source = (
    f'<html><head><meta charset="{encoding}"></head><body>'
    '<div class="mermaid">classDiagram\n'
    "    class Example {\n        &lt;<Sensor>&gt;\n    }\n"
    "</div><p>자료</p></body></html>"
  )

  content = prepare_html(source.encode(encoding))

  assert "<<Sensor>>" in next(iter(content.literals.values()))
  assert "자료" in content.markup.decode()
  assert content.problems == []


def test_semantic_blocks_remain_separate_without_splitting_inline_text(factory):
  raw = (
    b"<header>Heading</header><main><section>Section</section>"
    b"<article>Article</article><aside>Aside</aside></main>"
    b"<footer>Footer</footer><nav>Navigation</nav>"
    b"<p>pre<span>fix</span></p>"
    b"<pre><code>&lt;footer&gt;literal&lt;/footer&gt;</code></pre>"
  )
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  lines = {line.strip() for line in text.splitlines()}
  assert product["status"] == "complete"
  assert {
    "Heading", "Section", "Article", "Aside", "Footer", "Navigation", "prefix",
  } <= lines
  assert "<footer>literal</footer>" in text
  assert "pre fix" not in text
  assert "KCTLHTML" not in text


def test_card_and_project_boundaries_preserve_reference_table_order(factory):
  tables = "".join(
    f"<section><h2>Reference table {number}</h2>"
    f"<table><tr><td>Value {number}</td></tr></table></section>"
    for number in range(1, 4)
  )
  raw = (
    '<main><div class="card"><strong>Development</strong>'
    "<span>9–30 November</span></div>"
    "<h2>Assignments</h2><h2>Three-week schedule</h2></main>"
    "<main>" + tables + "</main>"
    '<main><details class="project"><summary><span class="number">01</span>'
    '<span><span class="project-name">Project title</span><br>'
    '<span class="meta">Project metadata</span></span></summary>'
    "<p>Project instructions</p></details></main>"
  ).encode()
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  lines = {line.strip() for line in text.replace("**", "").splitlines()}
  assert product["status"] == "complete"
  assert {"Development", "9–30 November", "01", "Project title"} <= lines
  expected = [
    "Assignments", "Three-week schedule", "Reference table 1", "Value 1",
    "Reference table 2", "Value 2", "Reference table 3", "Value 3",
    "Project title", "Project instructions",
  ]
  positions = [text.index(value) for value in expected]
  assert positions == sorted(positions)
  assert all(text.count(f"Value {number}") == 1 for number in range(1, 4))
  assert "KCTLHTML" not in text


@pytest.mark.parametrize(
  "markup",
  [
    "<p><strong>pre</strong><span>fix</span></p>",
    '<p class="card"><strong>pre</strong><span>fix</span></p>',
    '<div class="other"><strong>pre</strong><span>fix</span></div>',
    '<div class="card"><span><strong>pre</strong><span>fix</span></span></div>',
    '<summary><span class="number">pre</span><span>fix</span></summary>',
    '<details class="other"><summary><span class="number">pre</span>'
    "<span>fix</span></summary></details>",
    '<code><div class="card"><strong>pre</strong><span>fix</span></div></code>',
    '<div class="faculty"><strong>pre</strong><span>fix</span></div>',
    '<header><div class="other"><strong>pre</strong><span>fix</span>'
    "</div></header>",
    '<article><div class="meta"><span>pre</span><span>fix</span></div></article>',
    '<header><section><div class="meta"><span>pre</span><span>fix</span>'
    "</div></section></header>",
    '<div class="legend"><span>pre</span><span>fix</span></div>',
    '<div class="chart"><section><div class="legend">'
    "<span>pre</span><span>fix</span></div></section></div>",
    '<p><a href="#first">pre</a><a href="#second">fix</a></p>',
    '<nav><a href="#first">pre</a><a href="https://example.com/">fix</a></nav>',
    '<nav><span><a href="#first">pre</a><a href="#second">fix</a></span></nav>',
  ],
)
def test_html_layout_corrections_do_not_split_unrelated_inline_content(markup):
  prepared = prepare_html(markup.encode())
  page = soup_html(prepared.markup)

  assert page.find("br") is None
  assert page.get_text("", strip=False) == "prefix"
  assert prepared.problems == []


def test_html_layout_corrections_leave_protected_code_unchanged():
  source = (
    '<div class="card"><strong>Label</strong><span>Value</span></div>'
    '<details class="project"><summary><span class="number">01</span>'
    "<span>Title</span></summary></details>"
  )

  prepared = prepare_html(
    f'<pre><code class="language-html">{escape(source)}</code></pre>'.encode()
  )

  assert list(prepared.literals.values()) == [
    "```html\n" + source + "\n```\n",
  ]
  assert soup_html(prepared.markup).find("br") is None


@pytest.fixture(params=[
  (
    '<header><div class="faculty"><strong>Faculty name</strong>{gap}'
    "<span>Faculty role</span>{gap}<span>Faculty institution</span></div></header>",
    ("Faculty name", "Faculty role", "Faculty institution"),
  ),
  (
    '<nav><a href="#report">Guide report</a>{gap}'
    '<a href="#source">Guide source</a>{gap}'
    '<a href="#slides">Guide slides</a></nav>',
    ("Guide report", "Guide source", "Guide slides"),
  ),
  (
    '<header><div class="meta"><span>Audience label</span>{gap}'
    "<span>Duration label</span>{gap}<span>Format label</span></div></header>",
    ("Audience label", "Duration label", "Format label"),
  ),
  (
    '<div class="chart"><div class="legend">'
    "<span><i></i>Measured value</span>{gap}"
    "<span><i></i>Target value</span>{gap}"
    "<span><i></i>Threshold value</span></div></div>",
    ("Measured value", "Target value", "Threshold value"),
  ),
], ids=["faculty", "navigation", "metadata", "legend"])
def html_layout(request):
  return request.param


@pytest.mark.parametrize("gap", ["", " \n ", "<br>"])
def test_verified_html_layout_boundaries_survive_real_conversion(
  factory, monkeypatch, html_layout, gap
):
  no_network(monkeypatch)
  template, labels = html_layout
  markup = template.format(gap=gap)
  raw = ("<p>Before layout</p>" + markup + "<p>After layout</p>").encode()
  prepared = prepare_html(raw)
  page = soup_html(prepared.markup)
  original_links = [anchor["href"] for anchor in soup_html(markup).select("a")]
  assert len(page.find_all("br")) == len(labels) - 1
  assert [anchor["href"] for anchor in page.select("a")] == original_links
  assert prepared.problems == []
  assert prepared.images == []
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  text = body(output, product).decode()
  lines = text.splitlines()
  positions = [
    next(index for index, line in enumerate(lines) if value in line)
    for value in labels
  ]
  assert positions == sorted(set(positions))
  assert all(text.count(value) == 1 for value in labels)
  assert text.index("Before layout") < text.index(labels[0])
  assert text.index(labels[-1]) < text.index("After layout")
  assert all(target in text for target in original_links)
  assert product["status"] == "complete"
  assert "KCTLHTML" not in text
  assert processor.source.read_bytes() == raw


def test_verified_layout_rules_leave_code_examples_unchanged(html_layout):
  template, _ = html_layout
  source = template.format(gap="")

  prepared = prepare_html(
    f'<pre><code class="language-html">{escape(source)}</code></pre>'.encode()
  )
  inline = prepare_html(("<code>" + source + "</code>").encode())

  assert list(prepared.literals.values()) == [
    "```html\n" + source + "\n```\n",
  ]
  assert soup_html(prepared.markup).find("br") is None
  assert soup_html(inline.markup).find("br") is None
  assert soup_html(inline.markup).get_text() == soup_html(source).get_text()


def test_html_boundary_normalization_does_not_accumulate_breaks(html_layout):
  template, _ = html_layout
  page = soup_html(template.format(gap=""))

  conversion._html_blocks(page)
  before = str(page)
  conversion._html_blocks(page)

  assert str(page) == before


def test_static_html_does_not_reconstruct_javascript_risk_rows(
  factory, monkeypatch
):
  no_network(monkeypatch)
  raw = (
    b"<h1>Worksheet</h1><p>Static instructions</p><div id=risks></div>"
    b"<script>const initialRisks = ['Dynamic risk label'];"
    b"document.getElementById('risks').innerHTML = "
    b"'<input type=range value=7>Dynamic risk label';</script>"
  )
  engines = Engines()
  processor, output = factory(".html", raw, engines=engines)

  try:
    product, _ = processor.run()
  finally:
    engines.close()

  assert product["status"] == "complete"
  assert b"Static instructions" in body(output, product)
  assert b"Dynamic risk label" not in body(output, product)
  assert b"initialRisks" not in body(output, product)


def vector_pdf(kind="direct", text=True, replacement=None):
  drawing = b"q 1 1 0 rg 20 100 60 10 re f Q"
  sentence = (
    b"Synthetic document with enough native text for dense-page extraction."
  )
  content = (
    b"BT /F1 10 Tf 20 150 Td (" + sentence + b") Tj ET\n"
    if text else b""
  )
  content += drawing if kind == "direct" else b"/Fx Do" if kind == "form" else b""
  resources = b"/Font << /F1 5 0 R >>"
  if kind == "form":
    resources += b" /XObject << /Fx 6 0 R >>"

  font = b"/Type /Font /Subtype /Type1 /BaseFont /Helvetica"
  if replacement is not None:
    mapping_id = 7 if kind == "form" else 6
    font += f" /Encoding /WinAnsiEncoding /ToUnicode {mapping_id} 0 R".encode()

  objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    (
      b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200]"
      b" /Resources << " + resources + b" >> /Contents 4 0 R >>"
    ),
    (
      f"<< /Length {len(content)} >>\nstream\n".encode()
      + content + b"\nendstream"
    ),
    b"<< " + font + b" >>",
  ]
  if kind == "form":
    objects.append(
      b"<< /Type /XObject /Subtype /Form /BBox [0 0 400 200]"
      b" /Resources << >> /Length "
      + str(len(drawing)).encode()
      + b" >>\nstream\n" + drawing + b"\nendstream"
    )
  if replacement is not None:
    mappings = "\n".join(
      f"<{code:02X}> <{ord(replacement) if code == 83 else code:04X}>"
      for code in sorted(set(sentence))
    )
    cmap = (
      "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
      "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
      "/CMapName /Synthetic def\n/CMapType 2 def\n"
      "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
      f"{len(set(sentence))} beginbfchar\n{mappings}\nendbfchar\n"
      "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode()
    objects.append(
      f"<< /Length {len(cmap)} >>\nstream\n".encode()
      + cmap + b"endstream"
    )

  raw = bytearray(b"%PDF-1.7\n")
  offsets = [0]
  for number, value in enumerate(objects, 1):
    offsets.append(len(raw))
    raw.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")

  xref = len(raw)
  raw.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
  for offset in offsets[1:]:
    raw.extend(f"{offset:010d} 00000 n \n".encode())
  raw.extend(
    f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
    f"startxref\n{xref}\n%%EOF\n".encode()
  )
  return bytes(raw)


@pytest.mark.parametrize("kind", ["none", "direct", "form"])
def test_real_pdf_vectors_are_reported_without_rendering_or_answer_inference(
  factory, kind
):
  import pypdfium2 as pdfium

  raw = vector_pdf(kind)
  with pdfium.PdfDocument(raw) as document:
    with closing(document[0]) as page:
      assert _annotations(page) == set()
      assert _pdf_vectors(page) is (kind != "none")

  processor, output = factory(".pdf", raw)
  product, visuals = processor.run()
  coverage = product["summary"].get("coverage", [])

  assert b"Synthetic document" in body(output, product)
  assert product["retry"] is False
  assert visuals["status"] == "complete"
  assert visuals["images"] == []
  assert processor.source.read_bytes() == raw
  assert product["status"] == ("complete" if kind == "none" else "partial")
  assert len(coverage) == int(kind != "none")

  if coverage:
    assert "pages 1 contain vector graphics" in coverage[0]
    assert "Flattened markings" in coverage[0]
    assert "selected" not in coverage[0]
    assert "answer" not in coverage[0]


def test_sparse_vector_page_keeps_existing_full_page_rendering(factory):
  processor, output = factory(".pdf", vector_pdf(text=False))

  product, visuals = processor.run()

  assert product["status"] == "complete"
  assert product["content_bytes"] == 0
  assert "coverage" not in product["summary"]
  assert len(visuals["images"]) == 1


@pytest.mark.parametrize("limit", ["PDF_OBJECT_LIMIT", "PDF_FORM_DEPTH"])
def test_pdf_vector_inspection_limits_preserve_text_and_report_uncertainty(
  factory, monkeypatch, limit
):
  monkeypatch.setattr(conversion, limit, 0)
  processor, output = factory(".pdf", vector_pdf("form"))

  product, visuals = processor.run()

  assert b"Synthetic document" in body(output, product)
  assert product["status"] == "partial"
  assert product["retry"] is False
  assert product["summary"]["coverage"] == [
    "PDF vector inspection incomplete on pages 1",
  ]
  assert visuals["status"] == "complete"
  assert visuals["images"] == []


def test_pdf_vector_api_failure_is_retryable_without_losing_native_text(
  factory, monkeypatch
):
  import pypdfium2 as pdfium

  monkeypatch.setattr(pdfium.raw, "FPDFPage_CountObjects", lambda page: -1)
  processor, output = factory(".pdf", vector_pdf())

  product, _ = processor.run()

  assert b"Synthetic document" in body(output, product)
  assert product["retry"] is True
  assert "PDF vector inspection incomplete on pages 1" in (
    product["summary"]["coverage"]
  )


@pytest.mark.parametrize(
  "error",
  [StorageFailure("read failed"), ProtectionFailure("unsafe"), Cancelled()],
)
def test_pdf_vector_inspection_does_not_soften_fatal_errors(
  factory, monkeypatch, error
):
  def fail(page):
    raise error

  monkeypatch.setattr(conversion, "_pdf_vectors", fail)
  processor, _ = factory(".pdf", vector_pdf())

  with pytest.raises(type(error)) as captured:
    processor.run()

  assert captured.value is error


@pytest.mark.parametrize("step", ["convert", "ocr"])
def test_real_pdf_broken_mapping_schedules_a_full_page_without_merging_text(
  factory, step
):
  import pypdfium2 as pdfium

  raw = vector_pdf("form", replacement="\ufffd")
  with pdfium.PdfDocument(raw) as document:
    with closing(document[0]) as page:
      with closing(page.get_textpage()) as textpage:
        native = textpage.get_text_range().strip()
      assert _pdf_vectors(page)
      assert _annotations(page) == set()
  assert len(native) >= 40
  assert native.startswith("\ufffd")
  engines = FakeEngines(recognized="Supplemental transcription")
  processor, output = factory(".pdf", raw, step=step, engines=engines)

  product, visuals = processor.run()

  assert product["retry"] is False
  assert visuals["status"] == "complete"
  assert len(visuals["images"]) == 1
  image = visuals["images"][0]
  assert image["label"] == "Page 1"
  assert image["page"] == 1
  with Image.open(output.resolve(image["path"]).path) as rendered:
    assert rendered.size == (800, 400)
  if step == "convert":
    assert product["status"] == "partial"
    assert body(output, product).decode() == f"## Page 1\n\n{native}\n"
    assert engines.ocr_calls == []
    coverage = product["summary"]["coverage"]
    assert len(coverage) == 1
    assert "U+FFFD: 1" in coverage[0]
  else:
    assert product["status"] == "complete"
    assert engines.ocr_calls == [(800, 400)]
    assert body(output, product) == (
      b"## Page 1\n\nSupplemental transcription\n"
    )


@contextmanager
def archived_document(private_dir, kind, raw):
  archive = Archive(private_dir / "archive", create=True)
  course = "course/one"
  owner = course + "/page"
  folder = "documents/Course"
  name = "source" + kind
  url = "https://ctl.kmu.ac.kr/fileDownServlet?id=source"
  key = identity(owner, url)

  try:
    archive.prepare()
    archive.item("index", {
      "title": "Archive",
      "path": "documents/index.md",
      "folder": "documents",
      "status": "archived",
      "children": [course],
      "parent": None,
    }, None)
    archive.item(course, {
      "title": "Course",
      "path": folder + "/index.md",
      "folder": folder,
      "status": "archived",
      "children": [owner],
      "parent": "index",
      "course_id": "one",
      "include_submissions": True,
    }, None)
    archive.item(owner, {
      "title": "Page",
      "path": folder + "/page.md",
      "folder": folder,
      "status": "archived",
      "documents": [key],
      "parent": course,
    }, "Instructions\n")
    work = archive.new_work()
    archive.mkdir(work + "/new")
    archive.write(work + "/new/" + name, raw)
    archive.document(key, {
      "owner": owner,
      "url": url,
      "name": name,
      "folder": folder + "/Source",
      "original": name,
      "role": "attachment",
      "bytes": len(raw),
      "sha256": digest(raw),
      "signature": signature(archive.path(work + "/new/" + name)),
      "result": None,
      "attempts": {},
    }, work)
    yield archive, key
    assert archive.read(folder + "/Source/" + name, len(raw)) == raw
    assert archive.data["items"][owner]["documents"] == [key]
    archive.validate()
  finally:
    archive.close()


def publish_document(archive, key):
  document = copy.deepcopy(archive.data["documents"][key])
  work = archive.new_work()
  archive.mkdir(work + "/new")
  spec = {
    "key": key,
    "root": str(archive.root),
    "document": document,
    "signature": copy.deepcopy(document["signature"]),
    "settings": copy.deepcopy(archive.settings),
    "steps": ["convert", "ocr"],
    "retry_steps": [],
    "force": False,
    "work": work,
  }
  engines = Engines()

  try:
    message = DocumentJob(spec, engines).run()
  finally:
    engines.close()

  archive.publish(spec, {**message, "notes": [], "seconds": 0})
  return archive.data["documents"][key]["result"]["nodes"][""]


def export_and_reuse(archive, key, expected_code):
  course = "course/one"
  path = "extracted/Course.txt"
  assert archive.reusable(archive.data["documents"][key], ["convert", "ocr"])
  assert export_courses(archive, [course]) == expected_code
  exported = archive.read(path, 65536)
  processor = Processor(archive, force=False, jobs=1, timeout=30)

  try:
    processor.enqueue(key)
    processor.tick()
    assert not processor.busy()
    assert processor.reused == 1
    assert processor.failed == 0
    assert processor.workers.workers == []
  finally:
    processor.close()

  assert export_courses(archive, [course]) == expected_code
  assert archive.read(path, 65536) == exported
  return exported


@pytest.mark.parametrize("kind", [".html", ".pdf"])
def test_fidelity_updates_publish_export_and_reuse_without_ocr_or_rediscovery(
  private_dir, monkeypatch, kind
):
  no_network(monkeypatch)
  raw = (
    b'<main><div class="mermaid">classDiagram\n'
    b'    class Example {\n        &lt;<Sensor>&gt;\n    }\n'
    b"</div><aside>Body end</aside></main><footer>Footer</footer>"
    b'<div class="card"><strong>Development</strong><span>Dates</span></div>'
    if kind == ".html" else vector_pdf()
  )

  def unexpected(*args, **kwargs):
    pytest.fail("conversion-only update repeated discovery, visuals, or OCR")

  with archived_document(private_dir, kind, raw) as (archive, key):
    current = copy.deepcopy(archive.settings)
    archive.settings["convert"].pop("html_source")
    archive.settings["convert"].pop("pdf_vectors")
    for boundary in ("walk", "visuals", "ocr"):
      assert archive.settings[boundary] == current[boundary]

    with monkeypatch.context() as patch:
      patch.setattr(conversion, "_mermaid_markup", lambda source: source)
      patch.setattr(conversion, "_html_blocks", lambda page: None)
      patch.setattr(conversion, "_pdf_vectors", lambda page: False)
      previous = copy.deepcopy(publish_document(archive, key))

    assert previous["products"]["convert"]["status"] == "complete"
    assert previous["visuals"]["status"] == "complete"
    archive.settings = current
    assert not archive.reusable(archive.data["documents"][key], ["convert", "ocr"])

    with monkeypatch.context() as patch:
      patch.setattr(ContentWalker, "walk", unexpected)
      patch.setattr(FileProcessor, "pdf_visuals", unexpected)
      patch.setattr(FileProcessor, "html_visuals", unexpected)
      patch.setattr(Engines, "recognize", unexpected)
      node = publish_document(archive, key)

    assert node["products"]["ocr"] == previous["products"]["ocr"]
    assert node["attempts"]["convert"]["settings"] == current["convert"]
    assert node["attempts"]["convert"]["retry"] is False
    assert node["attempts"]["convert"]["retained"] is (kind == ".pdf")
    exported = export_and_reuse(archive, key, 3 if kind == ".pdf" else 0)

    if kind == ".html":
      assert b"<<Sensor>>" in exported
      assert b"Body endFooter" not in exported
      assert b"Footer" in exported
      assert b"DevelopmentDates" not in exported.replace(b"**", b"")
      assert b"KCTLHTML" not in exported
    else:
      assert b"Synthetic document" in exported
      assert b"Flattened markings" in exported
      assert b"Previous result retained" in exported
      assert node["products"]["convert"]["content_sha256"] == (
        previous["products"]["convert"]["content_sha256"]
      )


def test_new_page_ocr_survives_retained_native_conversion_and_reuses(
  private_dir, monkeypatch
):
  no_network(monkeypatch)
  raw = vector_pdf("form", replacement="\ufffd")
  recognized = []

  def old_render_decision(self, text):
    return len(text.strip()) < self.settings["visuals"]["sparse_text_threshold"]

  def unexpected(*args, **kwargs):
    pytest.fail("visual settings change must not repeat content discovery")

  def recognize(self, image):
    recognized.append(image.size)
    return "Supplemental rendered transcription"

  with archived_document(private_dir, ".pdf", raw) as (archive, key):
    current = copy.deepcopy(archive.settings)
    archive.settings["visuals"]["pdf_visuals"] -= 1
    archive.settings["convert"]["pdf_coverage"] -= 1
    assert archive.settings["walk"] == current["walk"]
    assert archive.settings["convert"] != current["convert"]
    assert archive.settings["ocr"] != current["ocr"]

    with monkeypatch.context() as patch:
      patch.setattr(FileProcessor, "_pdf_needs_render", old_render_decision)
      previous = copy.deepcopy(publish_document(archive, key))

    assert previous["products"]["convert"]["status"] == "partial"
    assert previous["visuals"]["status"] == "complete"
    assert previous["visuals"]["images"] == []
    assert previous["products"]["ocr"]["markdown"] is None
    archive.settings = current
    assert not archive.reusable(archive.data["documents"][key], ["convert", "ocr"])

    with monkeypatch.context() as patch:
      patch.setattr(ContentWalker, "walk", unexpected)
      patch.setattr(Engines, "recognize", recognize)
      node = publish_document(archive, key)

    assert recognized == [(800, 400)]
    assert node["attempts"]["convert"]["retained"] is True
    assert node["attempts"]["convert"]["retry"] is False
    assert node["products"]["convert"]["content_sha256"] == (
      previous["products"]["convert"]["content_sha256"]
    )
    assert node["products"]["ocr"]["settings"] == current["ocr"]
    assert node["products"]["ocr"]["status"] == "complete"
    assert node["products"]["ocr"]["summary"]["text"] == 1
    assert node["visuals"]["settings"] == current["visuals"]
    assert len(node["visuals"]["images"]) == 1
    assert node["visuals"]["images"][0]["label"] == "Page 1"
    files = archive.data["documents"][key]["result"]["files"]
    assert set(node["visuals"]["outputs"]) <= set(files)
    exported = export_and_reuse(archive, key, 3)
    assert "\ufffd".encode() in exported
    assert b"U+FFFD" in exported
    assert b"Supplemental rendered transcription" in exported
    assert b"/ OCR" in exported
    assert b"Previous result retained" in exported


def test_control_coverage_reaches_export_when_older_body_is_retained(
  private_dir, monkeypatch
):
  no_network(monkeypatch)
  raw = vector_pdf("none", replacement="\x04")
  detect = conversion._suspect_codes

  def old_codes(text):
    return {code: count for code, count in detect(text).items() if code >= 32}

  def unexpected(*args, **kwargs):
    pytest.fail("coverage-only update must reuse discovery, visuals, and OCR")

  with archived_document(private_dir, ".pdf", raw) as (archive, key):
    current = copy.deepcopy(archive.settings)
    archive.settings["convert"]["pdf_coverage"] -= 1

    with monkeypatch.context() as patch:
      patch.setattr(conversion, "_suspect_codes", old_codes)
      previous = copy.deepcopy(publish_document(archive, key))

    assert previous["products"]["convert"]["status"] == "complete"
    archive.settings = current

    with monkeypatch.context() as patch:
      patch.setattr(ContentWalker, "walk", unexpected)
      patch.setattr(FileProcessor, "pdf_visuals", unexpected)
      patch.setattr(Engines, "recognize", unexpected)
      node = publish_document(archive, key)

    assert node["attempts"]["convert"]["retained"] is True
    assert node["attempts"]["convert"]["retry"] is False
    assert any(
      "U+0004: 1" in note
      for note in node["attempts"]["convert"]["summary"]["coverage"]
    )
    assert node["products"]["convert"]["content_sha256"] == (
      previous["products"]["convert"]["content_sha256"]
    )
    assert node["products"]["ocr"] == previous["products"]["ocr"]
    exported = export_and_reuse(archive, key, 3)
    assert b"U+0004: 1" in exported
    assert b"Previous result retained" in exported


@pytest.mark.parametrize("partial", [False, True])
def test_html_layout_update_respects_retention_and_reuses_unaffected_operations(
  private_dir, monkeypatch, partial
):
  no_network(monkeypatch)
  raw = (
    '<header><div class="meta"><span>Audience label</span>'
    "<span>Duration label</span></div></header><p>Static instructions</p>"
    + (
      "<svg><title>Static chart description</title></svg>"
      if partial else ""
    )
  ).encode()
  converted = []
  original_markdown = Engines.markdown

  def unexpected(*args, **kwargs):
    pytest.fail("HTML layout update repeated discovery or OCR")

  def markdown(self, path, kind, raw=None):
    text = original_markdown(self, path, kind, raw)
    converted.append(text)
    return text

  with archived_document(private_dir, ".html", raw) as (archive, key):
    current = copy.deepcopy(archive.settings)
    archive.settings["convert"]["html_source"] -= 1
    for boundary in ("walk", "visuals", "ocr"):
      assert archive.settings[boundary] == current[boundary]

    with monkeypatch.context() as patch:
      patch.setattr(conversion, "_html_blocks", lambda page: None)
      previous = copy.deepcopy(publish_document(archive, key))

    old_product = previous["products"]["convert"]
    assert old_product["status"] == ("partial" if partial else "complete")
    doc = archive.data["documents"][key]
    old_body = archive.read_content(
      doc["folder"] + "/" + old_product["markdown"],
      old_product,
      size=doc["result"]["files"][old_product["markdown"]],
    )
    assert b"Audience labelDuration label" in old_body
    archive.settings = current
    assert not archive.reusable(doc, ["convert", "ocr"])

    with monkeypatch.context() as patch:
      patch.setattr(ContentWalker, "walk", unexpected)
      patch.setattr(Engines, "recognize", unexpected)
      patch.setattr(Engines, "markdown", markdown)
      if not partial:
        patch.setattr(FileProcessor, "html_visuals", unexpected)
      node = publish_document(archive, key)

    assert len(converted) == 1
    lines = {line.strip() for line in converted[0].splitlines()}
    assert {"Audience label", "Duration label"} <= lines
    assert node["products"]["ocr"] == previous["products"]["ocr"]
    assert node["attempts"]["ocr"] == previous["attempts"]["ocr"]
    attempt = node["attempts"]["convert"]
    assert attempt["settings"] == current["convert"]
    assert attempt["retained"] is partial
    assert attempt["retry"] is False
    assert attempt["status"] == ("partial" if partial else "complete")
    product = node["products"]["convert"]
    assert (product["content_sha256"] == old_product["content_sha256"]) is partial

    exported = export_and_reuse(archive, key, 3 if partial else 0)
    if partial:
      assert old_body in exported
      assert b"Previous result retained" in exported
      assert b"inline SVG is not rasterized or OCRed" in exported
      assert product["settings"] == old_product["settings"]
    else:
      assert b"Audience labelDuration label" not in exported
      assert b"Audience label" in exported
      assert b"Duration label" in exported
      assert product["settings"] == current["convert"]
