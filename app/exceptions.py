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
