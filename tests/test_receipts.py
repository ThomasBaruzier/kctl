import pytest

from kctl.auth_html import soup_html
from kctl.core import Failure
from kctl.courses import verify_receipt


EMPTY_MESSAGE = "제출 내역이 없습니다."


def empty_receipt(content):
  return soup_html(
    '<dl class="element"><dt>제출정보</dt><dd>'
    '<table class="boardListWrite" summary="산출물 제출 정보">'
    f'<tr><td colspan="2">{content}</td></tr>'
    "</table></dd></dl>"
  ).dl


@pytest.mark.parametrize(
  "content",
  [
    EMPTY_MESSAGE,
    "\n  " + EMPTY_MESSAGE + "\n",
    "<span>" + EMPTY_MESSAGE + "</span>",
  ],
)
def test_exact_empty_team_receipt_is_recognized(content):
  assert verify_receipt(empty_receipt(content), team=True) is True


@pytest.mark.parametrize(
  "extra",
  [
    " Additional output",
    "<span>Additional output</span>",
    "<a href='/file'>File</a>",
    "<a href='/file'></a>",
    "<input name='hidden-output'>",
    "<img src='/lmsdata/output.png'>",
    "<button>Download</button>",
    "<textarea>Saved output</textarea>",
  ],
)
def test_empty_team_message_cannot_hide_additional_content(extra):
  with pytest.raises(Failure, match="empty team-output layout"):
    verify_receipt(empty_receipt(EMPTY_MESSAGE + extra), team=True)


def test_individual_receipt_does_not_adopt_team_empty_layout():
  with pytest.raises(Failure, match="receipt layout"):
    verify_receipt(empty_receipt(EMPTY_MESSAGE), team=False)


def test_populated_team_receipt_is_not_misclassified_by_a_quoted_message():
  card = soup_html(
    '<dl class="element"><dt>제출정보</dt><dd><table>'
    "<tr><th>제출상태</th><td>Submitted</td></tr>"
    "<tr><th>첨부파일</th><td>None</td></tr>"
    f"<tr><th>산출물내용</th><td>Observed message: {EMPTY_MESSAGE}</td></tr>"
    "</table></dd></dl>"
  ).dl

  assert verify_receipt(card, team=True) is False
