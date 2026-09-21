/**
 * 正文角标：这一侧的那条测试。
 *
 * 同一件事在两边各有一份实现——后端 `report/citation_index.py` 与
 * 这里——所以两份**必须**给出同一个编号，否则页面上写 `[3]` 的那句话
 * 在导出的 Markdown 里指向另一条证据，而两处看起来都很正常。
 *
 * 那条等价性没法在这里直接跑（跨语言），所以做法是：把两边的规则
 * 逐条写成用例。下面每组都标了它对应用后端的哪一条，后端有一份
 * `tests/integration/test_report_citations.py` 守着同一批规则。
 *
 * 为什么 `citationNumbers` 要分两条路（读存的 / 现算）
 * ------------------------------------------------
 * 旧报告没有 `citations` 键。退回现算的规则必须与后端**逐字相同**，
 * 所以它在下面有一条和"读存的"并列的用例，而不是只测一条路。
 */
import { describe, expect, it } from 'vitest'

import {
  MARKER,
  citationNumbers,
  citedEvidences,
  evidenceIds,
  proseCitedIds,
  splitCitations,
  unresolvedCitations,
} from './reportCitation'
import type { ReportBody, ReportSection } from '../types/report'

/** 只手写用得到的键。这份类型全是可选的，所以它是一份合法的 `ReportBody`。 */
function body(partial: Partial<ReportBody>): ReportBody {
  return partial
}

/**
 * 一节。只写 `content`——被这两组用例调用的函数都只读它。
 *
 * 另外四个键必须补齐。它们是必填的，因为后端 `ReportSection.to_dict()`
 * 总是发全七个（`claimIds` / `evidenceIds` / `degraded` / `reworked`）——
 * 类型是**照着写入方**写的，不是照着"我这次用得到几个字段"写的。
 * 后者会让类型慢慢长成一份只描述测试夹具的谎言，而报告页按它渲染时
 * 才发现字段根本不存在。
 */
function sec(key: string, content: string): ReportSection {
  return { key, title: key, content, claimIds: [], evidenceIds: [], degraded: false, reworked: false }
}

const EVIDENCES: ReportBody['evidences'] = [
  { evidenceId: 'EV-a' } as never,
  { evidenceId: 'EV-b' } as never,
  { evidenceId: 'EV-c' } as never,
  { evidenceId: 'EV-d' } as never,
]

// ============================================================
// 标记的识别
// ============================================================


describe('引用标记', () => {
  it('半角与全角冒号都认', () => {
    // 后端 MARKER 是 `\[证据[::]...`，也就是这两者。漏掉全角那种的话，
    // 一批角标会原样显示成 `[证据: EV-x]`——难看，而且点不开。
    expect([...'看这里[证据: EV-a]'.matchAll(MARKER)].map((m) => m[1])).toEqual(['EV-a'])
    expect([...'看这里[证据：EV-a]'.matchAll(MARKER)].map((m) => m[1])).toEqual(['EV-a'])
  })

  it('冒号后的空格不算进 id', () => {
    expect([...'[证据:   EV-a  ]'.matchAll(MARKER)].map((m) => m[1])).toEqual(['EV-a'])
  })

  it('普通方括号不被当成引用', () => {
    // 正文里出现 `[1]`、`[注]` 之类的方括号是常事（模型会写）。
    expect([...'见[1]与[注]'.matchAll(MARKER)]).toHaveLength(0)
  })

  it('同一个正则可以被反复使用', () => {
    // 带 `g` 的正则是有状态的：如果实现里用了 `.test()` 或 `.exec()`，
    // 第二次调用会因为 `lastIndex` 没归零而漏掉匹配。
    // 这条用例就是钉住"两处调用结果一致"。
    const text = '[证据: EV-a]与[证据: EV-b]'
    expect([...text.matchAll(MARKER)].map((m) => m[1])).toEqual(['EV-a', 'EV-b'])
    expect([...text.matchAll(MARKER)].map((m) => m[1])).toEqual(['EV-a', 'EV-b'])
  })
})

// ============================================================
// 编号：读存的
// ============================================================


