"""接口层测试。

最重要的一条是**密钥隔离**：下发到浏览器的只有 JS Key，
带着服务端配额的高德 Web 服务 Key（REST Key）绝不能出现在响应里。
"""

from __future__ import annotations

REST_KEY = "REST_KEY_MUST_NEVER_LEAK_9f3a"
JS_KEY = "JS_KEY_IS_PUBLIC_BY_DESIGN_7c21"


def test_health_reports_ok(api_client) -> None:
    response = api_client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_version"] >= 1
    assert body["engine"]["available"] is True
    assert body["engine"]["upstream_commit"] == "ec911f7"
    assert body["database"]["exists"] is True


def test_health_reports_missing_keys_as_warnings(api_client, monkeypatch) -> None:
    """缺 Key 只警告，不让健康检查失败——前端仍应能打开并给出可读提示。"""
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")

    body = api_client.get("/api/health").json()
    assert body["status"] == "ok"
    assert any("AMAP_API_KEY" in w for w in body["warnings"])
    assert any("DEEPSEEK_API_KEY" in w for w in body["warnings"])


def test_config_hands_out_the_js_key(api_client, monkeypatch) -> None:
    monkeypatch.setenv("AMAP_JS_KEY", JS_KEY)
    body = api_client.get("/api/config").json()
    assert body["amap_js_key"] == JS_KEY


def test_config_never_hands_out_the_rest_key(api_client, monkeypatch) -> None:
    """核心断言：REST Key 不出现在响应里，连字段名都不该有。"""
    monkeypatch.setenv("AMAP_API_KEY", REST_KEY)
    monkeypatch.setenv("AMAP_JS_KEY", JS_KEY)

    response = api_client.get("/api/config")
    assert REST_KEY not in response.text

    body = response.json()
    assert set(body) == {"amap_js_key", "amap_js_security_code", "missing_keys"}
    assert not any("api_key" == key for key in body)


def test_root_gives_a_readable_hint_when_frontend_is_not_built(api_client, monkeypatch, tmp_path) -> None:
    """M0 阶段前端还没构建，访问根路径不应是 404。"""
    from lushu import config

    monkeypatch.setattr(config, "WEB_DIST_DIR", tmp_path / "不存在的前端产物")

    from fastapi.testclient import TestClient

    from lushu.app import create_app

    with TestClient(create_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "尚未构建" in response.json()["message"]
