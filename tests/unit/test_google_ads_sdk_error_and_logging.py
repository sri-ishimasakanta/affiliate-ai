"""C2.4 live canary で見つかった 2 件の回帰テスト (実通信 0)。

1. 実 SDK の ``ErrorCode`` (proto-plus) から authorization 等のエラー種別を分類できること。
   fake ではなく **実際の Google Ads 型** (``ErrorCode`` / ``GoogleAdsFailure`` /
   ``GoogleAdsException``) を使う。
2. SDK の失敗ログ (customer id / request id 入り) が stdout / stderr / logging に漏れないこと。
   SDK 自身の ``LoggingInterceptor.log_failed_request`` を使って実際の警告行を発生させる。
"""

from __future__ import annotations

import importlib
import logging
import sys
from types import SimpleNamespace

import google.ads.googleads.client as sdk_client
import pytest
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.interceptors.logging_interceptor import LoggingInterceptor

import scripts.discover_keyword_ideas as discover
from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.providers import google_ads as metrics_mod
from app.keyword.providers.google_ads_ideas import (
    MSG_API,
    MSG_AUTH,
    MSG_AUTHZ,
    MSG_QUOTA,
    MSG_REQUEST,
    GoogleAdsAuthError,
    GoogleAdsKeywordIdeaProvider,
    GoogleAdsQuotaError,
    _authorization_enum_name,
    authorization_subcode,
    classify_failure,
)
from app.keyword.providers.google_ads_logging import (
    SDK_LOGGER_NAMES,
    _DropAllRecords,
    suppress_google_ads_sdk_logging,
)
from tests.support.google_ads_fakes import dummy_google_ads_settings, unconfigured_settings
from tests.support.google_ads_idea_fakes import FakeGrpcError, FakeIdeasClient, FakeIdeasPager

_V = sdk_client._DEFAULT_VERSION
_ERRORS = importlib.import_module(f"google.ads.googleads.{_V}.errors.types.errors")
_CUSTOMER_ID = "1234567890"  # dummy_google_ads_settings の customer id
_REQUEST_ID = "req-7f3a91c2"
_IDENTIFIERS = (
    _CUSTOMER_ID,
    _REQUEST_ID,
    "ClientCustomerId",
    "RequestId",
    "FaultMessage",
    "dummy-developer-token",
    "dummy-client-id",
    "dummy-client-secret",
    "dummy-refresh-token",
)


def _enum(module: str, name: str):
    mod = importlib.import_module(f"google.ads.googleads.{_V}.errors.types.{module}")
    return getattr(mod, name)


def _error_code(kind: str):
    """実 SDK の (proto-plus) ErrorCode を作る。"""

    ErrorCode = _ERRORS.ErrorCode
    if kind == "authorization_error":
        value = _enum("authorization_error", "AuthorizationErrorEnum").AuthorizationError
        return ErrorCode(authorization_error=value.USER_PERMISSION_DENIED)
    if kind == "authentication_error":
        value = _enum("authentication_error", "AuthenticationErrorEnum").AuthenticationError
        return ErrorCode(authentication_error=value.AUTHENTICATION_ERROR)
    if kind == "quota_error":
        value = _enum("quota_error", "QuotaErrorEnum").QuotaError
        return ErrorCode(quota_error=value.RESOURCE_EXHAUSTED)
    if kind == "request_error":
        value = _enum("request_error", "RequestErrorEnum").RequestError
        return ErrorCode(request_error=value.INVALID_CUSTOMER_ID)
    if kind == "internal_error":
        value = _enum("internal_error", "InternalErrorEnum").InternalError
        return ErrorCode(internal_error=value.INTERNAL_ERROR)
    return ErrorCode()  # oneof 未設定


def _real_exception(*kinds: str) -> GoogleAdsException:
    """実 ``GoogleAdsException`` (failure は実 ``GoogleAdsFailure``)。"""

    failure = _ERRORS.GoogleAdsFailure(
        errors=[
            _ERRORS.GoogleAdsError(
                error_code=_error_code(kind),
                message=f"raw sdk message for customer {_CUSTOMER_ID}",
            )
            for kind in kinds
        ]
    )
    return GoogleAdsException(RuntimeError("rpc"), None, failure, _REQUEST_ID)


