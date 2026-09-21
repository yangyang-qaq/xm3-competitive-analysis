# Provider 适配层

这是整个项目最核心的一块设计，也是最能说明"工程"与"调 API"区别的地方。

---

## 要解决的问题

用 LLM 做调研，第一版通常长这样：在采集循环里写下

```python
resp = httpx.post(url, json={"q": q, "include": "douyin.com", "freshness": "oneMonth"})
```

然后换一个搜索源，就得回头改流水线。改成 Tavily 要把 `include` 换成
`include_domains`、`oneMonth` 换成 `days: 30`；再改成 Serper 又要变成
把 `site:` 拼进 query、时间范围换成 `tbs`。

问题不在"改起来累"，而在于**厂商方言漏进了编排层**。一旦漏进去：

- 换 provider 要动流水线，而流水线是这个系统里最贵、测试最厚的部分；
- 每个新 provider 都要在流水线里加一个 `if provider == ...`；
- 厂商的限流/鉴权错误码散落在各处，每处写法不同；
- **相关性过滤这类"业务逻辑"会不小心写进适配器**，换搜索源时静默消失。

最后一条最危险，因为它不报错。这个项目的对手项目就是这么做的
（其相关性过滤在搜索适配器内部），后果是换搜索源之后过滤能力
无声无息地没了，报告质量下降而没人知道为什么。

所以这一层的目标只有一句：**流水线里不出现任何厂商名。**

---

## 归一化接口

[`backend/app/providers/base.py`](../backend/app/providers/base.py) 定义了
三个 Protocol 和它们的输入输出类型。所有厂商方言在适配器内部消化掉，
外面只看到这些。

### 检索

```python
Freshness = Literal["any", "day", "week", "month", "year"]

@dataclass(frozen=True)
class SearchQuery:
    text: str
    limit: int = 10
    sites: tuple[str, ...] = ()
    freshness: Freshness = "any"
    locale: str = "zh-CN"

@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    site_name: str = ""
    published_at: str = ""
    provider: str = ""
    rank: int = 0
```

`Freshness` 是**五档枚举**而不是"天数"或厂商的字符串。理由是它是
意图（"我想要最近一个月的"），而各家对"一个月"的实现不一样：
博查是 `oneMonth`，Tavily 是 `days: 30`，有的家按自然月算。
把 `30` 写进接口会让"一个月"这个意图提前被一个数字钉死。

`SearchQuery` 是 frozen dataclass 且有 `cache_key()`——录制的键就是
它，所以字段顺序和内容必须确定。

### LLM

```python
Tier = Literal["core", "aux", "fast"]

@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: TokenUsage
    latency_ms: int
    finish_reason: str = "stop"

@dataclass(frozen=True)
class LLMCapabilities:
    json_mode: bool = False
    json_schema_mode: bool = False
    thinking_toggle: bool = False
    streaming: bool = True
    max_context_tokens: int = 32_000
    max_output_tokens: int = 8_192
```

`chat()` 的签名里只接受**档位**，不接受模型名：

```python
def chat(self, messages, *, tier: Tier = "aux", temperature=0.6,
         max_tokens=2048, json_mode=False, purpose="",
         evidence_ids=None) -> LLMResponse
```

`tier="core"` 是什么意思由 provider 决定。流水线说"这一步很重要，
用最好的模型"，至于最好的那个是哪个模型，是适配器的事。新增一家
provider 不需要动流水线一行。

`purpose` 与 `evidence_ids` **不参与缓存键**，只进 meta——它们用于
可观测性（"这次调用是为了写定价那一节"），不影响调用的内容。

### 抓取

```python
@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    ok: bool
    text: str
    title: str = ""
    published_at: str = ""
    status: int = 0
    images: list[ImageRef] = field(default_factory=list)
    og_image: str = ""
    captured_at: str = ""
    degraded: bool = True
    error: str = ""
```

`degraded` 默认 **`True`**——默认值取"这次抓取不可信"。
忘了设的后果是"少一条好证据"，而不是"一条坏证据被当成好的"。

---

## 方言对照表

这张表是这一层存在的理由。左边是流水线写的，右边是各家实际发出的。

### 站点过滤

| 归一化 | 博查 Bocha | Tavily | Serper（未实现） |
|---|---|---|---|
| `sites=("douyin.com",)` | `"include": "douyin.com"`（`\|` 连接） | `"include_domains": ["douyin.com"]` | 把 `site:douyin.com` 拼进 `query` |

三家的形状**没有一个相同**：一个是 `|` 连接的字符串，一个是数组，
一个根本没有这个参数、只能靠查询语法。流水线不需要知道。

### 时间范围

