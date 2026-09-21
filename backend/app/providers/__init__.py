"""Provider 适配层。

对外的入口是 `registry.get_llm()` / `registry.get_search()` / `registry.get_fetcher()`。
业务代码只依赖 `base.py` 里的 Protocol，不依赖任何具体实现。
"""