# ======================================================== 1) real ErrorCode classification
def test_fixture_is_the_real_proto_plus_shape_not_a_fake() -> None:
    exc = _real_exception("authorization_error")
    code = exc.failure.errors[0].error_code
    assert not hasattr(code, "WhichOneof")  # 本番と同じ: proto-plus は WhichOneof を持たない
    assert type(code).__module__.startswith(f"google.ads.googleads.{_V}")
    assert type(code).pb(code).WhichOneof("error_code") == "authorization_error"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("authorization_error", "authz"),  # live canary で見つかった USER_PERMISSION_DENIED
        ("authentication_error", "auth"),
        ("quota_error", "quota"),
        ("request_error", "request"),
        ("internal_error", "api"),  # 未対応の oneof は generic
        ("empty", "api"),  # oneof 未設定
    ],
)
def test_real_google_ads_exception_is_classified_from_its_error_code(kind, expected) -> None:
    assert classify_failure(_real_exception(kind)) == expected


def test_real_exception_first_mapped_error_wins_and_raw_protobuf_is_supported() -> None:
    assert classify_failure(_real_exception("internal_error", "authorization_error")) == "authz"

    # 生の protobuf message (WhichOneof を直接持つ) も従来どおり分類できる
    ErrorCode = _ERRORS.ErrorCode
    raw = ErrorCode.pb(_error_code("quota_error"))
    assert hasattr(raw, "WhichOneof")

    class _Failure:
        errors = [type("E", (), {"error_code": raw})()]

    class _Exc(Exception):
        failure = _Failure()

    assert classify_failure(_Exc()) == "quota"


@pytest.mark.parametrize(
    ("kind", "message", "error_type"),
    [
        ("authorization_error", MSG_AUTHZ, GoogleAdsAuthError),
        ("authentication_error", MSG_AUTH, GoogleAdsAuthError),
        ("quota_error", MSG_QUOTA, GoogleAdsQuotaError),
        ("request_error", MSG_REQUEST, Exception),
        ("internal_error", MSG_API, Exception),
    ],
)
def test_provider_maps_real_sdk_exceptions_to_safe_messages(kind, message, error_type) -> None:
    client = FakeIdeasClient(error=_real_exception(kind))
    provider = GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings(), client=client)
    with pytest.raises(error_type) as exc:
        provider.generate_keyword_ideas(["crm"])
    text = str(exc.value)
    assert message in text
    for secret in (_CUSTOMER_ID, _REQUEST_ID, "raw sdk message", *_IDENTIFIERS[5:]):
        assert secret not in text
    assert client.calls == 1 and provider.request_count == 1


def test_canary_authorization_failure_is_no_longer_reported_as_a_generic_error() -> None:
    """live canary の再現: USER_PERMISSION_DENIED は generic ではなく認可エラーになる。"""

    client = FakeIdeasClient(error=_real_exception("authorization_error"))
    provider = GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings(), client=client)
    with pytest.raises(GoogleAdsAuthError) as exc:
        provider.generate_keyword_ideas(["業務効率化 ツール"])
    assert MSG_AUTHZ in str(exc.value) and MSG_API not in str(exc.value)


# ================================================================ 2) SDK log suppression
@pytest.fixture(autouse=True)
def _clean_sdk_logger_state():
    """SDK logger を既知の状態 (filter なし・有効) にして、テスト終了後に元へ戻す。

    ``disabled`` も明示的に戻す: 他のテスト (Alembic の ``logging.config.fileConfig`` は既存
    logger を disabled にする) の副作用で、順序次第で record が出なくなるのを防ぐ。
    """

    loggers = [logging.getLogger(name) for name in SDK_LOGGER_NAMES]
    saved = [(list(lg.filters), lg.disabled) for lg in loggers]
    for lg in loggers:
        lg.filters = [f for f in lg.filters if not isinstance(f, _DropAllRecords)]
        lg.disabled = False
    try:
        yield
    finally:
        for lg, (filters, disabled) in zip(loggers, saved, strict=True):
            lg.filters = filters
            lg.disabled = disabled


@pytest.fixture
def enabled_loggers():
    """名前付き logger (と root) を、他テストの ``disable_existing_loggers`` に依らず有効にする。"""

    names = ("app.keyword.something", "google.auth", "google.ads.googleads", "")
    loggers = [logging.getLogger(n) for n in names]
    saved = [lg.disabled for lg in loggers]
    manager_disable = logging.root.manager.disable
    for lg in loggers:
        lg.disabled = False
    logging.disable(logging.NOTSET)
    try:
        yield
    finally:
        logging.disable(manager_disable)
        for lg, disabled in zip(loggers, saved, strict=True):
            lg.disabled = disabled


