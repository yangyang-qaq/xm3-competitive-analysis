"""应用配置。

设计要点
--------
**provider 凭据不写成字段，而是按命名约定动态查。**

    <PROVIDER>_API_KEY
    <PROVIDER>_BASE_URL
    <PROVIDER>_MODEL_{CORE,AUX,FAST}

也就是说，接入一家新 provider 只需要写一个适配器类 + 在 .env 里加几行，
`config.py`、流水线、API 层都不用动。若把 DEEPSEEK_API_KEY / ZHIPU_API_KEY
逐个写成字段，每接一家就要回来改一次这个文件，"可插拔"就只剩一半。

优先级：进程环境变量 > backend/.env > 代码里的默认值。
部署时用环境变量覆盖 .env 是常规做法，load_dotenv(override=False) 保证这一点。
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# app/core/config.py -> app/core -> app -> backend
BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"

# 把 .env 灌进 os.environ，让「按约定查凭据」这条路能看见它。
# override=False：真实环境变量优先于文件。
load_dotenv(ENV_FILE, override=False)

CassetteMode = Literal["off", "record", "replay"]
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _env(name: str, default: str = "") -> str:
    """读环境变量，空字符串视同未设置。"""
    return (os.environ.get(name) or "").strip() or default


@dataclass(frozen=True)
class ProviderCredentials:
    """一家 provider 的全部凭据与模型映射。"""

    provider: str
    api_key: str = ""
    base_url: str = ""
    models: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """是否已配置到「可以尝试调用」的程度。

        只判断有没有 key；base_url 允许由适配器给默认值。
        注意这里不校验 key 是否有效——那要靠 /api/providers/health 的真实探测。
        """
        return bool(self.api_key)

    def model_for(self, tier: str, default: str = "") -> str:
        """取某个档位的模型名；未配置则回落到适配器内置的默认值。"""
        return self.models.get(tier) or default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用 ----
    app_host: str = "127.0.0.1"
    app_port: int = 8020
    frontend_origin: str = "http://localhost:3500"
    # 额外允许的跨域来源，逗号分隔。开发时前端走 vite 代理，
    # 根本不触发跨域；这里收紧到具体来源而不是 "*"，是为了部署时不留下敞口。
    cors_extra_origins: str = ""
    log_level: str = "INFO"

    # ---- provider 选择 ----
    llm_provider: str = "deepseek"
    search_provider: str = "bocha"
    fetch_provider: str = "http"
    llm_fallback: str = ""

    # ---- 压测护栏 ----
    # 置 1 时三家 provider 一律换成 mock，且**任何 `*_PROVIDER` 都覆盖不了它**。
    # 见下面 `_force_mock` 的注释：这不只是"让压测快一点"，是防真金白银。
    force_mock_provider: bool = False

    # ---- 网络与限额 ----
    llm_timeout: float = 180.0
    llm_max_retries: int = 1
    fetch_timeout: float = 20.0
    fetch_max_chars: int = 20_000

    # ---- 测试录制回放 ----
    cassette_mode: CassetteMode = "off"
    cassette_dir: str = "tests/fixtures/cassettes"

    # ---- mock provider 的行为开关（只在 provider=mock 时有意义）----
    # 每次 provider 调用后人为等待的毫秒数，默认 0（不等待）。
    #
    # 为什么需要它：mock 快到一整个 quick 任务 0.2 秒就跑完了。于是所有
    # **依赖时间**的行为都没有办法验证——浏览器断网 10 秒再恢复、SSE 心跳、
    # 压测时的并发窗口，全都来不及发生。把 mock 调慢，这些才成为可复现的
    # 现象，而且仍然零成本、零网络。
    #
    # 一次完整运行的总耗时大致是「调用次数 × 这个值」，所以演示「跑到一半
    # 断网」通常取 100~300；排查并发问题的压测则取 0，因为那时慢下来的是
    # 被测对象之外的噪声。
    mock_latency_ms: int = 0

    # ---- 存储 ----
    # 留空表示用 backend/data/xm3.db
    db_path: str = ""

    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _force_mock(self) -> Settings:
        """压测护栏：`FORCE_MOCK_PROVIDER=1` 时把三家 provider 全按到 mock。

        **为什么这不是"让压测快一点"的优化，而是一道刹车**
        ----------------------------------------------
        `loadtest/locustfile.py` 会起 20 个并发用户。那个规模打到真实
        provider 上是**真金白银**，而且测出来的 p95 反映的是对面 API 的排队，
        不是这个系统的。所以压测**必须**跑在 mock 上。

        而"必须"不能靠人记得在命令行上多敲三个变量——`.env` 里
        `LLM_PROVIDER=deepseek` 是这台机器的常态，忘了覆盖就中招。
        这个开关的作用正是**盖过 `.env`**。

        覆盖发生在校验**之后**，而不是拿三个字段的默认值做文章：
        写在默认值上的话，`.env` 一填就把它盖掉了，压不住的开关等于没有。

        `llm_fallback` 一并清空：只换主 provider 的话，备用还挂着 deepseek，
        而它**是会真的被调用的**（主 provider 出错时降级）。漏掉这一条，
        护栏就有一个自己开的洞。
        """
        if not self.force_mock_provider:
            return self
        self.llm_provider = "mock"
        self.search_provider = "mock"
        self.fetch_provider = "mock"
        self.llm_fallback = ""
        return self

    # ------------------------------------------------------------------
    def credentials(self, provider: str) -> ProviderCredentials:
        """按命名约定取某家 provider 的凭据。"""
        name = (provider or "").strip().lower()
        if not name:
            return ProviderCredentials(provider="")
        upper = name.upper()
        return ProviderCredentials(
            provider=name,
            api_key=_env(f"{upper}_API_KEY"),
            base_url=_env(f"{upper}_BASE_URL"),
            models={
                tier: _env(f"{upper}_MODEL_{tier.upper()}")
                for tier in ("core", "aux", "fast")
            },
        )

    def pricing_for(self, provider: str) -> dict[str, tuple[float, float, float | None]]:
        """读某家 provider 的定价表：`{模型名: (输入, 输出, 缓存命中输入)}`，
        单位是**美元/百万 token**。

        从 `<PROVIDER>_PRICING` 环境变量读 JSON，例如：

            DEEPSEEK_PRICING={"deepseek-chat": [0.27, 1.10]}
            DEEPSEEK_PRICING={"deepseek-chat": [0.15, 0.59, 0.003]}

        第三个数字可省略，省略即"没配缓存价"，此时命中部分按输入价计（上界），
        读出来是 `None` 而不是 0——0 是一个货真价实的单价（缓存可能不计费），
        两者在成本上差 50 倍，不能共用一个值。
        它是可选的而不是必填的，因为只有一部分 provider 区分缓存命中价；
        要求每家都写会让"接入一家新 provider"多一个无意义的必填项。

        刻意做成配置而不是写死在代码里：定价会变，而且各家按自己的货币计价，
        写死的数字过几个月就是错的，而错误的成本指标比没有成本指标更糟。
        没配就返回空表，此时成本记 0——**不估算、不猜**。
        """
        raw = _env(f"{provider.strip().upper()}_PRICING")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error("%s_PRICING 不是合法 JSON，成本将记为 0：%s", provider.upper(), exc)
            return {}
        if not isinstance(parsed, dict):
            log.error("%s_PRICING 应为对象，实际是 %s", provider.upper(), type(parsed).__name__)
            return {}

        table: dict[str, tuple[float, float, float | None]] = {}
        for model, value in parsed.items():
            if isinstance(value, (list, tuple)) and len(value) in (2, 3):
                try:
                    table[str(model)] = (
                        float(value[0]),
                        float(value[1]),
                        # 省略第三项 = 未配置缓存价（不是"缓存免费"）。
                        # 这两件事在成本上差 50 倍，所以用 None 而不是 0 区分开。
                        float(value[2]) if len(value) == 3 else None,
                    )
                except (TypeError, ValueError):
                    log.error("%s_PRICING[%s] 不是数值，已跳过", provider.upper(), model)
            else:
                log.error(
                    "%s_PRICING[%s] 应为 [输入单价, 输出单价] 或 "
                    "[输入单价, 输出单价, 缓存命中单价]，已跳过",
                    provider.upper(),
                    model,
                )
        return table

    @property
    def cors_origins(self) -> list[str]:
        origins = [self.frontend_origin.strip()] if self.frontend_origin.strip() else []
        origins += [o.strip() for o in self.cors_extra_origins.split(",") if o.strip()]
        return origins

    @property
    def cassette_path(self) -> Path:
        p = Path(self.cassette_dir)
        return p if p.is_absolute() else BACKEND_DIR / p

    @property
    def db_file(self) -> Path:
        if self.db_path:
            p = Path(self.db_path)
            return p if p.is_absolute() else BACKEND_DIR / p
        return BACKEND_DIR / "data" / "xm3.db"

    def describe(self) -> dict:
        """给 /health 用的脱敏自述。**绝不返回 key 本身**。

        三家都报。之前漏了 `fetch_provider`——补上是因为
        `loadtest/locustfile.py` 拿这份自述当**压测的前置条件**：
        它要确认三家全在 mock 上才肯开始发压。缺一家就检查不全，
        而抓取恰恰是这套系统最主要的降级来源，漏掉它等于漏掉最该防的那个。

        `force_mock_provider` 也报：它区分"这台机器上 `.env` 本来就是 mock"
        与"这次是被护栏按下来的"。前者换台机器就没了，后者是这次压测的
        前提条件，读数的人有权知道是哪种。
        """
        llm = self.credentials(self.llm_provider)
        search = self.credentials(self.search_provider)
        return {
            "llm_provider": self.llm_provider,
            "llm_configured": llm.configured,
            "llm_base_url": llm.base_url,
            "search_provider": self.search_provider,
            "search_configured": search.configured,
            "fetch_provider": self.fetch_provider,
            "llm_fallback": self.llm_fallback or None,
            "force_mock_provider": self.force_mock_provider,
            "cassette_mode": self.cassette_mode,
            "db_path": str(self.db_file),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
