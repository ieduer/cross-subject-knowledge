# 实虚词典页面升级方案（2026-03-11）

## 结论

`/dict.html` 不应继续只做“输入一个字词 -> 看教材 / 真题 / 馆藏页图”的检索页。

如果要满足“北京 + 全国高考语文文言文真题中的虚词 / 实词系统梳理”这个目标，页面必须升级为一个 **考表驱动、查典补强、教材例证回落** 的专题页：

- 第一层对象不是“词典条目”，而是“高考考过什么”
- 第二层对象才是“这个虚词 / 实词的详细解释与证据”
- 虚词做全链路：
  - 考表
  - 频次
  - 北京 / 全国标签
  - 可视化
  - 两本辞典精确 OCR
  - 用法 / 意义拆解
  - 教材经典例句
  - 思维导图
- 实词只做：
  - 考表
  - 频次
  - 北京 / 全国标签
  - 可视化
  - 真题证据回看
- 实词本轮不接辞典、不做思维导图

## 当前启用状态说明（2026-03-11 更新）

- 两本馆藏辞典的 OCR 提要、义项拆解、思维导图运行时数据与接口均已保留
- 但因 OCR 识别质量当前不满足上线要求，前端已暂时停用这部分内容
- 当前线上真题虚词 / 真题实词详情页只启用：
  - 年份分布
  - 年度真题全文展开
  - 教材例句与原图
  - 两本馆藏辞典原图
  - 教育部《重编国语辞典修订本》原文
- 年度真题全文当前已直接随运行时考表数据打包
  - 不再依赖生产 `gaokao` 数据库补拉全文
  - 这样可以避免全国卷与部分实词条目退化成片段摘要
- 后续待人工校对并提供精确替换版本后，再恢复 OCR 义项层展示

## 发布后已知风险（2026-03-11）

- `HEAD /dict.html` 当前返回 `405 Method Not Allowed`
  - 页面探针应改为 `GET`
- OCR 详情层前端代码仍保留在 `dict.js` 中，但当前未挂载、未启用
  - 在人工校对版交付前，不应恢复旧 OCR 详情 UI

## 当前数据现实（基于本地仓库，2026-03-11）

### 已有

- `data/index/gaokao_chunks.jsonl`
  - 当前运行时只有 **北京卷语文古文题**
  - 可见 `2002-2025` 共 `24` 题
- 北京古文题里，当前能直接识别的题型大致有：
  - `加点词解释 / 词语解说`：`16` 题
  - `加点词意义和用法都相同`：`5` 题
  - `加点词意义和用法不同`：`4` 题
- `data/index/dict_headword_pages.json`
  - 已有《古代汉语虚词词典》与《古汉语常用字字典》的字头到页图索引
- `platform/backend/main.py`
  - 已有 `/api/dict/search`
  - 已有 `/api/dict/textbook`
  - 已有 `/api/dict/gaokao`
  - 已有 `/api/dict/page-images`

### 未有

- 当前运行时 **没有全国卷语文古文题** 的标准化索引
- 因此现在的页面实际上无法稳定做：
  - 北京 / 全国双标签
  - 北京 / 全国频次对比
  - 全国口径的虚词 / 实词考表

### 可补源

- `data/gaokao_raw/GAOKAO-Bench/Data/Subjective_Questions/2010-2022_Chinese_Language_Classical_Chinese_Reading.json`
  - 本地已有全国卷文言文阅读原始题源
  - 当前看到 `29` 条
  - 但尚未标准化进运行时检索库
- `data/gaokao_raw/GAOKAO-Bench-Updates`
  - 当前未见对应的 `Classical_Chinese_Reading` 更新文件
  - 说明 `2023-2025` 的全国卷文言文题还需要单独补源 / 归档

结论很直接：

- 北京数据：可先做
- 全国数据：先补标准化，再上页

可复核脚本：

```bash
/Users/ylsuen/.venv/bin/python platform/scripts/audit_shixuci_exam_sources.py
```

## 产品目标重构

页面从“查词页”改成“双模式”最稳：

1. `考表总览`
2. `按词查典`

这样能保留现有 `/dict.html` 的可用能力，不需要一次性推翻已有检索页。

### 模式一：考表总览

这是新页面主入口，承担以下任务：

