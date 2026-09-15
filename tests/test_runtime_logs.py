import logging

from strmflow.core.runtime_logs import RuntimeLogStore, request_category, status_level


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
