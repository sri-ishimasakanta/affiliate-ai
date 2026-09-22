"""公開済み記事を更新する意図の明文化 (C9.4、pure)。

``ArticleEditorialRevision`` は、``published`` な記事を改訂するとき
``published_update_intent`` (空でない文字列) を要求する。これは
``published_update_intent_ok`` ゲートの本体であり、「公開中だと知ったうえで更新
する」という意図を改訂行に **永続的に残す** ための契約である。

C9 はこのゲートを弱めない。弱める代わりに、**人の承認という事実そのものを意図
として書き起こす**。したがってこの関数は、承認が確かに存在し、その承認がいま
適用しようとしている提案そのものに対するものであるときに限り、文字列を返す。

以下のどれか 1 つでも崩れていれば :class:`PublishedUpdateIntentError` を送出し、
呼び出し側は改訂を作れない (= ゲートは fail-closed のまま):

- ``--execute`` が明示されていない (PLAN では意図を作らない)
- 承認が存在しない
- 承認の hash がいまの提案 hash と一致しない
- 記事本文が提案時点から変わっている

つまり「承認済みで、内容が一致していて、人が明示的に実行した」という C9 の不変
条件が、そのまま既存契約の入力になる。定数の既定値でも、記事ごとの例外でもない。
"""

from __future__ import annotations

_TEMPLATE = (
    "人が承認した変更要求 {request_id} (提案 {proposal_hash} v{version}) を、"
    "公開中の記事 {article_id} へ managed 経路で適用する。"
    "承認記録 {approval_id} (decided_by={approved_by})。"
    "変更種別 {change_type}{target}。"
    "適用直前の本文 hash {source_body_hash} は提案時点と一致している。"
)


class PublishedUpdateIntentError(Exception):
    """承認の事実が揃っていないので、公開更新の意図を書けない。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"published update intent unavailable: {reason}")
        self.reason = reason


def build_published_update_intent(
    *,
    execute: bool,
    change_request_id: int,
    change_type: str,
    article_id: int,
    target_article_id: int | None,
    proposal_hash: str,
    proposal_version: int,
    approval_id: int | None,
    approved_proposal_hash: str | None,
    approved_by: str | None,
    expected_source_body_hash: str,
    current_source_body_hash: str,
) -> str:
    """承認の事実から、公開更新の意図を 1 文で書き起こす。"""

    if not execute:
        raise PublishedUpdateIntentError(
            "no explicit execution was requested; PLAN never authorizes a published update"
        )
    if approval_id is None or not (approved_proposal_hash or "").strip():
        raise PublishedUpdateIntentError("no human approval exists for this change request")
    if approved_proposal_hash != proposal_hash:
        raise PublishedUpdateIntentError(
            "the approval was granted for a different proposal; "
            "a published update must be approved for this exact proposal"
        )
    if current_source_body_hash != expected_source_body_hash:
        raise PublishedUpdateIntentError("the article body changed after the proposal was approved")

    target = f" (リンク先 {target_article_id})" if target_article_id is not None else ""
    return _TEMPLATE.format(
        request_id=change_request_id,
        proposal_hash=proposal_hash,
        version=proposal_version,
        article_id=article_id,
        approval_id=approval_id,
        approved_by=approved_by or "human",
        change_type=change_type,
        target=target,
        source_body_hash=current_source_body_hash,
    )
