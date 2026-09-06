"""app/affiliate/token.py — opaque token 生成・形式検証。"""

from __future__ import annotations

from app.affiliate.token import generate_token, is_well_formed_token


def test_generate_token_is_url_safe_and_high_entropy() -> None:
    t = generate_token()
    assert is_well_formed_token(t)
    assert set(t) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    # secrets.token_urlsafe(16) -> 128bit -> 22 文字
    assert len(t) >= 22


def test_generate_token_is_not_sequential_or_repeating() -> None:
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200  # 事実上衝突しない


def test_token_generation_takes_no_semantic_inputs() -> None:
    # 生成関数に article id / program id / slug / provider / host を渡す引数が無い。
    import inspect

    sig = inspect.signature(generate_token)
    assert list(sig.parameters) == []
    assert generate_token() != generate_token()


def test_is_well_formed_token_rejects_bad_shapes() -> None:
    assert not is_well_formed_token("")
    assert not is_well_formed_token("short")
    assert not is_well_formed_token("has space inside token xxxx")
    assert not is_well_formed_token("has/slash/aaaaaaaaaaaaaaaa")
    assert not is_well_formed_token("aaaa.aaaa.aaaa.aaaa")
    assert not is_well_formed_token("x" * 65)
    assert not is_well_formed_token(None)
    assert not is_well_formed_token(12345)