- 展示虚词考表
- 展示实词考表
- 展示北京 / 全国对比
- 展示频次和年度分布
- 提供按词点击进入详情

### 模式二：按词查典

保留当前检索能力，但要降成辅助入口：

- 查某个虚词时：
  - 优先展示该词的考情摘要
  - 再展示辞典页图 / OCR 义项 / 教材例句 / 思维导图
- 查某个实词时：
  - 只展示考情摘要 + 真题证据 + 教材例句
  - 不接辞典解释和思维导图

## 页面信息架构

建议信息架构如下。

### 1. 顶部总览区

- 标题：`高考文言实虚词`
- 子说明：明确口径是“北京卷 + 全国卷语文文言文真题”
- 统计卡片：
  - 虚词总数
  - 实词总数
  - 北京卷覆盖年份
  - 全国卷覆盖年份
  - 已完成辞典 OCR 的虚词数
- 模式切换：
  - `考表总览`
  - `按词查典`

### 2. 虚词总览区

这是页面第一主屏。

- 左侧：
  - 虚词考频榜
  - 支持北京 / 全国 / 全部切换
  - 支持按：
    - 总频次
    - 北京频次
    - 全国频次
    - 最近五年频次
    - 覆盖年份数
- 右侧：
  - 可视化面板
  - 默认三张图：
    - 横向频次条形图
    - 北京 / 全国堆叠对比图
    - 年份热力图

### 3. 虚词详情区

点击任一虚词进入详情。

详情区分六块：

- `考情摘要`
  - 总频次
  - 北京频次
  - 全国频次
  - 首次出现年份
  - 最近一次出现年份
  - 典型题型
- `真题证据`
  - 列出所有相关题目
  - 每条标注：
    - 年份
    - 北京 / 全国
    - 卷别
    - 题型
    - 原题片段
    - 加点词位置
- `辞典义项`
  - 两本辞典的精确 OCR 结果合并后展示
  - 每条义项拆成：
    - 用法
    - 意义
    - 辞典来源
    - 页码
- `教材例证`
  - 每个义项向下挂教材文言文经典例句
  - 每条例句标明教材篇目
- `思维导图`
  - 结构必须是：
    - 虚词
    - 用法（词性）
    - 意义（表示何种关系）
    - 教材例句
    - 对应真题年份 / 地区
- `馆藏页图`
  - 保留现有辞典页图查看器

### 4. 实词总览区

这是第二主屏。

- 实词考频榜
- 北京 / 全国筛选
- 按年份 / 频次 / 覆盖卷别排序
- 可视化与虚词一致：
  - 横向频次条形图
  - 北京 / 全国堆叠对比图
  - 年份热力图

### 5. 实词详情区

点击任一实词进入详情。

只展示：

- 考情摘要
- 真题证据
- 教材内经典例句
- 如有必要可加“近义区分提示”

不展示：

- 辞典 OCR
- 思维导图

## 数据层设计

## 1. 真题抽取层

先把“考表”做成独立数据层，而不是运行时现搜现判。

建议新增以下运行时资产。

### 虚词

- `data/index/xuci_exam_occurrences.jsonl`
- `data/index/xuci_exam_summary.json`

### 实词

- `data/index/shici_exam_occurrences.jsonl`
- `data/index/shici_exam_summary.json`

### 公共题源清单

- `data/index/wenyan_exam_questions.jsonl`

### 推荐字段

`wenyan_exam_questions.jsonl`

```json
{
  "question_id": "beijing_2024_guwen_24_q7",
  "source_scope": "beijing",
  "year": 2024,
  "paper": "北京卷",
  "question_type": "古文",
  "subtype": "xuci_compare_same",
  "passage_title": "墨子",
  "stem": "下列各组语句中，加点词的意义和用法都相同的一组是",
  "raw_text": "..."
}
```

`xuci_exam_occurrences.jsonl`

```json
{
  "question_id": "beijing_2024_guwen_24_q7",
  "source_scope": "beijing",
  "year": 2024,
  "paper": "北京卷",
  "headword": "之",
  "token_raw": "之",
  "option_label": "A",
  "pair_index": 1,
  "passage_excerpt": "下以阻百姓之从事 / 是灭天下之人也",
  "question_subtype": "xuci_compare_same",
  "is_answer_option": true,
  "source_ref": "gaokao_gknet_2024_guwen_24"
}
```

