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
