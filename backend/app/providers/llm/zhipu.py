"""智谱 GLM provider。

放在这里的意义是**证明适配层真的可插拔**：整个文件二十来行，
接入它没有改动流水线、没有改动 config.py，只是多了一个类和一个注册装饰器。

默认作为 DeepSeek 的降级备选（`.env` 里配 `LLM_FALLBACK=zhipu`）。
模型名同样留空由 `.env` 指定，理由见 deepseek.py 的说明。
"""
from __future__ import annotations

from app.providers.base import LLMCapabilities
from app.providers.llm.openai_compat import OpenAICompatProvider
from app.providers.registry import register_llm


@register_llm("zhipu")
class ZhipuProvider(OpenAICompatProvider):
    name = "zhipu"
    display_name = "智谱 GLM"

    capabilities = LLMCapabilities(
        json_mode=True,
        json_schema_mode=False,
        thinking_toggle=True,
        streaming=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    )

    default_base_url = "https://open.bigmodel.cn/api/paas/v4"
    default_models: dict[str, str] = {}

    _THINKING_EXEMPT = ("z1-air", "z1-flash", "reasoner")

    def _extra_body(self, model: str) -> dict | None:
        """GLM 系部分模型默认开思考，关掉的收益与 DeepSeek 侧相同。"""
        if any(marker in model.lower() for marker in self._THINKING_EXEMPT):
            return None
        return {"thinking": {"type": "disabled"}}