`shici_exam_occurrences.jsonl`

```json
{
  "question_id": "beijing_2025_guwen_187_q6",
  "source_scope": "beijing",
  "year": 2025,
  "paper": "北京卷",
  "headword": "处",
  "token_raw": "处",
  "option_label": "A",
  "passage_excerpt": "临患涉难而处义不越",
  "gloss": "坚守",
  "question_subtype": "shici_explanation",
  "is_answer_option": false,
  "source_ref": "gaokao_gknet_2025_guwen_187"
}
```

## 2. 题型归一化

不能只按单一句式抽取，要统一归类。

建议至少归一到以下类型：

- `xuci_compare_same`
  - 例如“意义和用法都相同”
- `xuci_compare_diff`
  - 例如“意义和用法不同”
- `shici_explanation`
  - 例如“加点词的解释不正确”
- `shici_gloss_commentary`
  - 例如“加点词语的解说”

页面统计时：

- 虚词考表使用 `xuci_compare_same + xuci_compare_diff`
- 实词考表使用 `shici_explanation + shici_gloss_commentary`

## 3. 辞典 OCR 层

只对“考过的虚词”做定向精确 OCR，不做全书粗 OCR。

### 来源

- 《古代汉语虚词词典》
- 《古汉语常用字字典》

### 原则

- 先用 `dict_headword_pages.json` 定位字头页
- 再做页内分栏和目标区域 OCR
- 结果不直接上线，先过人工校对表

### 新增资产

- `data/index/xuci_dict_ocr_segments.jsonl`
- `data/index/xuci_knowledge_base.json`
- `data/index/xuci_ocr_review.tsv`

### `xuci_knowledge_base.json` 推荐结构

```json
{
  "之": {
    "headword": "之",
    "exam_stats": {
      "total": 12,
      "beijing": 5,
      "national": 7
    },
    "usages": [
      {
        "usage_label": "代词",
        "sense_label": "代人、事、物",
        "dict_sources": [
          {
            "dict_source": "xuci",
            "page_numbers": [844]
          }
        ],
        "textbook_examples": [
          {
            "title": "劝学",
            "quote": "青，取之于蓝，而青于蓝"
          }
        ]
      }
    ]
  }
}
```

## 4. 教材例句层

教材例句不能简单全文检索命中后直接挂上去，要做“义项级挂接”。

建议拆两步：

1. 自动候选
   - 从教材文言文清单中检索同词出现位置
2. 人工归义
   - 把候选例句挂到具体义项

新增资产：

- `data/index/xuci_textbook_examples.json`
- `data/index/shici_textbook_examples.json`

## 5. 思维导图层

思维导图不要运行时临时拼。

应提前产出结构化树：

- `data/index/xuci_mindmaps.json`

推荐树结构：

- 第一层：虚词
- 第二层：用法（词性）
- 第三层：意义（表示何种关系）
- 第四层：教材经典例句
- 第五层：高考证据点

## 抽取策略

## 1. 北京卷

北京卷优先用当前运行时库：

- `data/index/gaokao_chunks.jsonl`

原因：

- 字段已标准化
- 已有 `year / category / region / question_type / title / text`
- 可直接形成第一版考表

## 2. 全国卷

全国卷应分两段补齐：

### 第一段：先接现成本地原始题

- `GAOKAO-Bench`
- 当前至少能覆盖 `2010-2022`

### 第二段：补 `2023-2025`

因为本地 `GAOKAO-Bench-Updates` 当前未见文言文阅读更新文件，所以需要补源。

建议补源方式：

- 优先复用现有高考抓取 / OCR 脚本
- 补入统一的 `wenyan_exam_questions.jsonl`
- 明确标注为：
  - `source_scope = national`
  - `source_stage = supplemental_manual`

## 3. 加点词抽取规则

抽取时要以“全部加点词”为主，而不是只记录正确答案。

因此每道题至少要保留：

- 所有选项里的加点词
- 正误标记
- 题干题型
- 原文上下文

这是后续做频次、题型分布、误判对比和思维导图回链的基础。

## 页面可视化设计

不建议做词云，信息密度太低。

建议固定四类图：

### 虚词

- `Top N` 横向条形图
  - 看总频次
- 北京 / 全国堆叠条形图
  - 看区域差异