@pytest.fixture
def stderr_root_handler():
    """logging が未設定の環境 (lastResort 相当) を模し、record を stderr へ流す。"""

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _emit_real_sdk_failure_log(exc: GoogleAdsException | None = None) -> None:
    """SDK 自身の ``LoggingInterceptor.log_failed_request`` で実際の警告行を出す。"""

    logger = logging.getLogger("google.ads.googleads.client")
    interceptor = LoggingInterceptor(logger, _V, "googleads.googleapis.com")
    failure = exc or _real_exception("authorization_error")
    interceptor._get_error_from_response = lambda _response: failure  # grpc 応答の代わり
    interceptor.log_failed_request(
        "/google.ads.googleads.v25.services.KeywordPlanIdeaService/GenerateKeywordIdeas",
        _CUSTOMER_ID,
        "{}",
        _REQUEST_ID,
        "request",
        "{}",
        object(),
    )


def _leaked(text: str) -> list[str]:
    return [i for i in _IDENTIFIERS if i in text]


def test_control_the_real_sdk_failure_line_leaks_identifiers_when_not_suppressed(
    capsys, stderr_root_handler
) -> None:
    _emit_real_sdk_failure_log()
    err = capsys.readouterr().err
    assert (
        _CUSTOMER_ID in err and _REQUEST_ID in err
    )  # ← 実際に漏れていた挙動 (テストの有効性の証明)


def test_suppression_drops_the_real_sdk_failure_line_everywhere(
    caplog, capsys, stderr_root_handler
) -> None:
    suppress_google_ads_sdk_logging()
    with caplog.at_level(logging.DEBUG):
        _emit_real_sdk_failure_log()
    captured = capsys.readouterr()
    assert _leaked(captured.out) == [] and _leaked(captured.err) == []
    assert _leaked(caplog.text) == [] and caplog.records == []


def test_suppression_is_idempotent() -> None:
    for _ in range(3):
        suppress_google_ads_sdk_logging()
    for name in SDK_LOGGER_NAMES:
        drops = [f for f in logging.getLogger(name).filters if isinstance(f, _DropAllRecords)]
        assert len(drops) == 1


def test_suppression_does_not_touch_unrelated_logging(caplog, enabled_loggers) -> None:
    suppress_google_ads_sdk_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("app.keyword.something").warning("application warning stays")
        logging.getLogger().warning("root warning stays")
        logging.getLogger("google.auth").warning("other library stays")
        logging.getLogger("google.ads.googleads").warning("parent logger stays")
    messages = [r.getMessage() for r in caplog.records]
    for expected in (
        "application warning stays",
        "root warning stays",
        "other library stays",
        "parent logger stays",
    ):
        assert expected in messages
    assert logging.root.manager.disable == 0  # logging.disable() は使っていない
    assert not logging.getLogger("app.keyword.something").filters


def test_logger_names_match_the_real_sdk_loggers() -> None:
    """SDK が logger 名を変えたら、この test が落ちて抑止が無効になったことに気づける。"""

    import google.ads.googleads.config as sdk_config

    assert sdk_client._logger.name in SDK_LOGGER_NAMES
    assert sdk_config._logger.name in SDK_LOGGER_NAMES
    assert "Request made: ClientCustomerId" in LoggingInterceptor._SUMMARY_LOG_LINE


class _StubSdkClient:
    """``GoogleAdsClient.load_from_dict`` の代わり。SDK ロガーに触れる前に呼ばれる。"""

    calls = 0

    @classmethod
    def load_from_dict(cls, config):
        cls.calls += 1
        return object()


def test_building_a_client_installs_the_suppression_for_both_providers(monkeypatch) -> None:
    monkeypatch.setattr(sdk_client, "GoogleAdsClient", _StubSdkClient)
    assert not any(logging.getLogger(n).filters for n in SDK_LOGGER_NAMES)  # 前提: 未設定

    settings = dummy_google_ads_settings()
    metrics_mod.GoogleAdsKeywordMetricsProvider(settings)._build_client()
    assert all(
        any(isinstance(f, _DropAllRecords) for f in logging.getLogger(n).filters)
        for n in SDK_LOGGER_NAMES
    )


def test_ideas_provider_client_build_installs_the_suppression(monkeypatch) -> None:
    monkeypatch.setattr(sdk_client, "GoogleAdsClient", _StubSdkClient)
    provider = GoogleAdsKeywordIdeaProvider(dummy_google_ads_settings())
    provider._acquire_client()
    assert all(
        any(isinstance(f, _DropAllRecords) for f in logging.getLogger(n).filters)
        for n in SDK_LOGGER_NAMES
    )


