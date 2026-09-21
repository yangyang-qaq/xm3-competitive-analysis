"""模型在结构化块里填了不该填的数/漏了该填的数时，报告会怎么处理。

三个真实缺陷，各自对应一组容易写出的"看起来正常"的输出：

1. **`fiveForces[].intensity` 缺了。** 早先代码默认 3.0——在 1–5 分量表上
   这就是一句"强度中等"的**判断**，而模型根本没做这个判断。
   补 0 才是"没判定"，因为 0 落在量表之外、不可能是真值，而且**可逆**：
   读者一眼看出这是缺的，不是"恰好中等"。同时记一条 coercion note。
2. **`marketShare[].share` 填成了用户规模。** 库里那份真报告里它是 `2022.0`，
   而同一项的 `basis` 自己写着"2022年 Notion 用户规模（3000万）"。
   饼图会把它画成 `Notion：2022%`——不报错、不空，就是单位全错。
   所以 `charts` 里有一道 (0, 100] 闸，越界就**一张饼图都不出**。
3. **`share` 正常时那张饼图要带 `note`。** 份额是推算值，口径在
   `basis` 里逐条写着；不带这句话的百分比会被读成实测值。

前两条的"修复"都必须在**报告里留痕**（coercion note），
否则就是悄悄改数据——而这三条链子（默认值、闸、披露）**没有一条**
能被纯单元测试覆盖到，它们都在 `analyze.run()` 里。
"""
from __future__ import annotations

import asyncio

from app.core.pipeline import analyze
from app.providers import mock

#: 越界那个真实值。用它而不是随便一个 999，是因为这个数有出处：
#: 它出现在库里那份报告里，且同一项的 basis 解释了它的来历。
REAL_UNIT_ERROR = 2022.0


def _comparison(**overrides) -> dict:
    payload = mock._comparison_payload()
    payload.update(overrides)
    return payload


def _share_rows(*rows: tuple[str, float, str]) -> list[dict]:
    """把份额行挂上**真实存在**的证据 id。

    不挂的话，下面两条"越界就不出图"的用例会绿在一个**错误的原因**上：
    `build_charts()` 出口有一道收口——没有证据链的图一律不生成。
    份额行不带 `evidenceIds` 时，饼图先被那道收口拦掉，于是
    **份额闸被删掉、断言照样通过**。

    这不是推理出来的，是量出来的：把 `charts.py` 里的份额闸换成
    `if False:`，本文件十项全绿（见 `问题记录.md` 问题 52）。
    加证据 id 之后，饼图"本来该出得来"，唯一拦得住它的就只剩份额闸。
    """
    ids = mock._pick_evidence(len(rows))
    assert ids, (
        "mock 的证据池是空的：份额行拿不到证据 id，饼图会被"
        "「没有证据链」那道收口拦掉，这几条用例又会绿在错误的原因上"
    )
    return [
        {
            "brand": brand,
            "share": share,
            "basis": basis,
            "evidenceIds": [ids[index % len(ids)]],
        }
        for index, (brand, share, basis) in enumerate(rows)
    ]


def _pie(body: dict) -> dict | None:
    return next((c for c in body["charts"] if c["chartId"] == "chart-market-share"), None)


def _dropped_for_missing_evidence(body: dict) -> bool:
    """饼图缺席的原因是不是"这张图没有证据链"。

    这是 `_share_rows` 那条注释的可执行版本：只断言"饼图不在"的话，
    它缺席的原因可以是上游任意一道闸——而那正是这条用例曾经绿错的地方。
    """
    return any("市场份额" in line and "证据链" in line for line in body["degraded"])


def _run(run_mock_pipeline):
    return asyncio.run(run_mock_pipeline())


# ============================================================
# 五力强度：缺了就是"未判定"，不是"中等"
# ============================================================


def test_没给强度的五力补零而不是中等(run_mock_pipeline, monkeypatch) -> None:
    """**本文件的重点之一。**

    3.0 在 1–5 量表上是一个合法的、看起来很正常的判断。补 3.0 的后果是
    报告里出现一句模型从没做过的判断，而且**它是不可逆的**：
    读者无法区分"模型说中等"和"模型没说"。
    """
    payload = mock._comparison_payload()
    payload["fiveForces"] = [{"force": "现有竞争者", "analysis": "", "evidenceIds": []}]
    monkeypatch.setitem(mock._FIXTURES, "analyze_comparison", lambda: payload)

    body = _run(run_mock_pipeline).body

    assert body["fiveForces"][0]["intensity"] == 0.0, (
        "缺强度时补的值必须落在 1–5 之外，否则它会被读成一个真判断"
    )