- 年份热力图
  - 看持续性
- 义项分布图
  - 看某个虚词常考哪种用法 / 关系

### 实词

- `Top N` 横向条形图
- 北京 / 全国堆叠条形图
- 年份热力图
- 高频实词题型分布图

## API 设计

现有 API 不够，需要新增专题接口。

建议新增：

- `GET /api/dict/exam/overview`
  - 返回全局统计
- `GET /api/dict/exam/xuci`
  - 返回虚词榜单
- `GET /api/dict/exam/shici`
  - 返回实词榜单
- `GET /api/dict/exam/xuci/{headword}`
  - 返回某个虚词详情
- `GET /api/dict/exam/xuci/{headword}/mindmap`
  - 返回思维导图树
- `GET /api/dict/exam/shici/{headword}`
  - 返回某个实词详情
- `GET /api/dict/exam/questions`
  - 返回题目证据列表

现有接口保留：

- `/api/dict/search`
- `/api/dict/textbook`
- `/api/dict/gaokao`
- `/api/dict/page-images`

但它们从“主接口”降为“辅助接口”。

## 前端落地建议

## 1. 不直接推翻当前页

最稳妥的前端策略是：

- 第一阶段保留现有查询框
- 查询框上方新增模式切换
- 默认进入 `考表总览`
- `按词查典` 仍可使用当前交互

## 2. 首屏默认内容

首屏不要再是几个词的 suggestion chip。

默认应该直接展示：

- 虚词榜单前 `10`
- 实词榜单前 `10`
- 北京 / 全国筛选
- 近年趋势图

## 3. 详情区交互

点击榜单词条后：

- 右侧详情区联动
- 不跳页
- URL hash 可更新为 `#xuci-之` / `#shici-道`

这样便于分享和回看。

## 开发顺序

建议按四期做。

### 第一期：补全题源与考表

- 先把北京卷现有题抽出来
- 再把全国卷 `2010-2022` 标准化
- 形成虚词 / 实词 occurrence 层和 summary 层

交付标准：

- 页面能看见北京 / 全国标签
- 页面能看见虚词 / 实词榜单和频次

### 第二期：虚词辞典 OCR 与义项库

- 只处理“考过的虚词”
- 建立人工复核链路
- 生成义项库

交付标准：

- 点击虚词能看到可靠的用法 / 意义说明

### 第三期：教材例句与思维导图

- 给每个虚词义项挂教材例句
- 产出思维导图树

交付标准：

- 每个高频虚词都有完整知识卡

### 第四期：交互收口与质检

- 前端模式切换
- 图表联动
- 错题回看
- 词条锚点
- 移动端排版

## 验收标准

至少应满足以下标准再上线：

- 北京卷虚词考表可回溯到每一道原题
- 北京卷实词考表可回溯到每一道原题
- 全国卷题源覆盖范围在页面上明确写清楚
- 虚词频次统计以 occurrence 计数，不以词典条目计数
- 每个虚词详情都能看到：
  - 考情
  - 义项
  - 教材例句
  - 思维导图
  - 馆藏页图
- 实词详情不混入辞典和思维导图

## 风险与控制

### 风险 1：全国题源口径不全

控制：

- 页面显式写明覆盖年份
- 不把北京数据冒充全国全量

### 风险 2：辞典 OCR 误差污染义项

控制：

- 只做定向 OCR
- 先 review TSV
- 学生端只展示复核通过内容

### 风险 3：教材例句误挂义项

控制：

- 自动候选 + 人工归义
- 例句条数宁少勿乱

### 风险 4：页面过重

控制：

- 榜单接口和详情接口拆开
- 图表先摘要加载
- 思维导图按需加载

## 回滚

回滚要尽量简单：

- 保留现有 `/dict.html` 检索能力
- 新专题能力挂在 feature flag 或 mode 切换后
- 若新接口不稳定：
  - 只关闭 `考表总览`
  - 保留 `按词查典`

## 本轮建议的最小落地范围

如果只做最小正确版本，建议先完成这四件事：

1. 北京卷虚词考表
2. 北京卷实词考表
3. 全国卷 `2010-2022` 文言文题标准化接入
4. 高频虚词前 `20` 的辞典 OCR + 义项库

这样页面就已经从“词典搜索页”升级成“可真正服务文言复习的考情词典页”了。