def test_unconfigured_settings_do_not_touch_sdk_logging() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        metrics_mod.build_google_ads_client(unconfigured_settings())
    assert not any(logging.getLogger(n).filters for n in SDK_LOGGER_NAMES)


# ============================================ end-to-end through the real CLI + real client build
class _FailingClient(FakeIdeasClient):
    """SDK と同じ順序で動く fake: SDK の実ログ行を出してから実 GoogleAdsException を送出する。"""

    def generate_keyword_ideas(self, *, request):
        self.calls += 1
        _emit_real_sdk_failure_log()
        raise _real_exception("authorization_error")


def test_cli_with_the_real_client_build_path_prints_no_identifiers(
    monkeypatch, capsys, caplog, stderr_root_handler
) -> None:
    client = _FailingClient(response=FakeIdeasPager([]))

    class _Stub(_StubSdkClient):
        @classmethod
        def load_from_dict(cls, config):
            return client

    monkeypatch.setattr(sdk_client, "GoogleAdsClient", _Stub)

    with caplog.at_level(logging.DEBUG):
        code = discover.run(
            execute=True, clusters=["B"], settings=dummy_google_ads_settings()
        )  # 既定の provider factory = 実 client 生成パス

    captured = capsys.readouterr()
    assert code == discover.EXIT_AUTH
    assert client.calls == 1  # request は 1 回だけ
    assert "AUTH ERROR" in captured.out and MSG_AUTHZ in captured.out  # generic ではない
    everything = captured.out + captured.err + caplog.text
    assert _leaked(everything) == []
    assert "raw sdk message" not in everything


# ======================================================================
# authorization sub-code (enum 定数名のみ) を安全な文言に添える
# ======================================================================
_AUTHZ_ENUM = _enum("authorization_error", "AuthorizationErrorEnum").AuthorizationError
_AUTHZ_NAMES = [member.name for member in _AUTHZ_ENUM]
_HOSTILE_MESSAGE = (
    f"customer {_CUSTOMER_ID} login 9998887776 token dummy-developer-token "
    f"request {_REQUEST_ID} secret dummy-client-secret"
)


def _authz_exception(name: str, message: str = _HOSTILE_MESSAGE) -> GoogleAdsException:
    """実 SDK の AuthorizationError enum 値を持つ実 ``GoogleAdsException``。"""

    code = _ERRORS.ErrorCode(authorization_error=_AUTHZ_ENUM[name])
    failure = _ERRORS.GoogleAdsFailure(
        errors=[_ERRORS.GoogleAdsError(error_code=code, message=message)]
    )
    return GoogleAdsException(RuntimeError("rpc"), None, failure, _REQUEST_ID)


def _authz_message(exc: Exception) -> str:
    provider = GoogleAdsKeywordIdeaProvider(
        dummy_google_ads_settings(), client=FakeIdeasClient(error=exc)
    )
    with pytest.raises(GoogleAdsAuthError) as caught:
        provider.generate_keyword_ideas(["業務効率化 ツール"])
    return str(caught.value)


def test_the_authorization_enum_exposes_the_documented_sub_codes() -> None:
    for name in ("USER_PERMISSION_DENIED", "DEVELOPER_TOKEN_PROHIBITED"):
        assert name in _AUTHZ_NAMES
    assert len(_AUTHZ_NAMES) > 5  # 全 enum 値を下のテストで網羅する


@pytest.mark.parametrize("name", _AUTHZ_NAMES)
def test_every_real_authorization_sub_code_is_reported_as_its_fixed_constant(name: str) -> None:
    exc = _authz_exception(name)
    assert authorization_subcode(exc) == name
    # メッセージは固定文言 + enum 定数名だけ。SDK の message / ID / token / request id は含まない。
    assert _authz_message(exc) == f"google_ads: {MSG_AUTHZ} ({name})"


@pytest.mark.parametrize(
    "name",
    [
        n
        for n in (
            "USER_PERMISSION_DENIED",
            "DEVELOPER_TOKEN_NOT_APPROVED",
            "DEVELOPER_TOKEN_PROHIBITED",
            "CUSTOMER_NOT_ENABLED",
        )
        if n in _AUTHZ_NAMES
    ],
)
def test_named_authorization_sub_codes_from_the_canary_diagnosis(name: str) -> None:
    text = _authz_message(_authz_exception(name))
    assert text.endswith(f"({name})")
    for secret in (*_IDENTIFIERS, "9998887776", "raw sdk", "customer ", "login "):
        assert secret not in text