def test_补零这件事写进了改写记录(run_mock_pipeline, monkeypatch) -> None:
    """悄悄补 0 也不行：报告里的强度与模型输出不一致，这件事要能查到。"""
    payload = mock._comparison_payload()
    payload["fiveForces"] = [{"force": "现有竞争者", "analysis": "", "evidenceIds": []}]
    monkeypatch.setitem(mock._FIXTURES, "analyze_comparison", lambda: payload)

    repairs = _run(run_mock_pipeline).body["coercion"]["repairs"]

    assert any("现有竞争者" in line and "强度" in line for line in repairs), (
        f"改写记录里没有五力强度这一条：{repairs}"
    )


def test_给了强度就照用_不被默认值顶掉(run_mock_pipeline) -> None:
    """反向守卫：别把整段逻辑写成"永远补 0"。

    mock 原样给的是 4，报告里就必须是 4。
    """
    assert _run(run_mock_pipeline).body["fiveForces"][0]["intensity"] == 4.0


# ============================================================
# 份额闸：越界就不出图
# ============================================================


def test_份额填成用户规模时不出饼图(run_mock_pipeline, monkeypatch) -> None:
    """**本文件的重点之二。**

    照画的话饼图只有一片、标注 `示例品牌：2022%`。它不报错、不空白，
    就是一张看着有数据、单位完全错的图。而"市场份额"这个标题
    会让人相信那是个占比。
    """
    monkeypatch.setitem(
        mock._FIXTURES,
        "analyze_comparison",
        lambda: _comparison(
            marketShare=_share_rows(
                ("示例品牌", REAL_UNIT_ERROR, "2022年示例品牌用户规模（3000万）")
            )
        ),
    )

    body = _run(run_mock_pipeline).body

    assert _pie(body) is None, "份额超出 0–100 还出了饼图"
    assert not _dropped_for_missing_evidence(body), (
        "饼图是缺证据链被收口拦掉的，不是被份额闸拦掉的——这条断言没在测份额闸"
    )
    # 表格里的原值要留着——剔掉的话读者连"模型填了什么"都看不到
    assert body["marketShare"][0]["share"] == REAL_UNIT_ERROR


def test_份额越界这件事写进了改写记录(run_mock_pipeline, monkeypatch) -> None:
    monkeypatch.setitem(
        mock._FIXTURES,
        "analyze_comparison",
        lambda: _comparison(
            marketShare=[
                {"brand": "示例品牌", "share": 2022.0, "basis": "用户规模", "evidenceIds": []}
            ]
        ),
    )

    repairs = _run(run_mock_pipeline).body["coercion"]["repairs"]

    assert any("示例品牌" in line and "份额" in line for line in repairs), (
        f"改写记录里没有份额越界这一条：{repairs}"
    )


def test_份额正常时照常出图(run_mock_pipeline) -> None:
    """反向守卫：闸不能把正常数据也拦掉。

    mock 给的是 23.5，那是一条正常的份额。
    """
    assert _pie(_run(run_mock_pipeline).body) is not None


def test_只有一个品牌越界就整张不出(run_mock_pipeline, monkeypatch) -> None:
    """**闸的粒度是"这张图"，不是"这一行"。**

    一片正常的 23.5 和一片 2022 画在同一张饼里，视觉上 2022 那一片
    会把 23.5 挤成一条看不见的缝——于是正常的那个数也读不出来了。
    所以越界一条就不出图，整张不出比出一张半真半假的好。
    """
    monkeypatch.setitem(
        mock._FIXTURES,
        "analyze_comparison",
        lambda: _comparison(
            marketShare=_share_rows(
                ("示例品牌", 23.5, "按样本推算"),
                ("对照品牌", REAL_UNIT_ERROR, "用户规模"),
            )
        ),
    )

    body = _run(run_mock_pipeline).body

    assert _pie(body) is None
    assert not _dropped_for_missing_evidence(body), (
        "饼图是缺证据链被收口拦掉的，不是被份额闸拦掉的——这条断言没在测份额闸"
    )


# ============================================================
# 那张饼图上的口径说明
# ============================================================


def test_份额饼图带着推算口径的说明(run_mock_pipeline) -> None:
    """份额是推算值，不是实测值。

    这句话由后端写在 spec 的 `note` 里，前端负责显示——丢在任一侧，
    读者看到的就只是一个光秃秃的百分比。
    """
    pie = _pie(_run(run_mock_pipeline).body)

    assert pie is not None
    assert "推算" in pie["spec"].get("note", ""), (
        f"饼图 spec 里没有口径说明：{pie['spec'].keys()}"
    )