| 归一化 | 博查 | Tavily |
|---|---|---|
| `any` | `"freshness": "noLimit"` | **整个键不发** |
| `day` | `"freshness": "oneDay"` | `"days": 1` |
| `week` | `"freshness": "oneWeek"` | `"days": 7` |
| `month` | `"freshness": "oneMonth"` | `"days": 30` |
| `year` | `"freshness": "oneYear"` | `"days": 365` |

`any` 那一行是这张表里最值得看的一格：博查要显式发 `noLimit`，
Tavily 要**完全不发这个键**（发 `null` 会被拒）。同一个语义，
一个是"显式说不要限制"，一个是"别说这个事"。

### 其他参数

| 归一化 | 博查 | Tavily |
|---|---|---|
| `limit` | `"count"`，上限 50 | `"max_results"`，上限 20 |
| 摘要 | `"summary": true`（写死） | `"content"` 字段直接返回 |
| 搜索深度 | 无此概念 | `"search_depth": "advanced"`（写死） |
| 鉴权 | `Authorization: Bearer <key>` 头 | `api_key` **在请求体里** |
| 站点名 | 响应里有 `siteName` | 响应里没有，从 URL 取 host |

上限不同（50 vs 20）这件事必须由适配器处理，不能靠调用方记得。
所以 `limit` 在适配器里会被 `min(limit, 该家上限)` 夹一次。

### LLM 方言

| 归一化 | DeepSeek | 智谱 GLM |
|---|---|---|
| 关闭思考 | `extra_body={"thinking": {"type": "disabled"}}` | 同左 |
| 不需要关的模型 | 名字含 `reasoner` | 名字含 `z1-air` / `z1-flash` / `reasoner` |
| 缓存命中 token | `usage.prompt_cache_hit_tokens`（顶层） | — |
| JSON 模式 | `response_format={"type": "json_object"}` | 同左 |

"关闭思考"这条值得单独说：这两家的推理模型默认会输出一段思考过程，
而调研流水线要的是结构化 JSON。不发这个参数，思考内容会混进正文里，
后面的 JSON 解析就崩。**这个 `extra_body` 的形状是厂商方言，
所以它只出现在适配器里**，流水线完全不知道有"思考开关"这回事。

缓存命中的 token 位置也不同：DeepSeek 放在顶层
`prompt_cache_hit_tokens`，OpenAI 系放在嵌套的
`prompt_tokens_details.cached_tokens`。基类默认读后者，
`DeepSeekProvider` 覆盖前者。

---

## 能力协商

方言能藏起来，但**能力差异藏不住**：有的搜索源不支持站点过滤。
这时正确的做法不是静默忽略参数（那会返回一批不相干的结果，而调用方
以为已经过滤过了），而是**显式退化成另一条策略**。

```python
# backend/app/core/pipeline/calls.py
def adapt_query(ctx, query):
    capabilities = getattr(ctx.search, "capabilities", None)
    if not query.sites or (capabilities and capabilities.site_filter):
        return query
    _stats(ctx).site_filter_folded += 1
    keywords = " ".join(site.replace("www.", "") for site in query.sites)
    return SearchQuery(text=f"{query.text} {keywords}".strip(),
                       limit=query.limit, sites=(), freshness=query.freshness)
```

四点：

1. **不拼 `site:` 前缀**，只把域名当普通关键词拼进去。`site:` 本身
   就是一种厂商方言，拼进去等于把方言写进了流水线。
2. **同时清空 `sites`**。不清的话适配器还会尝试过滤，然后返回 0 条。
3. **计数**：`site_filter_folded` 出现在报告的指标里。降级是允许的，
   静默降级不是——它会变成一个可以看见的数。
4. 这段代码**不知道**自己在跟哪家打交道，只问 `capabilities.site_filter`。

> **能力协商目前只实现了 `site_filter` 一项。**
> `freshness_filter` / `long_snippet` / `max_results_per_call` /
> `json_mode` / `thinking_toggle` / 上下文上限都还没有对应的策略分支。
> 这些能力在类型上有声明、在 `describe_all()` 里能查到，
> 但流水线还不会根据它们改变行为。这是一个真实的缺口，
> 不是"暂时看不出来"——写在这里以免读者以为协商是完备的。

---

## 错误分类与重试

### 错误类型

[`errors.py`](../backend/app/providers/errors.py) 把错误分成"值得重试"
和"不值得"两类，这个区分写在类属性上：