describe('编号取自报告里存的那份', () => {
  it('存了就用存的', () => {
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        // **存的编号与正文顺序故意相反**：正文里 EV-a 在前，而存的编号
        // 说 EV-b 是 1。写反了的话现算与读存的给出同一个答案，
        // 这条用例就什么都证明不了（第一版就是这么写的，跑绿之后
        // 才发现它其实没在测"用了哪一份"）。
        citations: [
          { number: 1, evidenceId: 'EV-b' },
          { number: 2, evidenceId: 'EV-a' },
        ],
        sections: [sec('executive_summary', '[证据: EV-a][证据: EV-b]')],
      }),
    )
    expect(numbers.get('EV-a')).toBe(2)
    expect(numbers.get('EV-b')).toBe(1)
  })

  it('存的编号里指向不存在证据的那些被丢掉', () => {
    // 收下的话，页面上会出现一个能点的角标，点开什么都没有。
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        citations: [
          { number: 1, evidenceId: 'EV-a' },
          { number: 2, evidenceId: 'EV-不存在' },
        ],
      }),
    )
    expect([...numbers.keys()]).toEqual(['EV-a'])
  })
})

// ============================================================
// 编号：退回现算（旧报告）
// ============================================================


describe('没有 citations 时按正文顺序现算', () => {
  it('按首次出现顺序编号，而不是按证据表顺序', () => {
    // 与后端 build_citations 是同一条规则。按证据表编的话，
    // [1] 会是正文里可能根本没提到的第一条证据。
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        sections: [
          sec('executive_summary', '先说乙[证据: EV-b]，再说甲[证据: EV-a]。'),
          sec('conclusion', '只看丙[证据: EV-c]。'),
        ],
      }),
    )
    expect([...numbers.entries()]).toEqual([
      ['EV-b', 1],
      ['EV-a', 2],
      ['EV-c', 3],
    ])
  })

  it('同一证据被引多次只占一个号', () => {
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        sections: [
          sec('a', '[证据: EV-a] 又一次 [证据: EV-a]'),
          sec('b', '[证据: EV-b]'),
        ],
      }),
    )
    expect([...numbers.entries()]).toEqual([
      ['EV-a', 1],
      ['EV-b', 2],
    ])
  })

  it('不存在的证据不占号', () => {
    // 占了的话，后面的编号会整体推后一位：一处看得见的坏
    // （一个 [?]）扩散成一片看不见的坏（所有编号都位移）。
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        sections: [sec('a', '[证据: EV-不存在] 然后 [证据: EV-a]')],
      }),
    )
    expect([...numbers.entries()]).toEqual([['EV-a', 1]])
  })

  it('citations 是空数组时也退回现算', () => {
    // 后端给的是 `[]` 而不是缺键的情况要一起处理——`stored.length > 0`
    // 这个条件写漏了的话，页面上一份有引用的报告会一个角标都不显示。
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        citations: [],
        sections: [sec('a', '[证据: EV-a]')],
      }),
    )
    expect([...numbers.keys()]).toEqual(['EV-a'])
  })

  it('存的那份不全时，正文引到而没编号的接在最大号之后', () => {
    // **这条是给已有的坏数据兜底的。** 2026-09-19 之前「深化本节」
    // 不重发编号（后端 `refine._recompute` 漏了这一步，已修），
    // 所以库里那些被深化过的旧报告，`citations` 里没有新采到的证据。
    // 不续号的话，那条引用会显示成 `[?]`——而它明明是一张打得开的卡片。
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        citations: [
          { number: 1, evidenceId: 'EV-a' },
          { number: 2, evidenceId: 'EV-b' },
        ],
        // 深化重写过的那一节引到了一条新证据。
        sections: [sec('executive_summary', '新的[证据: EV-c]，旧的[证据: EV-a]')],
      }),
    )
    expect([...numbers.entries()]).toEqual([
      ['EV-a', 1],
      ['EV-b', 2],
      ['EV-c', 3],
    ])
  })

  it('续号从已有的最大号起算，而不是从个数起算', () => {
    // 与后端 `_CitationIndex._next` 是同一条规则（`max(已有) + 1`）。
    //
    // 种子有缺口时才分得出这两条规则：个数是 2，最大号是 5。
    // 按个数算的话新证据拿到 3，**和第 3 个编号撞车**——
    // 两句话的角标指向同一条证据，而页面上看起来完全正常。
    //
    // 种子是真的 `build_citations` 发出来的话不会有缺口（它按 1,2,3… 发），
    // 所以这条钉的是**规则**本身，而不是某条能走到的路径——
    // 和这个文件里别的几条跨语言等价性用例同一个性质。
    const numbers = citationNumbers(
      body({
        evidences: EVIDENCES,
        citations: [
          { number: 1, evidenceId: 'EV-a' },
          { number: 5, evidenceId: 'EV-b' },
        ],
        sections: [sec('a', '[证据: EV-c]')],
      }),
    )
    expect(numbers.get('EV-c')).toBe(6)
  })
})