def test_正常数据的份额原样进图(run_mock_pipeline) -> None:
    """不做归一化、不改单位：`share` 是 0–100 的数，直接当 value。"""
    pie = _pie(_run(run_mock_pipeline).body)

    assert pie is not None
    assert [item["share"] for item in pie["spec"]["data"]] == [23.5]


def test_没有份额数据时不出图(run_mock_pipeline, monkeypatch) -> None:
    """空数组是合法的（这份报告没采到份额），出图条件是"有数据"。"""
    monkeypatch.setitem(
        mock._FIXTURES, "analyze_comparison", lambda: _comparison(marketShare=[])
    )

    assert _pie(_run(run_mock_pipeline).body) is None


# ============================================================
# 同一品牌两条份额：口径冲突，不画饼图
# ============================================================


def test_同一品牌两条份额时不出饼图(run_mock_pipeline, monkeypatch) -> None:
    """**库里那份真报告的形状。** `特来电` 有两行：41.0 与 27.4。

    两行都合法——不同机构的统计口径不同，表格里逐行保留。
    但画进同一张饼图就是把同一个品牌算了两遍：图上两片"特来电"，
    而读者只会读出"这个市场集中度很高"。
    """
    monkeypatch.setitem(
        mock._FIXTURES,
        "analyze_comparison",
        lambda: _comparison(
            marketShare=_share_rows(
                ("特来电", 41.0, "中国充电联盟统计"),
                ("特来电", 27.4, "三个皮匠报告"),
                ("国家电网", 17.8, "三个皮匠报告"),
            )
        ),
    )

    body = _run(run_mock_pipeline).body

    assert _pie(body) is None
    assert not _dropped_for_missing_evidence(body), (
        "饼图是缺证据链被收口拦掉的，不是被口径冲突拦掉的——这条断言没在测那道闸"
    )


def test_口径冲突写进了改写记录(run_mock_pipeline, monkeypatch) -> None:
    """悄悄不画是不行的：读者会以为是"这次没采到份额"。

    而真相是"采到了两个互不相同的数"——那本身是个值得知道的事实。
    """
    monkeypatch.setitem(
        mock._FIXTURES,
        "analyze_comparison",
        lambda: _comparison(
            marketShare=_share_rows(
                ("特来电", 41.0, "中国充电联盟统计"),
                ("特来电", 27.4, "三个皮匠报告"),
            )
        ),
    )

    repairs = _run(run_mock_pipeline).body["coercion"]["repairs"]
    line = next((r for r in repairs if "特来电" in r and "口径" in r), None)

    assert line is not None, f"改写记录里没有口径冲突这一条：{repairs}"
    assert "41" in line and "27.4" in line, f"两个数都要写出来，实际是：{line}"


def test_每个品牌拿到自己那棵功能树(run_mock_pipeline) -> None:
    """**这条是真缺陷的回归守卫**（问题 54）。

    缺陷的形状是：同一个品牌两棵树、另一个品牌一棵都没有。
    mock 早先对每个品牌都回同一份载荷，所以"一个品牌两棵树"在 mock 下
    是常态——这条用例问的是"树的品牌互不相同，且等于要对比的那几个"。
    """
    body = _run(run_mock_pipeline).body
    brands = [tree["brand"] for tree in body["featureTrees"]]

    assert brands == ["示例品牌", "对照品牌"], f"功能树的品牌对不上：{brands}"
    assert len(brands) == len(set(brands)), f"同一个品牌拿到了多棵树：{brands}"
    assert "示例调研对象" not in brands, (
        "调研对象是「示例调研对象」——一句短语，不是品牌，不该被当成品牌问功能树"
    )


def test_调研对象不是品牌这件事说了出来(run_mock_pipeline) -> None:
    """跳过要说。不说的话，读者看到矩阵里有调研对象、功能树里没有，
    只能猜是漏了还是故意的。"""
    # 读**推出去的事件**而不是报告里那份：这条用例问的是"实时看的人
    # 有没有被告知"，而报告落库的那份 `thoughts` 是事后回看用的。
    # 两者一致性另有 `test_streamed_thoughts_match_the_persisted_ones` 守着。
    outcome = _run(run_mock_pipeline)
    thoughts = [p["thought"]["text"] for p in outcome.payloads("thought")]

    assert any("示例调研对象" in t and "不是竞品" in t for t in thoughts), (
        f"思维流里没说为什么跳过调研对象：{thoughts[-6:]}"
    )


