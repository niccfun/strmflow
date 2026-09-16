import socket
from base64 import b64encode

import httpx
from fastapi.testclient import TestClient

from strmflow import __version__
from strmflow.core.config import Settings
from strmflow.main import create_app


def test_login_cookie_and_health(tmp_path) -> None:
    app = create_app(
        Settings(
            app_user="admin",
            app_password="secret",
            session_secret="test-session-key",
            openlist_url="http://127.0.0.1:5244",
            openlist_token="token",
            openlist_web_url="https://openlist.example.test",
            emby_url="http://127.0.0.1:8096",
            emby_web_url="https://emby.example.test",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
            legacy_json_import=False,
        )
    )
    with TestClient(app) as client:
        login_page = client.get("/login")
        assert login_page.status_code == 200
        assert "<title>登录 - StrmFlow</title>" in login_page.text
        assert "<h1>StrmFlow</h1>" in login_page.text
        redirect = client.get("/", follow_redirects=False)
        assert redirect.status_code == 302
        unauthorized = client.get("/api/health")
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {"ok": False, "error": "需要登录"}

        response = client.post("/api/login", json={"username": "admin", "password": "secret"})
        assert response.status_code == 200
        assert response.json()["data"]["loggedIn"] is True
        page = client.get("/")
        assert page.status_code == 200
        assert "<title>StrmFlow</title>" in page.text
        assert "<h1>StrmFlow</h1>" in page.text
        assert f'<span class="version-pill">v{__version__}</span>' in page.text
        assert "__STRMFLOW_VERSION__" not in page.text
        assert 'id="settingsButton"' in page.text
        assert 'id="editorError"' in page.text
        icon = client.get("/static/strmflow.svg")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")
        assert '<title id="title">StrmFlow</title>' in icon.text
        assert client.get("/api/health").json() == {"ok": True, "data": {"ok": True}}
        public_config = client.get("/api/config").json()["data"]
        assert public_config["openListInternalUrl"] == "http://127.0.0.1:5244"
        assert public_config["openListExternalUrl"] == "https://openlist.example.test"
        assert public_config["embyExternalUrl"] == "https://emby.example.test"
        items = client.get("/api/items")
        assert items.status_code == 200
        assert items.json()["data"] == {"items": [], "count": 0}
        paths = client.put(
            "/api/settings/paths",
            json={"listRoot": "/strm/tv", "embyStrmRoot": "/emby/strm"},
        )
        assert paths.status_code == 200
        assert paths.json()["data"] == {
            "listRoot": "/strm/tv",
            "embyStrmRoot": "/emby/strm",
            "updatedItems": 0,
        }
        overlapping_paths = client.put(
            "/api/settings/paths",
            json={"listRoot": "/strm/tv", "embyStrmRoot": "/strm/tv/output"},
        )
        assert overlapping_paths.status_code == 409
        assert "目录重叠" in overlapping_paths.json()["error"]
        media = {
            "sourcePath": "/strm/tv/国产剧/示例剧 (2026)",
            "title": "示例剧",
            "year": "2026",
            "category": "国产剧",
            "mediaType": "tv",
            "season": 1,
        }
        first_save = client.post("/api/items/save", json=media)
        assert first_save.status_code == 200
        duplicate_save = client.post("/api/items/save", json=media)
        assert duplicate_save.status_code == 409
        assert duplicate_save.json()["error"] == "该媒体已添加：示例剧 (2026)"
        assert client.get("/api/items").json()["data"]["count"] == 1

        gateway = client.get("/api/emby302")
        assert gateway.status_code == 200
        assert gateway.json()["data"]["config"]["port"] == 18096
        gateway_update = client.put(
            "/api/emby302",
            json={
                "enabled": False,
                "embyUrl": "http://emby:8096",
                "openlistUrl": "http://openlist:5244",
                "host": "127.0.0.1",
                "port": 18097,
                "cacheTtl": 300,
                "cacheMax": 500,
                "bodyBufferMax": 2_097_152,
                "timeoutMs": 20_000,
            },
        )
        assert gateway_update.status_code == 200
        assert gateway_update.json()["data"]["config"] == {
            "enabled": False,
            "embyUrl": "http://emby:8096",
            "openlistUrl": "http://openlist:5244",
            "host": "127.0.0.1",
            "port": 18097,
            "cacheTtl": 300,
            "cacheMax": 500,
            "bodyBufferMax": 2_097_152,
            "timeoutMs": 20_000,
        }
        assert client.post("/api/emby302/cache/clear").status_code == 200

        probe = client.get("/api/media-probe")
        assert probe.status_code == 200
        assert probe.json()["data"]["config"]["dailyEnabled"] is False
        probe_update = client.put(
            "/api/media-probe",
            json={"dailyEnabled": True, "scanTime": "04:35"},
        )
        assert probe_update.status_code == 200
        probe_data = probe_update.json()["data"]
        assert probe_data["config"]["scanTime"] == "04:35"
        assert probe_data["config"]["timezone"] == "Asia/Hong_Kong"
        assert probe_data["runtime"]["nextScanAt"]

        logs = client.get("/api/logs?limit=500")
        assert logs.status_code == 200
        log_data = logs.json()["data"]
        assert any(
            entry.get("path") == "/"
            and entry.get("statusCode") == 302
            and entry.get("redirectTo") == "/login"
            for entry in log_data["items"]
        )
        assert log_data["categories"]["http"] >= 1
        assert client.delete("/api/logs").json()["data"]["cleared"] >= 1
        assert client.get("/api/logs").json()["data"]["items"] == []


