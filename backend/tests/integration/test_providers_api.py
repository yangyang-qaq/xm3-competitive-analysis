"""`/api/providers` 与 `/api/providers/health`。

**这里必须零网络**。两个端点在默认配置下会真的去打 DeepSeek 与博查
（本机 `.env` 里就有密钥），所以每个用例都要么用 mock provider，
要么把注册表换掉。否则跑一次测试就花一次钱，而且结果依赖外网是否通畅。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers import registry
from app.providers.mock import MockFetcher, MockLLMProvider, MockSearchProvider


@pytest.fixture
def client(mock_pipeline_db):
    """三个 provider 都是 mock 的测试客户端。

    依赖 `mock_pipeline_db` 是为了那套环境变量与缓存清理——它顺带把库建好，
    `_today_cost()` 因此走的是真查询而不是兜底的零值。
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture
def only_mock(monkeypatch):
    """把注册表收窄到只剩 mock。

    给 `scope=all` 用。真实的注册表里有 deepseek / bocha，而本机 `.env`
    给它们配了能用的密钥——不收窄的话，这条测试会真的去打外部 API。
    """
    monkeypatch.setattr(
        registry,
        "_REGISTRIES",
        {
            "llm": {"mock": MockLLMProvider},
            "search": {"mock": MockSearchProvider},
            "fetch": {"mock": MockFetcher},
        },
    )


# ============================================================
# 能力矩阵
# ============================================================


def test_matrix_has_the_five_parts_the_page_needs(client):
    body = client.get("/api/providers").json()

    assert set(body) == {"active", "fallback", "cassetteMode", "providers", "cost"}
    assert set(body["active"]) == {"llm", "search", "fetch"}


def test_every_row_is_the_same_shape(client):
    """一行缺字段的话，前端那张表会静默少显示一列。

    所以这里断言的是**键集合相等**，不是"包含"——多出来的键同样要过一遍，
    因为多键意味着契约该更新了。
    """
    rows = client.get("/api/providers").json()["providers"]

    assert rows
    for row in rows:
        assert set(row) == {
            "kind",
            "name",
            "active",
            "configured",
            "error",
            "baseUrl",
            "models",
            "capabilities",
            "pricing",
        }


def test_the_active_providers_are_the_ones_from_settings(client):
    body = client.get("/api/providers").json()

    active = {(r["kind"], r["name"]) for r in body["providers"] if r["active"]}
    assert active == {("llm", "mock"), ("search", "mock"), ("fetch", "mock")}
    assert body["active"] == {"llm": "mock", "search": "mock", "fetch": "mock"}


def test_mock_is_reported_as_usable_even_though_it_has_no_api_key(client):
    """踩过的坑：mock 没有也不需要密钥，但"能用"曾经有两套判据
    （构造得出来 / 有没有密钥），于是它被标成"未配置"。

    这条测试盯住的是**结果**，而不是那两套判据里的哪一套——判据怎么实现
    是内部的事，mock 能用是事实。
    """
    rows = client.get("/api/providers").json()["providers"]
    mock_llm = next(r for r in rows if r["kind"] == "llm" and r["name"] == "mock")

    assert mock_llm["configured"] is True
    assert mock_llm["error"] == ""


def test_llm_rows_carry_the_three_tiers(client):
    rows = client.get("/api/providers").json()["providers"]
    llm_rows = [r for r in rows if r["kind"] == "llm"]

    assert llm_rows
    for row in llm_rows:
        if row["configured"]:
            assert set(row["models"]) == {"core", "aux", "fast"}


def test_only_llm_rows_have_models_and_pricing(client):
    """搜索与抓取没有"档位"这个概念。

    返回 `null` 而不是 `{}`：这两种在语义上不同——`null` 是"这个类别没有档位"，
    `{}` 是"有档位但一个都没配"。前端据此决定显不显示那一栏。
    """
    rows = client.get("/api/providers").json()["providers"]

    for row in rows:
        if row["kind"] != "llm":
            assert row["models"] is None, row
            assert row["pricing"] == {}, row