def test_sub_code_output_never_contains_sdk_text_ids_or_credentials() -> None:
    text = _authz_message(_authz_exception("USER_PERMISSION_DENIED"))
    assert text == f"google_ads: {MSG_AUTHZ} (USER_PERMISSION_DENIED)"
    assert "9998887776" not in text and _leaked(text) == []


def test_raw_protobuf_error_code_also_yields_the_sub_code() -> None:
    raw = _ERRORS.ErrorCode.pb(
        _ERRORS.ErrorCode(authorization_error=_AUTHZ_ENUM.USER_PERMISSION_DENIED)
    )
    assert hasattr(raw, "WhichOneof")

    class _Failure:
        errors = [type("E", (), {"error_code": raw})()]

    class _Exc(Exception):
        failure = _Failure()

    assert authorization_subcode(_Exc()) == "USER_PERMISSION_DENIED"


def test_unknown_enum_value_adds_no_sub_code() -> None:
    raw = _ERRORS.ErrorCode.pb(_ERRORS.ErrorCode(authorization_error=_AUTHZ_ENUM.UNKNOWN))
    raw.authorization_error = 987654  # SDK より新しい (未知の) 値
    assert raw.WhichOneof("error_code") == "authorization_error"

    class _Failure:
        errors = [type("E", (), {"error_code": raw})()]

    class _Exc(Exception):
        failure = _Failure()

    assert authorization_subcode(_Exc()) is None
    assert _authz_message(_Exc()) == f"google_ads: {MSG_AUTHZ}"


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("authentication_error", MSG_AUTH),
        ("quota_error", MSG_QUOTA),
        ("request_error", MSG_REQUEST),
    ],
)
def test_only_authorization_errors_get_a_sub_code(kind: str, message: str) -> None:
    provider = GoogleAdsKeywordIdeaProvider(
        dummy_google_ads_settings(), client=FakeIdeasClient(error=_real_exception(kind))
    )
    with pytest.raises(ExternalProviderError) as caught:
        provider.generate_keyword_ideas(["crm"])
    assert str(caught.value) == f"google_ads: {message}"  # 固定文言そのまま (sub-code なし)
    assert authorization_subcode(_real_exception(kind)) is None


def test_grpc_permission_denied_without_an_authorization_error_has_no_sub_code() -> None:
    error = FakeGrpcError("PERMISSION_DENIED", "customer 1234567890")
    assert authorization_subcode(error) is None
    assert _authz_message(error) == f"google_ads: {MSG_AUTHZ}"


def test_enum_name_helper_only_accepts_fixed_constant_names() -> None:
    def _fake_code(name: str):
        enum_value = SimpleNamespace(name=name)
        field = SimpleNamespace(enum_type=SimpleNamespace(values_by_number={1: enum_value}))
        return SimpleNamespace(
            DESCRIPTOR=SimpleNamespace(fields_by_name={"authorization_error": field}),
            authorization_error=1,
            WhichOneof=lambda _name: "authorization_error",
        )

    assert _authorization_enum_name(_fake_code("SOME_FIXED_CONSTANT")) == "SOME_FIXED_CONSTANT"
    for bad in ("customer 1234567890", "user_permission_denied", "1BAD", "A" * 65, "X\nY", ""):
        assert _authorization_enum_name(_fake_code(bad)) is None
    assert _authorization_enum_name(None) is None
    assert _authorization_enum_name(SimpleNamespace(authorization_error=1)) is None


def test_cli_prints_the_sub_code_and_still_leaks_no_identifiers(
    monkeypatch, capsys, caplog, stderr_root_handler
) -> None:
    class _Denied(_FailingClient):
        def generate_keyword_ideas(self, *, request):
            self.calls += 1
            _emit_real_sdk_failure_log(_authz_exception("USER_PERMISSION_DENIED"))
            raise _authz_exception("USER_PERMISSION_DENIED")

    client = _Denied(response=FakeIdeasPager([]))

    class _Stub(_StubSdkClient):
        @classmethod
        def load_from_dict(cls, config):
            return client

    monkeypatch.setattr(sdk_client, "GoogleAdsClient", _Stub)
    with caplog.at_level(logging.DEBUG):
        code = discover.run(execute=True, clusters=["B"], settings=dummy_google_ads_settings())

    captured = capsys.readouterr()
    assert code == discover.EXIT_AUTH and client.calls == 1
    assert f"AUTH ERROR: google_ads: {MSG_AUTHZ} (USER_PERMISSION_DENIED)" in captured.out
    everything = captured.out + captured.err + caplog.text
    assert _leaked(everything) == [] and "9998887776" not in everything
