"""アプリケーション層で扱う例外。

Repository / DB 由来の低レベル例外 (SQLAlchemy の ``IntegrityError`` 等) を
そのまま上位へ漏らさず、ここで定義した型へ変換して扱う。
細分化はせず、必要最小限に留める。
"""

from __future__ import annotations


class ApplicationError(Exception):
    """アプリケーション層の例外の基底クラス。"""


class EntityNotFoundError(ApplicationError):
    """指定した識別子のエンティティが存在しない。"""

    def __init__(self, entity: str, identifier: object) -> None:
        super().__init__(f"{entity} not found: {identifier!r}")
        self.entity = entity
        self.identifier = identifier


class DuplicateEntityError(ApplicationError):
    """一意であるべき値が既に存在する。"""

    def __init__(self, entity: str, field: str, value: object) -> None:
        super().__init__(f"{entity} already exists: {field}={value!r}")
        self.entity = entity
        self.field = field
        self.value = value


class InvalidStatusTransitionError(ApplicationError):
    """許可されていない status 遷移が要求された。"""

    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"{entity}: '{current}' -> '{target}' is not allowed")
        self.entity = entity
        self.current = current
        self.target = target


class IncompleteSignalSetError(ApplicationError):
    """Opportunity Score 計算に必要な component の Signal が揃っていない。"""

    def __init__(self, keyword_id: int, missing_components: list[str]) -> None:
        missing = ", ".join(missing_components)
        super().__init__(
            f"Keyword {keyword_id}: missing signals for components: {missing}"
        )
        self.keyword_id = keyword_id
        self.missing_components = list(missing_components)


class FactValidationError(ApplicationError):
    """Source / ArticleFact の入力が業務ルール上不正 (verified なのに source なし、
    別 Article の Source 参照、URL に credential、value 型不一致 など)。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"fact validation error: {reason}")
        self.reason = reason


class EntityInUseError(ApplicationError):
    """他レコードから参照されているため削除できない (例: Fact が参照する Source)。"""

    def __init__(self, entity: str, identifier: object, used_by: str) -> None:
        super().__init__(
            f"{entity} {identifier!r} is still referenced by {used_by}"
        )
        self.entity = entity
        self.identifier = identifier
        self.used_by = used_by


class DraftInputNotReadyError(ApplicationError):
    """DraftInputSnapshot の freeze gate を満たしていない。

    Article が planned でない / body 済み / primary 不整合 / FactPack not ready /
    required stale / claim partition 崩れ / ArticlePlan build 失敗 など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"draft input not ready: {reason}")
        self.reason = reason


class SnapshotInputChangedError(ApplicationError):
    """preview 後に生成入力が変化し、``expected_content_hash`` と一致しない。

    Human がレビューしていない入力を freeze しないための drift guard。
    """

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            "draft input changed since preview: expected content_hash "
            f"{expected!r}, current {actual!r}"
        )
        self.expected = expected
        self.actual = actual


class PromptInputChangedError(ApplicationError):
    """prepare 時に ``expected_prompt_hash`` / ``expected_rendered_prompt_hash`` が
    現在の builder / renderer の出力と一致しない (Human が review した prompt と別物)。
    """

    def __init__(self, field: str, expected: str, actual: str) -> None:
        super().__init__(
            f"draft prompt changed since preview: {field} expected {expected!r}, "
            f"current {actual!r}"
        )
        self.field = field
        self.expected = expected
        self.actual = actual


class DraftGenerationStateError(ApplicationError):
    """DraftGenerationRun / Article が要求された遷移を許さない状態にある。

    run が prepared でない / 同一 Article に running run が既にある /
    Article status が planned・drafting 以外 / idempotency_key の identity 衝突 など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"draft generation state error: {reason}")
        self.reason = reason


class DraftGenerationNotReadyError(ApplicationError):
    """生成 artifact が整合しておらず実行できない。

    保存済み prompt_package の hash が prompt_input_hash と不一致 /
    rendered_prompt の hash 不一致 / snapshot binding 不整合 /
    PromptPackage に禁止キー (commission 等) が混入 など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"draft generation not ready: {reason}")
        self.reason = reason