| 类 | `retryable` | 含义 |
|---|---|---|
| `ProviderError` | `False` | 基类 |
| `ProviderNotConfigured` | `False` | 没配 key / base_url |
| `AuthFailed` | `False` | key 无效或无权限 |
| `QuotaExhausted` | `False` | 额度用尽或欠费 |
| `RateLimited` | **`True`** | 触发限流（带 `retry_after`） |
| `Transient` | **`True`** | 连接重置、超时、5xx |
| `MalformedResponse` | **`True`** | 响应形状不对（字段缺失、JSON 截断） |
| `BadRequest` | `False` | 请求本身不合法 |
| `NotFound` | `False` | 目标不存在（页面被删） |
| `HarnessError` | `False` | **测试夹具/评测设施的错误，不是运行时的故障** |

`HarnessError` 是这一层里最容易被忽略、但最重要的一条。
它代表"这不是外部世界的问题，是我们的测试环境配错了"。
**降级逻辑必须永远不吞它**——吞掉的话，一个录错的夹具会让流水线
安静地走降级路径，跑出一份看起来正常的报告。
`should_degrade()` 里对它的判断是第一句。

HTTP 状态码到异常的映射在一张表里（`_BY_STATUS`），
不在各个适配器里各写一份：400→`BadRequest`、401/403→`AuthFailed`、
402→`QuotaExhausted`、404→`NotFound`、408→`Transient`、
409/422→`BadRequest`、429→`RateLimited`、≥500→`Transient`。

**"怎么判断是不是限流"这件事只有一处实现。** 上游项目是靠
`if "429" in str(err)` 这样的字符串匹配——差别不在于哪个更优雅，
而在于那是"我们处理了 429"和"我们有一层错误抽象"的区别。

### 重试策略

```python
DEFAULT = RetryPolicy(backoffs=(4.0, 8.0, 15.0, 25.0))   # 4 次重试 / 共 5 次尝试
FETCH   = RetryPolicy(backoffs=(1.0, 3.0))                # 2 次重试 / 共 3 次尝试
NO_RETRY = RetryPolicy(backoffs=())
```

退避是**指数偏慢**的（4/8/15/25 秒），因为限流窗口通常是分钟级，
隔一秒重试只是再撞一次。

抓取用更短的重试（1s/3s）：一次页面抓取失败不影响报告结论，
而一个卡住的抓取会拖住整条流水线。

`with_backoff()` 有两条规则：

- 只重试 `exc.retryable == True` 的 `ProviderError`。
  **非 `ProviderError` 的异常（KeyError、AttributeError）直接放行**——
  它们是代码缺陷，重试只会让同一个 bug 多跑四次。
- 如果异常带了 `retry_after`（来自 `Retry-After` 头），
  实际等待取 `max(退避值, retry_after)`。服务端说"60 秒后再来"时，
  按 4 秒重试是没用的。

---

## 录制与回放

适配层带来一个副作用，而且它是这个项目验证成本能压到零的原因：
**既然所有外部调用都经过同一层，那就可以在这一层包一层录制/回放。**

```
CASSETTE_MODE=off     正常联网（开发默认）
CASSETTE_MODE=record  联网 + 把调用写进 cassette（花钱，主动开）
CASSETTE_MODE=replay  只读 cassette，缺失就抛 CassetteMiss，**绝不联网**（CI 默认）
```

于是 CI 里跑的是**同一份 `run_pipeline` 代码**，只换掉了 provider 实现。
不需要为测试写一套平行的"假流水线"，也不需要联网。

### 键怎么算

```python
def _digest(payload):
    return sha256(json.dumps(payload, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
```

- LLM：`{kind, messages, tier, temperature, max_tokens, json_mode}`
- 检索：`{kind, **query.cache_key()}`
- 抓取：`{kind, url}`

`sort_keys=True` + 紧凑分隔符保证同样的调用得到同样的键。

### `CassetteMiss` 继承 `HarnessError`

这一条是有意的。回放时缺记录**不是**"provider 挂了"，
而是"夹具不全"。它必须穿过降级逻辑直接抛出去，否则一次录漏
会表现成"这一次跑得有点降级"，而不是"夹具缺一条"。

### 已知限制

- **流式调用不录制**。`ReplayProvider.chat_stream()` 直接抛
  `CassetteMiss("流式调用不录制，无法回放")`。目前 SSE 推的是流水线
  事件，不是 token 流，所以这条不影响任何测试。
- **回放不保证时间**。同一次运行的实录与回放对比：
  31 项指标里 31 项逐位一致，只有 `durationMs`（50859 → 139 ms）
  和 `firstEvidenceMs` 这类时间量变了。所以评测的漂移比对里
  `DRIFT_EXCLUDED = {"durationMs", "firstEvidenceMs"}`——
  **对回放跑出来的耗时做断言是没有意义的**。