def test_health_accepts_basic_auth(tmp_path) -> None:
    app = create_app(
        Settings(
            app_password="secret",
            openlist_token="token",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
            legacy_json_import=False,
        )
    )
    encoded = b64encode(b"admin:secret").decode()
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"Authorization": f"Basic {encoded}"})
        assert response.status_code == 200


def test_bdpan_command_preview_is_available_while_execution_disabled(tmp_path) -> None:
    app = create_app(
        Settings(
            app_password="secret",
            openlist_token="token",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
            legacy_json_import=False,
        )
    )
    encoded = b64encode(b"admin:secret").decode()
    headers = {"Authorization": f"Basic {encoded}"}
    payload = {
        "provider": "bdpan",
        "shareUrl": "https://pan.baidu.com/s/example",
        "extractCode": "abcd",
        "destination": "/影视/待整理",
    }
    with TestClient(app) as client:
        preview = client.post("/api/transfers/preview", headers=headers, json=payload)
        assert preview.status_code == 200
        command = preview.json()["data"]["command"]
        assert command[0] == "bdpan"
        assert command[1:3] == ["transfer", "https://pan.baidu.com/s/example"]
        assert command[command.index("-d") + 1] == "影视/待整理"
        assert "--json" in command
        assert "--no-check-update" in command
        assert "--agentname" in command
        assert "abcd" not in command
        assert "[已隐藏]" in command

        create = client.post("/api/transfers", headers=headers, json=payload)
        assert create.status_code == 503


def test_bdpan_runtime_config_is_available_when_cli_is_missing(tmp_path) -> None:
    app = create_app(
        Settings(
            app_password="secret",
            openlist_token="token",
            bdpan_binary="/definitely/missing/bdpan",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
            legacy_json_import=False,
        )
    )
    encoded = b64encode(b"admin:secret").decode()
    headers = {"Authorization": f"Basic {encoded}"}
    with TestClient(app) as client:
        status = client.get("/api/bdpan", headers=headers)
        assert status.status_code == 200
        assert status.json()["data"]["runtime"]["available"] is False

        updated = client.put(
            "/api/bdpan",
            headers=headers,
            json={
                "enabled": False,
                "binary": "/definitely/missing/bdpan",
                "checkIntervalMinutes": 15,
                "saveRoot": "StrmFlow",
                "settleSeconds": 120,
                "maxNewItems": 10,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["config"]["checkIntervalMinutes"] == 15

        disclaimer = client.post(
            "/api/bdpan/login/start",
            headers=headers,
            json={"accepted": False},
        )
        assert disclaimer.status_code == 400

        secret_like_code = "a" * 31
        invalid = client.post(
            "/api/bdpan/login/complete",
            headers=headers,
            json={"code": secret_like_code},
        )
        assert invalid.status_code == 422
        assert secret_like_code not in invalid.text

        webhook = (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
            "?key=12345678-1234-1234-1234-123456789abc"
        )
        notification = client.put(
            "/api/notifications/wecom",
            headers=headers,
            json={
                "webhookUrl": webhook,
                "episodeUpdateEnabled": True,
                "linkInvalidEnabled": True,
            },
        )
        assert notification.status_code == 200
        notification_config = notification.json()["data"]["config"]
        assert notification_config["webhookConfigured"] is True
        assert notification_config["episodeUpdateEnabled"] is True
        assert webhook not in notification.text
        assert client.get("/api/notifications/wecom", headers=headers).status_code == 200


def test_emby302_gateway_can_be_started_and_stopped(tmp_path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        gateway_port = probe.getsockname()[1]

    app = create_app(
        Settings(
            app_password="secret",
            openlist_token="token",
            emby_url="http://127.0.0.1:8096",
            emby_api_key="emby-key",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
            legacy_json_import=False,
        )
    )
    encoded = b64encode(b"admin:secret").decode()
    headers = {"Authorization": f"Basic {encoded}"}
    config = {
        "enabled": True,
        "embyUrl": "http://127.0.0.1:8096",
        "openlistUrl": "http://127.0.0.1:5244",
        "host": "127.0.0.1",
        "port": gateway_port,
        "cacheTtl": 180,
        "cacheMax": 100,
        "bodyBufferMax": 1_048_576,
        "timeoutMs": 30_000,
    }
    with TestClient(app) as client:
        enabled = client.put("/api/emby302", headers=headers, json=config)
        assert enabled.status_code == 200
        assert enabled.json()["data"]["stats"]["running"] is True

        health = httpx.get(f"http://127.0.0.1:{gateway_port}/__strmflow302/health", timeout=3)
        assert health.status_code == 200
        assert health.json()["ok"] is True

        config["enabled"] = False
        disabled = client.put("/api/emby302", headers=headers, json=config)
        assert disabled.status_code == 200
        assert disabled.json()["data"]["stats"]["running"] is False