def test_问A答B的功能树被丢掉(run_mock_pipeline, monkeypatch) -> None:
    """模型把回答标成了另一个品牌时，**丢掉它**，不要改名收下。

    改名收下等于把 B 的能力挂到 A 名下——那是替 A 编内容。
    丢掉的后果是这一块缺失，而缺失是明说的（`completeness` 判 `missing`）。
    """
    mislabelled = mock._structured_payload()
    mislabelled["featureTree"] = {**mislabelled["featureTree"], "brand": "特来电"}
    monkeypatch.setitem(mock._FIXTURES, "analyze_structured", lambda: mislabelled)

    body = _run(run_mock_pipeline).body

    assert body["featureTrees"] == [], (
        "品牌归属对不上的功能树进了报告——它会被当成「示例品牌」或「对照品牌」的能力"
    )
    assert any("归属" in line for line in body["coercion"]["repairs"]), (
        f"丢掉这件事没有记账：{body['coercion']['repairs']}"
    )
    # 缺失的披露渠道是 `completeness`，**不是** `degraded`——
    # 后者的那条说明只在三个块同时为空时才写（见 `_analyze_structured`），
    # 而这里定价与画像都解析出来了。写这条断言时我原本指望 `degraded`，
    # 实测是空的：**报告缺了一整块，降级清单里一个字都没有。**
    # 好在完整度把这一块判成了 `missing`，而 `isPublishable` 因此为假——
    # 这才是真正拦得住那份报告的那道闸。
    completeness = body["completeness"]
    assert completeness["blocks"]["featureTrees"] == "missing", completeness
    assert "功能矩阵" in completeness["missing"], completeness
    assert completeness["isPublishable"] is False, (
        "功能树整块被丢掉之后报告还能发布，等于这一块没了也没人管"
    )


def test_品牌改了写法仍然收下(run_mock_pipeline, monkeypatch) -> None:
    """反向守卫：别把这条闸写成"名字必须一字不差"。

    `国家电网` / `国家电网有限公司` 是同一个品牌，模型两种写法都可能给。
    收下它，也让"丢掉"那条闸不至于把正常数据一起丢掉。
    """
    renamed = mock._structured_payload()
    renamed["featureTree"] = {**renamed["featureTree"], "brand": "示例品牌有限公司"}
    monkeypatch.setitem(mock._FIXTURES, "analyze_structured", lambda: renamed)

    body = _run(run_mock_pipeline).body
    brands = [tree["brand"] for tree in body["featureTrees"]]

    assert brands == ["示例品牌有限公司"], f"改了写法就被丢掉了：{brands}"


def test_同一品牌两条记录会在报告里点名(run_mock_pipeline, monkeypatch) -> None:
    """出口上的绊线：重复真的漏到报告里时，报告要多一句话。

    **注入的是那个前提条件本身**——`_structured_brands` 返回两个同名品牌，
    于是两个请求真的产出了两棵同名的树。不去伪造一个 `ctx.feature_trees`
    是因为那样只测到"函数会不会数重复"，测不到"它接在流水线的哪一步上"：
    绊线如果挂在 `assemble()` 之后，这条用例会红，而伪造的会绿。
    """
    monkeypatch.setattr(analyze, "_structured_brands", lambda ctx: ["示例品牌", "示例品牌"])

    body = _run(run_mock_pipeline).body
    brands = [tree["brand"] for tree in body["featureTrees"]]

    assert brands == ["示例品牌", "示例品牌"], f"没造出重复，这条用例没测到东西：{brands}"
    line = next(
        (line for line in body["coercion"]["repairs"] if "不止一次" in line), None
    )
    assert line is not None, (
        f"重复进了报告而一个字都没说，页面上会少一张卡：{body['coercion']['repairs']}"
    )
    assert "功能对比" in line and "示例品牌" in line, f"那句话没说清是哪一块：{line}"


def test_正常产出时绊线不响(run_mock_pipeline) -> None:
    """反向守卫：别把它写成"只要有两块内容就报"。

    份额那种**合法的**同品牌两行（不同机构口径）走的是 `marketShare`，
    它不在绊线的名单里；这一条同时确认正常的两个品牌不会被点名。
    """
    body = _run(run_mock_pipeline).body

    assert [tree["brand"] for tree in body["featureTrees"]] == ["示例品牌", "对照品牌"]
    assert not [line for line in body["coercion"]["repairs"] if "不止一次" in line], (
        f"正常产出被点名了：{body['coercion']['repairs']}"
    )