class DraftPromotionStateError(ApplicationError):
    """Article / source run が draft promotion を許さない状態にある。

    Article status が drafting 以外 / Article.body・meta が既に埋まっている /
    source run が succeeded でない / source run が別 Article のもの /
    候補 validator が warn・fail / idempotency_key の identity 衝突 など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"draft promotion state error: {reason}")
        self.reason = reason


class CandidateChangedError(ApplicationError):
    """promote 時に ``expected_body_hash`` / ``expected_meta_hash`` /
    ``expected_candidate_content_hash`` のいずれかが現在の候補から計算した hash と
    一致しない (Human が承認した本文と別物)。3-hash drift guard。
    """

    def __init__(self, field: str, expected: str, actual: str) -> None:
        super().__init__(
            f"draft candidate changed since review: {field} expected {expected!r}, "
            f"current {actual!r}"
        )
        self.field = field
        self.expected = expected
        self.actual = actual


class RenderedCandidateChangedError(ApplicationError):
    """WordPress draft request の組み立て時に ``expected_renderer_version`` /
    ``expected_rendered_content_hash`` が現在の renderer 出力と一致しない
    (Human が HTML を承認した時点と renderer/コードが drift している)。
    """

    def __init__(self, field: str, expected: str, actual: str) -> None:
        super().__init__(
            f"rendered candidate changed since approval: {field} expected "
            f"{expected!r}, current {actual!r}"
        )
        self.field = field
        self.expected = expected
        self.actual = actual


class WordPressTargetError(ApplicationError):
    """WORDPRESS_BASE_URL が正規化できない (scheme 不正 / userinfo / query / fragment /
    hostname 欠落 / wp-json path 混入 など)。credential 値はメッセージに含めない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"wordpress target invalid: {reason}")
        self.reason = reason


class WordPressDraftRunStateError(ApplicationError):
    """WordPressDraftRun / Article / Promotion が prepare を許さない状態にある。

    Article が review でない / 既に公開済みフィールドを持つ / promotion 不整合 /
    publication validator 非 pass / 承認済み hash からの drift / target 未設定 /
    idempotency_key の identity 衝突 / 同一 target に active な run が既にある など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"wordpress draft run state error: {reason}")
        self.reason = reason


class ProtectedArticleStatusTransitionError(ApplicationError):
    """Article の汎用 status 変更 API では、公開ライフサイクル上の保護された target
    status (承認 / 公開) へ遷移できない。

    review -> approved は専用の publication approval workflow、
    * -> published は将来の専用 publication workflow を経由する必要がある。
    """

    def __init__(self, entity: str, target: str) -> None:
        super().__init__(
            f"{entity}: target status {target!r} requires a dedicated guarded "
            "workflow (not available via the generic status endpoint)"
        )
        self.entity = entity
        self.target = target


class ArticlePublicationApprovalError(ApplicationError):
    """Article の Human publication approval (review -> approved) を許さない状態にある。

    Article が review でない / 既に公開済みフィールドを持つ / wordpress_post_id 不一致・
    未設定 / 対応する WordPressDraftRun が succeeded でない・不整合 / 承認済み
    target_request_identity_hash からの drift / prepare 後の本文・meta drift など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"article publication approval error: {reason}")
        self.reason = reason


class WordPressPublicationRunPreparationError(ApplicationError):
    """WordPressPublicationRun の prepare を許さない状態にある。

    Article が approved でない / 既に公開済みフィールドを持つ / wordpress_post_id 未設定・
    不一致 / 本文・meta hash drift / 対応する succeeded WordPressDraftRun が無い・不整合 /
    承認済み target_request_identity_hash からの drift / configured target 不一致 /
    raw content hash の形式不正 など。credential 値はメッセージに含めない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"wordpress publication run preparation error: {reason}")
        self.reason = reason


class WordPressPublicationRunConflictError(ApplicationError):
    """同じ idempotency key が別の publication identity で既に使われている、または同一の
    Human 承認済み publication identity に対して active / succeeded な run が既に存在する。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"wordpress publication run conflict: {reason}")
        self.reason = reason


class WordPressPublicationRunExecutionError(ApplicationError):
    """WordPressPublicationRun の execute を許さない state にある (WordPress 通信前の guard
    失敗、または既に running/terminal な run への再実行要求)。credential は含めない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"wordpress publication run execution error: {reason}")
        self.reason = reason


class WordPressPublicationPreflightError(ApplicationError):
    """execute 直前の read-only preflight GET が期待外の WordPress post state を報告した
    (post 不在 / id 不一致 / status が draft でない / title・slug・excerpt drift /
    content.raw hash drift / category 未設定 など)。publish POST は送らない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"wordpress publication preflight error: {reason}")
        self.reason = reason