现有 cassette：`llm.deepseek.jsonl`（98 条）、`search.bocha.jsonl`（143 条）、
`fetch.http.jsonl`（68 条）。录制清单在
[`MANIFEST.json`](../backend/tests/fixtures/cassettes/MANIFEST.json)：
5 条黄金查询、90 次 LLM 调用、571,355 prompt tokens、
101,236 completion tokens、成本 **$0.1326**。

---

## 降级链

配了 `LLM_FALLBACK` 时，主 provider 外面会包一层 `FallbackLLM`。

包装顺序（由内到外）：**真实 provider → 录制 → 降级**。

两条规则值得说：

- **致命错误会闩锁（latch）。** `AuthFailed` / `QuotaExhausted` /
  `ProviderNotConfigured` 三种一旦出现，`latched` 置位，
  后续调用**不再尝试主 provider**，直接走降级。
  理由：key 无效这件事不会在 3 秒后自己变好，每次调用都去撞一遍
  只是把每次调用都拖慢一个超时。
- **`HarnessError` 永远重新抛出**，绝不降级（与上面同一条理由）。
- **`chat_stream` 不做中途切换**：流已经吐了一半，换一家接着吐
  只会得到一段语义断裂的文本。宁可让这一次失败。
- `RateLimited` / `Transient` / `MalformedResponse` 只降级**那一次调用**，
  不闩锁——这些确实会自己好。

`name` 是 `f"{主}>{副}"`，`summary()` 返回
`{primary, secondary, latched, fallbackCalls, degraded, events}`，
在 `/api/providers/health` 里能看到。
**降级了多少次是一个可见的数**，不是日志深处的一行。

---

## 接入一家新 provider

以新增一个搜索源为例，实际要写的只有这些：

```python
@register_search("example")
class ExampleSearchProvider:
    name = "example"
    capabilities = SearchCapabilities(site_filter=False, freshness_filter=True,
                                      long_snippet=True, max_results_per_call=30)

    def __init__(self, settings):
        cred = settings.credentials(self.name)
        if not cred.api_key:
            raise ProviderNotConfigured("未配置 EXAMPLE_API_KEY")
        self._cred = cred

    def search(self, query: SearchQuery) -> list[SearchHit]:
        payload = {"q": query.text,
                   "n": min(query.limit, self.capabilities.max_results_per_call)}
        # 方言在这一行被消化掉：
        if query.freshness != "any":
            payload["since"] = _SINCE[query.freshness]
        ...
        return [SearchHit(title=..., url=..., snippet=..., provider=self.name, rank=i)
                for i, item in enumerate(data["items"])]

    def probe(self) -> ProbeResult:
        return probe_search(self)
```

然后把模块名加进 `registry.py` 的 `_BUILTIN_MODULES`，
在 `.env` 里写 `SEARCH_PROVIDER=example`。**流水线零改动。**

能力声明里 `site_filter=False` 时，流水线会自动把域名折进查询词
（见上文能力协商），这是免费的。

`probe_search()` 里有一条容易被忽略的约定：**返回 0 条结果算探测成功**。
探测的是"这个接口能不能调通"，不是"这个词有没有东西"。
把 0 结果当失败会让探测结果依赖于探测词选得好不好。

---

## 目前注册了什么

| 类型 | 名字 | 状态 |
|---|---|---|
| llm | `mock` | 确定性假 provider，零成本 |
| llm | `deepseek` | 默认 |
| llm | `zhipu` | 适配器完整，未在默认配置里启用 |
| search | `mock` | 同上 |
| search | `bocha` | 默认 |
| search | `tavily` | 适配器完整，未在默认配置里启用 |
| fetch | `mock` | 同上 |
| fetch | `http` | 默认，trafilatura + BeautifulSoup 兜底 |

### 诚实说明

- **`zhipu` / `tavily` 的适配器写完了，但没有被真实调用过。**
  它们的方言映射是照着公开文档写的，测试用的是
  `httpx.MockTransport` 断言请求体的形状，**不是**对真实接口的
  一次成功调用。第一次接真接口时仍可能需要微调。
- **没有 `serper` 适配器**，尽管 `docs` 与 `base.py` 的注释里提到过它
  作为"另一种方言"的例子，而且开发机上的 `.env` 里留着
  `SERPER_API_KEY=` / `SERPER_BASE_URL=` 两行空配置。
  后者应该被删掉——配置项是一种承诺。
- **`Settings.llm_max_retries` 没有被任何代码读取**，
  重试次数实际由 `retry.py` 的 `RetryPolicy` 决定。这个字段应该删掉
  或接进去，留着会让读配置的人以为改它有用。