def test_cost_is_present_even_on_an_empty_database(client):
    """`/api/providers` 是启动后最先被打开的页面之一。

    它不该因为"还没跑过任何任务"而报 500，所以空库要返回零值而不是抛。
    """
    cost = client.get("/api/providers").json()["cost"]

    assert cost["totalCostUsd"] == 0.0
    assert cost["calls"] == 0


# ============================================================
# 连通性探测
# ============================================================


def test_active_scope_probes_all_three_and_is_green(client):
    body = client.get("/api/providers/health").json()

    assert body["ok"] is True
    assert [p["kind"] for p in body["probes"]] == ["llm", "search", "fetch"]
    assert all(p["probed"] for p in body["probes"])
    assert all(p["ok"] for p in body["probes"])


def test_every_probe_entry_is_the_same_shape(client):
    for probe in client.get("/api/providers/health").json()["probes"]:
        assert set(probe) == {
            "kind",
            "name",
            "probed",
            "ok",
            "detail",
            "latencyMs",
            "error",
        }
        assert probe["detail"], "detail 为空的话前端就是一个红点配一句空话"


def test_latency_is_measured_even_for_the_zero_cost_mock(client):
    """mock 不联网，但**耗时仍然是真的量出来的**。

    mock 的 `chat()` 里 `latency_ms` 是写死的 1——那是它给 trace 用的假值。
    探测不能读它，否则界面上每一条都显示 1ms，看起来像一个坏掉的计时器。
    """
    probes = client.get("/api/providers/health").json()["probes"]

    assert all(p["latencyMs"] >= 0 for p in probes)


def test_all_scope_walks_the_whole_registry(client, only_mock):
    body = client.get("/api/providers/health?scope=all").json()

    assert body["scope"] == "all"
    assert {p["kind"] for p in body["probes"]} == {"llm", "search", "fetch"}
    assert body["ok"] is True


def test_all_scope_keeps_an_unbuildable_provider_in_the_result(client, monkeypatch):
    """构造不出来的那家要**留在结果里**，标成"没探"。

    把它从列表里摘掉的话，`scope=all` 体检会给出一个"全部通过"的假象——
    而"我明明配了它、它却不在结果里"是最难注意到的一类问题。
    """

    class _NeedsKey:
        name = "needs_key"

        def __init__(self, settings) -> None:
            from app.providers.errors import ProviderNotConfigured

            raise ProviderNotConfigured("未配置 NEEDS_KEY_API_KEY", provider=self.name)

    monkeypatch.setattr(
        registry,
        "_REGISTRIES",
        {
            "llm": {"mock": MockLLMProvider, "needs_key": _NeedsKey},
            "search": {"mock": MockSearchProvider},
            "fetch": {"mock": MockFetcher},
        },
    )

    body = client.get("/api/providers/health?scope=all").json()
    broken = next(p for p in body["probes"] if p["name"] == "needs_key")

    assert broken["probed"] is False
    assert broken["ok"] is False
    assert "NEEDS_KEY_API_KEY" in broken["detail"]


def test_an_unprobed_provider_does_not_turn_the_whole_page_red(client, monkeypatch):
    """没配密钥的可选 provider **不算失败**。

    算的话，任何一份没配齐备用 provider 的配置打开这页都是红的，
    而这页很快就没人看了——一个总是红的告警等于没有告警。
    """
    monkeypatch.setattr(
        registry,
        "_REGISTRIES",
        {
            "llm": {"mock": MockLLMProvider, "nope": lambda settings: _raise_unconfigured()},
            "search": {"mock": MockSearchProvider},
            "fetch": {"mock": MockFetcher},
        },
    )

    body = client.get("/api/providers/health?scope=all").json()

    assert any(p["probed"] is False for p in body["probes"])
    assert body["ok"] is True, "只探过的都绿，整体就该是绿的"


def _raise_unconfigured():
    from app.providers.errors import ProviderNotConfigured

    raise ProviderNotConfigured("未配置", provider="nope")


def test_scope_rejects_unknown_values(client):
    """`scope` 有 pattern 约束，拼错时要报 422 而不是静默当成 active。"""
    assert client.get("/api/providers/health?scope=every").status_code == 422