class WordPressAmbiguousPublishOutcomeError(ApplicationError):
    """publish POST 送信後、timeout / 接続断でレスポンスを受け取れず、WordPress 側で
    publish が成功したか不明 (定義的な 4xx/5xx とは区別する)。絶対に自動再送しない。
    Human が WordPress を直接確認してから判断する。
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"ambiguous_wordpress_publish_outcome: {message}")


class WordPressPublicationReadbackFailedError(ApplicationError):
    """publish POST は 200/status=publish で成功が確認できたが、その後の read-back GET が
    失敗した (timeout / 接続断 / 期待外レスポンス)。external publication 成功は既知。
    絶対に再 POST しない。durable run は running のまま。Human reconciliation が必要。
    """

    def __init__(self, wordpress_post_id: str) -> None:
        super().__init__(
            "external_publish_confirmed_readback_failed: "
            f"wordpress_post_id={wordpress_post_id!r} is published on WordPress but the "
            "read-back could not confirm it; do not retry; Human reconciliation required"
        )
        self.wordpress_post_id = wordpress_post_id


class WordPressPublicationExternalSuccessLocalPersistFailedError(ApplicationError):
    """publish POST + read-back の両方で publish 成功が確認できたが、ローカル DB の
    finalization commit に失敗した。WordPress 側は publish 済み。絶対に再 POST しない。
    durable run は running のまま。Human reconciliation が必要。
    """

    def __init__(self, wordpress_post_id: str) -> None:
        super().__init__(
            "external_publish_succeeded_local_persist_failed: "
            f"wordpress_post_id={wordpress_post_id!r} is published on WordPress but local "
            "finalization failed; do not retry; Human reconciliation required"
        )
        self.wordpress_post_id = wordpress_post_id


class SearchConsoleImportStateError(ApplicationError):
    """SearchConsoleImportRun の prepare / execute を許さない state にある
    (guard 失敗、既に running/terminal な run への再実行要求、無効な期間など)。
    credential / 生レスポンスは含めない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"search console import state error: {reason}")
        self.reason = reason


class SearchConsoleCredentialError(ApplicationError):
    """Search Console の service-account credential file の構成エラー
    (未設定 / 不在 / 非ファイル / 非 JSON / type 不正 / 必須フィールド欠落 /
    repo 内に配置)。private_key など secret はメッセージに一切含めない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"search console credential error: {reason}")
        self.reason = reason


class AffiliateLinkTargetError(ApplicationError):
    """AffiliateLinkTarget の create / disable / supersede が business rule 上不可。

    article/program 関係なし / program が active でない / tracking_url 不在 /
    destination 検証失敗 / destination host が independently 未承認 / 既に active な
    target がある / idempotency_key 競合 / 許可されない status 遷移 など。
    tracking_url など secret 相当値はメッセージに含めない。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"affiliate link target error: {reason}")
        self.reason = reason


class PlanApprovalError(ApplicationError):
    """Article Plan の承認要求が検証で拒否された (企画側の入力・状態の問題)。

    incomplete plan の承認・カニバリ未確認・候補外/inactive な affiliate 指定など。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"article plan approval rejected: {reason}")
        self.reason = reason


class ProviderNotConfiguredError(ApplicationError):
    """外部プロバイダの認証情報 / 設定が未設定 (運用上の構成エラー)。

    メッセージに credential 値そのものは含めない。
    """

    def __init__(self, provider: str) -> None:
        super().__init__(f"external provider '{provider}' is not configured")
        self.provider = provider


class ExternalProviderError(ApplicationError):
    """外部プロバイダ API の呼び出しに失敗した (通信・認証・SDK 内部エラー等)。

    元例外は ``__cause__`` にのみ保持し、HTTP レスポンス・メッセージには
    SDK 内部詳細や credential を露出させない。
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider


class ExternalProviderDataError(ApplicationError):
    """外部プロバイダから有効なデータ (指標) が得られなかった。

    構成エラー・通信エラーとは区別する。0 点 Signal を無条件に作らないための型。
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider


class WordPressAmbiguousOutcomeError(ApplicationError):
    """WordPress create-post POST 送信後、timeout / 接続断でレスポンスを受け取れなかった。

    WordPress 側で実際に作成が成功したか不明 (定義的な 4xx/5xx とは区別する)。
    絶対に自動再送しない。Human が WordPress を直接確認してから判断する。
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"ambiguous_wordpress_outcome: {message}")


class WordPressExternalCreateLocalPersistFailedError(ApplicationError):
    """WordPress の create-post は 201 で成功したが、ローカル DB への成功記録の commit
    に失敗した (external/local 非原子性)。

    WordPress 側には既に post が存在する可能性が高い。絶対に再 POST しない。
    Human による reconciliation が必要。
    """

    def __init__(self, wordpress_post_id: str) -> None:
        super().__init__(
            "external_create_succeeded_local_persist_failed: "
            f"wordpress_post_id={wordpress_post_id!r} may already exist on WordPress; "
            "do not retry automatically; Human reconciliation required"
        )
        self.wordpress_post_id = wordpress_post_id
