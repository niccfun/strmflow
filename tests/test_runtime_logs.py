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
