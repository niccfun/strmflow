import logging

from strmflow.core.runtime_logs import (
    RuntimeLogStore,
    redact_sensitive_text,
    request_category,
    status_level,
)


def test_runtime_log_store_filters_and_honors_capacity() -> None:
    store = RuntimeLogStore(capacity=2)
    store.add(category="http", message="GET /", statusCode=302)
    store.add(category="scan", message="POST /api/items/scan/start", statusCode=200)
    latest = store.add(category="sync", message="POST /api/emby/publish", statusCode=502)

    assert [entry["category"] for entry in store.list()] == ["sync", "scan"]
    assert store.list(category="sync") == [latest]
    assert store.categories() == {"scan": 1, "sync": 1}
    assert store.clear() == 2
    assert store.list() == []


def test_request_log_classification_and_status_levels() -> None:
    assert request_category("/") == "http"
    assert request_category("/api/login") == "auth"
    assert request_category("/api/items/scan/start") == "scan"
    assert request_category("/api/emby/publish") == "sync"
    assert request_category("/api/telegram") == "telegram"
    assert status_level(200) == "success"
    assert status_level(302) == "redirect"
    assert status_level(404) == "warning"
    assert status_level(502) == "error"


def test_application_events_are_mirrored_to_console_without_duplicate_access_logs(
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    store = RuntimeLogStore()

    store.add(category="bdpan", message="开始检查百度网盘分享：测试剧", fileCount=12)
    store.add(
        category="http",
        message="GET /api/items",
        method="GET",
        statusCode=200,
    )

    assert "[bdpan] 开始检查百度网盘分享：测试剧" in caplog.text
    assert '"fileCount":12' in caplog.text
    assert "GET /api/items" not in caplog.text


def test_raw_log_stream_is_bounded_and_keeps_original_text() -> None:
    store = RuntimeLogStore(capacity=2)
    store.capture_raw("first")
    store.capture_raw("second")
    store.capture_raw("third")

    assert [line["text"] for line in store.raw_lines(limit=10)] == ["second", "third"]


def test_runtime_logs_redact_secrets_in_raw_and_structured_values() -> None:
    store = RuntimeLogStore()
    store.capture_raw(
        "Authorization: Bearer top-secret\n"
        "GET /play?access_token=token-value&pwd=6666 HTTP/1.1 "
        'body={"api_hash":"hash-value"} upstream=http://admin:url-secret@emby:8096'
    )
    entry = store.add(
        category="security",
        message="request https://example.test/hook?key=webhook-secret",
        password="password-value",
        nested={"embyApiKey": "emby-secret", "safe": "visible"},
        command="bdpan transfer -p 6666 --session-id session-value",
    )

    raw = store.raw_lines()[0]["text"]
    combined = raw + str(entry)
    for secret in (
        "top-secret",
        "token-value",
        "6666",
        "hash-value",
        "webhook-secret",
        "password-value",
        "emby-secret",
        "session-value",
        "url-secret",
    ):
        assert secret not in combined
    assert entry["nested"]["safe"] == "visible"
    assert "[已隐藏]" in raw


def test_redact_sensitive_text_keeps_non_secret_status_fields() -> None:
    value = redact_sensitive_text('statusCode=200 payload={"code":"13001"}')

    assert value == 'statusCode=200 payload={"code":"13001"}'
