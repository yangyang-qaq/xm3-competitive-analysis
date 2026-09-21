"""Provider 错误分类。

为什么不用字符串匹配
--------------------
"是不是限流"这个判断，参考实现是靠 `"429" in str(err) or "rate" in msg.lower()`。
问题不在于它不准，而在于**它把重试决策建立在一段可能变动的文本上**：
provider 改一句错误描述、或者底层 SDK 换个措辞，退避重试就悄悄失效了，
而失效的表现是"偶发失败"，没人会去查。

更实际的问题是：有些 provider 返回 HTTP 200，业务错误码在响应体里。
同一件事（限流）在不同 provider 那里是 `429` / `1302` / `code: 42900`……
把"识别"和"应对"拆开后，各适配器只需负责前者，后者共用一套策略。

所以：
  - **识别**由适配器负责（它才知道自己的错误码长什么样）
  - **应对**（退避重试）由 `retry.py` 统一实现

`retryable` 是这套分类的核心字段：只有它明确为 True 的错误才会被重试。
"""
from __future__ import annotations

import httpx


class ProviderError(Exception):
    """所有 provider 错误的基类。

    非本类的异常（KeyError、AttributeError 之类）一律视为代码缺陷，直接抛出、不重试——
    重试一个 bug 只会让 bug 晚几分钟出现。
    """

    retryable: bool = False

    def __init__(self, message: str, *, provider: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 展示用
        prefix = f"[{self.provider}] " if self.provider else ""
        return f"{prefix}{super().__str__()}"


def should_degrade(exc: BaseException, *, required: bool = False) -> bool:
    """**唯一**决定"这次失败该不该降级继续"的地方。

    两个条件都满足才降级：调用方说它是可选的，且失败确实是运行时故障。

    为什么把判断收成一个函数，而不是在每个 `except ProviderError` 里各写一遍：
    各写一遍时，新加一处调用很容易漏掉其中一半条件，而漏掉的表现是
    "这份报告有一部分建立在一个残缺的输入上，却长得完全正常"。
    这和 `charts.build_charts` 出口那道收口是同一个理由——
    **一个口子比 N 处各判一次更不容易漏**。

    实测踩到过（这条是写它的原因）：改了专家调度的提示词之后，那条
    dispatch 录制因为 key 变化而失效。那处调用是 `required=False`，
    于是 `CassetteMiss` 被吞掉、返回空 payload、`validate_team({})`
    把三个角色**整类退回默认队伍**——报告照常产出，降级横幅上只有一句
    "整类退回默认队伍"，而闸门报"12 项契约全绿"。
    **绿着闸门，48 人动态组队整个没生效。**
    一个 `required=False`，把"夹具坏了"伪装成了"这次可选调用没成功"。
    """
    if required:
        return False
    return not isinstance(exc, HarnessError)


def is_retryable(exc: BaseException) -> bool:
    """这个异常值不值得重试。

    给**事件层**用：`error` 事件要带上它，前端才能把"等会儿自己会好"
    （限流、连接抖动）与"得有人去看"（密钥错了、代码 bug）分开显示。

    非 `ProviderError` 一律为 False——`KeyError`、`AttributeError` 这类是代码缺陷，
    重试只会让 bug 晚几分钟出现。默认 False 而不是"猜一个 True"：
    猜错的方向是让用户干等一个永远不会成功的重试。
    """
    return bool(getattr(exc, "retryable", False))


class ProviderNotConfigured(ProviderError):
    """缺少 API Key 或 base_url。属于部署配置问题，重试没有意义。"""


class AuthFailed(ProviderError):
    """密钥无效或无权限。"""


class QuotaExhausted(ProviderError):
    """额度用尽或账户欠费。重试不会让额度回血，应当直接失败并让人介入。"""


class RateLimited(ProviderError):
    """触发限流。**这是唯一值得立即退避重试的常见错误。**"""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, detail=detail)
        self.retry_after = retry_after


class Transient(ProviderError):
    """瞬时故障：连接重置、超时、5xx。重试通常有效。"""

    retryable = True


class MalformedResponse(ProviderError):
    """响应结构不符合预期（缺字段、JSON 截断）。

    可重试——大模型输出被长度截断是常见情况，重试往往能得到完整结果。
    """

    retryable = True


class BadRequest(ProviderError):
    """请求本身不合法（参数越界、模型名不存在）。重试会得到同样的错误。"""


class NotFound(ProviderError):
    """目标资源不存在（如搜索结果指向的页面已下线）。"""


class HarnessError(ProviderError):
    """**夹具/评测基础设施**的错误，不是运行时故障。

    它继承 `ProviderError`，是为了让它出现在同一层错误处理里——
    卡带的缺失确实是在 provider 边界上暴露出来的。

    但它和其余子类有一条**关键区别，必须被区别对待**：
    `RateLimited` / `Transient` 这些说的是"这次调用没成功，但系统是好的"，
    所以 `required=False` 时降级继续是对的——生产里不该因为一个可选步骤
    挂掉整份报告。而本类说的是"**这次运行所依赖的东西本身是坏的**"：
    录制缺了一条、prompt 改过之后 key 变了。降级继续只会产出一个
    建立在残缺输入上的结果，而那个结果**看起来完全正常**。

    实测踩到过（见 `calls.py::_should_degrade` 的 docstring）：
    调度那条录制因为改了提示词而失效，它是 `required=False`，
    于是异常被吞、拿到空 payload、三个角色全类退回默认队伍，
    报告照常产出，闸门报"12 项契约全绿"——**闸门绿着，而 48 人动态组队整个没生效**。

    所以规矩是一条：**降级逻辑不许吞本类。** 它不是"这次运气不好"，
    是"这次的结果不该被相信"。
    """


#: HTTP 状态码 → 错误类。适配器先查自己的业务错误码，落到这个表兜底。
_BY_STATUS: dict[int, type[ProviderError]] = {
    400: BadRequest,
    401: AuthFailed,
    402: QuotaExhausted,
    403: AuthFailed,
    404: NotFound,
    408: Transient,
    409: BadRequest,
    422: BadRequest,
    429: RateLimited,
}


def classify_status(
    status: int,
    *,
    provider: str = "",
    detail: str = "",
    retry_after: float | None = None,
) -> ProviderError:
    """把 HTTP 状态码翻译成类型化错误。

    5xx 一律归为瞬时故障——服务端错误重试是标准做法，不需要区分具体是哪个 5xx。
    """
    if status >= 500:
        return Transient(f"服务端错误 HTTP {status}", provider=provider, detail=detail)
    if status == 429:
        return RateLimited(
            "触发限流", provider=provider, detail=detail, retry_after=retry_after
        )
    error_cls = _BY_STATUS.get(status)
    if error_cls is None:
        return ProviderError(f"请求失败 HTTP {status}", provider=provider, detail=detail)
    return error_cls(f"请求失败 HTTP {status}", provider=provider, detail=detail)


def classify_httpx(exc: Exception, *, provider: str = "") -> ProviderError:
    """把 httpx 的传输层异常翻译成类型化错误。

    超时与连接错误是可重试的；其余（如编码问题）不是。
    """
    if isinstance(exc, httpx.TimeoutException):
        return Transient(f"请求超时：{exc}", provider=provider)
    if isinstance(exc, httpx.TransportError):
        return Transient(f"网络传输失败：{exc}", provider=provider)
    return ProviderError(f"请求异常：{exc}", provider=provider)


def parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头。只支持秒数形式，HTTP 日期形式罕见且不值得为它写解析器。"""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
