"""app/article/draft_output_contract.py の pure テスト。"""

import json

import pytest

from app.article.draft_output_contract import DraftContractError, parse_draft_output


def _out(**over) -> str:
    base = {
        "meta_description": "業務効率化ツールのおすすめを目的別に比較して解説します。",
        "body_markdown": "## はじめに\n本文です。\n\n## 比較\n各ツール。",
        "generation_notes": ["前提なし"],
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def test_parses_valid_output() -> None:
    p = parse_draft_output(_out())
    assert p.meta_description.startswith("業務効率化")
    assert p.body_markdown.startswith("## ")
    assert p.generation_notes == ["前提なし"]


def test_strips_code_fence() -> None:
    p = parse_draft_output("```json\n" + _out() + "\n```")
    assert p.body_markdown.startswith("## ")


def test_rejects_non_json() -> None:
    with pytest.raises(DraftContractError):
        parse_draft_output("これはJSONではありません")


def test_rejects_missing_body() -> None:
    with pytest.raises(DraftContractError):
        parse_draft_output(_out(body_markdown=""))


def test_rejects_missing_meta() -> None:
    with pytest.raises(DraftContractError):
        parse_draft_output(_out(meta_description="   "))


def test_rejects_bad_notes_type() -> None:
    with pytest.raises(DraftContractError):
        parse_draft_output(_out(generation_notes="not a list"))


def test_rejects_empty() -> None:
    with pytest.raises(DraftContractError):
        parse_draft_output("")