// ============================================================
// 拆片段
// ============================================================


describe('把正文拆成文本与角标', () => {
  const numbers = new Map([
    ['EV-a', 1],
    ['EV-b', 2],
  ])

  it('文本与角标按顺序交替', () => {
    const segments = splitCitations('开头[证据: EV-a]中间[证据: EV-b]结尾', numbers)
    expect(segments).toEqual([
      { kind: 'text', text: '开头' },
      { kind: 'citation', text: '[证据: EV-a]', evidenceId: 'EV-a', number: 1 },
      { kind: 'text', text: '中间' },
      { kind: 'citation', text: '[证据: EV-b]', evidenceId: 'EV-b', number: 2 },
      { kind: 'text', text: '结尾' },
    ])
  })

  it('没有标记时原样一段', () => {
    expect(splitCitations('一句普通的话。', numbers)).toEqual([
      { kind: 'text', text: '一句普通的话。' },
    ])
  })

  it('空正文拆出空数组', () => {
    expect(splitCitations('', numbers)).toEqual([])
  })

  it('标记在开头与结尾时不产生空文本片段', () => {
    // 产生空片段的话，渲染出来是一堆没用的 <span>，
    // 而 `key` 也会因此不稳定。
    const segments = splitCitations('[证据: EV-a]', numbers)
    expect(segments).toEqual([
      { kind: 'citation', text: '[证据: EV-a]', evidenceId: 'EV-a', number: 1 },
    ])
  })

  it('找不到编号的角标 number 是 null', () => {
    // 页面据此显示 [?] 并给一个"这条引用找不到"的提示。
    // 悄悄当成普通文本的话，读者不会知道这里曾经有一条引用。
    const segments = splitCitations('[证据: EV-不存在]', numbers)
    expect(segments).toEqual([
      {
        kind: 'citation',
        text: '[证据: EV-不存在]',
        evidenceId: 'EV-不存在',
        number: null,
      },
    ])
  })
})

// ============================================================
// 给面板用的派生数据
// ============================================================


describe('派生数据', () => {
  it('被引证据按编号排序，且只含正文引到的', () => {
    const report = body({
      evidences: EVIDENCES,
      citations: [
        { number: 1, evidenceId: 'EV-c' },
        { number: 2, evidenceId: 'EV-a' },
      ],
      sections: [sec('a', '[证据: EV-a]')],
    })
    expect(citedEvidences(report).map((item) => item.evidenceId)).toEqual(['EV-c', 'EV-a'])
  })

  it('正文里指向不存在证据的引用被单独数出来', () => {
    const report = body({
      evidences: EVIDENCES,
      sections: [sec('a', '[证据: EV-a][证据: EV-幽灵][证据: EV-幽灵]')],
    })
    // 按出现次数（两次），不是去重后的一次：页面上要说的是
    // "有几处引用点不开"，而不是"有几种坏的 id"。
    expect(unresolvedCitations(report)).toEqual(['EV-幽灵', 'EV-幽灵'])
    expect(proseCitedIds(report)).toEqual(['EV-a', 'EV-幽灵'])
  })

  it('证据表为空时一切都不炸', () => {
    // 一份装配失败的残报告会走到这里。抛异常的话整页白屏，
    // 而那时用户连"报告是坏的"这句话都看不到。
    const empty = body({})
    expect(evidenceIds(empty)).toEqual([])
    expect(citationNumbers(empty).size).toBe(0)
    expect(citedEvidences(empty)).toEqual([])
    expect(unresolvedCitations(empty)).toEqual([])
  })
})
