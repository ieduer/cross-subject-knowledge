# 实虚词典实施说明

## 当前上线状态（2026-03-11）

`/dict.html` 当前已经不是单一“查典页”，而是三模式并存：

- `查典`
- `真题虚词`
- `真题实词`

其中：

- `查典` 模式继续保留教材 / 馆藏辞典原页 / 真题 / AI 学习助手主链路
- `查典` 右栏已新增教育部《重编国语辞典修订本》只读结果区
- `真题虚词 / 真题实词` 当前启用的是：
  - 年份分布
  - 年度真题全文展开
  - 教材例句与原图
  - 两本馆藏辞典原图
  - 教育部《重编国语辞典修订本》原文
- 年度真题全文当前已随 `dict_exam_xuci.json / dict_exam_shici.json` 一并打包到运行时数据
  - 不再依赖生产 `gaokao` 库回查全文
  - 可避免全国卷或部分实词条目退化为“只显示片段摘录”
- `2026-03-11 14:05 UTC` 已完成一次基于 clean release 的生产修复
  - 当前线上镜像：`textbook-knowledge:build-36617c8-20260311_140512`
  - 当前前端版本：`2026.03.11-r30`
  - 主站综合搜索的“查看原文”链路已恢复
  - 修复点不是数据回滚，而是把正确的 `frontend/assets/pages/book_map.json` 连同当前前端一起重新装回镜像

本轮同时保留但暂不前端启用：

- `dict_exam_xuci_details.json`
- `GET /api/dict/exam/xuci-detail`

原因是两本馆藏辞典的 OCR 识别质量当前未达到学生端展示标准，待人工校对并提供精确替换版本后再恢复。

## 当前已知风险（2026-03-11）

- `HEAD /dict.html` 当前返回 `405 Method Not Allowed`
  - 页面可用性检查需使用 `GET /dict.html`
- `frontend/assets/dict.js` 内仍保留 OCR 详情层的旧渲染函数
  - 当前页面没有挂载点，也没有调用链，不会对学生端生效
  - 后续若恢复 OCR 层，必须以人工校对后的替换数据重新验收，不能直接重新接回旧 UI
- 线上 `dict_moe_revised.db` 当前文件 SHA 与本地重建版不同
  - 但已核对 `row_count=163916`、`headword_count=161136`，且 `之 / 焉` 抽样内容一致
  - 这说明当前更接近“文件级漂移”而不是用户可见内容漂移
  - 后续若要重发该库，需走显式数据工件同步，不要仅凭采样一致就跳过工件对账

## 结论

`sun.bdfz.net` 已接入一个新的独立页面 `/dict.html`，并新增了对应的后端 API 骨架：

- `GET /api/dict/search`
- `GET /api/dict/moe-revised`
- `GET /api/dict/textbook`
- `GET /api/dict/gaokao`
- `GET /api/dict/exam/xuci`
- `GET /api/dict/exam/shici`
- `GET /api/dict/exam/questions`
- `GET /api/dict/exam/xuci-detail`
- `GET /api/dict/page-images`
- `POST /api/dict/chat`

当前页面、API、教材/真题侧筛选、真题词表区、教育部修订本只读结果区、AI 多轮对话都已就位。

两本馆藏辞典相关的数据建库和接口已存在，但基于 OCR 生成的虚词提要 / 义项 / 思维导图当前不启用。现阶段继续按“学生端优先展示原图和授权原文，不直接展示未校对 OCR 正文”的口径执行。

## 2026-03-07 页面升级方案

本轮升级目标不是推翻已有实现，而是在不影响现有主搜索页和教材 / 真题接口的前提下，把 `/dict.html` 收口为学生可直接使用的“图片优先”版本。

### 不变部分

- 首页 `/` 的综合搜索、真题、数据洞察、知识图谱不动
- `GET /api/dict/textbook` 的教材古文 / 古诗词筛选逻辑不动
- `GET /api/dict/gaokao` 的真题古文 / 古诗词筛选逻辑不动
- `POST /api/dict/chat` 的多轮对话逻辑不动

### 新页面结构

- 首屏新增“检索链路总览”
  - 固定展示 教材 → 馆藏页图 / 官方参考 → 真题 → AI 四步
  - 检索后实时显示每一步是否命中、是否失败、当前证据数
- 左栏继续显示教材中的古文 / 古诗词命中
- 右栏上半部改为“馆藏辞典原页”
  - 学生只看字头页图与页码
  - 不再直接显示 OCR 文字条目
- 右栏下半部新增“官方与外部参考”
  - 教育部《重编国语辞典修订本》
  - 教育部《国语辞典简编本》
  - 教育部《异体字字典》（单字时显示）
  - `zi.tools`（单字优先，多字词拆字）
  - 汉语多功能字库（单字优先，多字词拆字）
- 下方继续显示语文真题中的古文 / 古诗词命中
- 最下方继续保留 AI 对话

### 后端接口升级

保留原有 `GET /api/dict/search`，但返回约定改为“页图优先”：

- 新增 `display_mode = "page_images"`
- 新增 `student_safe_only = true`
- 新增 `query_kind = "single_char" | "term"`
- 新增 `source_mode`
  - `headword_page_index`
  - `dict_db`
  - `unavailable`
- 条目新增：
  - `page_numbers`
  - `page_count`
  - `page_urls`

新增接口：

- `GET /api/dict/references?q=...`
  - 返回教育部与外部高质量资源的站内参考卡片
  - 当前阶段先走深链，不直接抓外站正文
- `GET /api/dict/status`
  - 返回当前运行时已核定条数、候选条数与启用源状态
  - 返回 `student_safe_mode = "page_images_only"`
  - 返回 `external_reference_mode = "deep_links"`
  - 前端首页文案与 source chips 直接读取这个状态，而不是写死

### 前端交互降级原则

- 四路请求改为分路容错：
  - 教材
  - 馆藏辞典
  - 外部参考
  - 真题
- 任意一路失败时，只在对应面板显示错误，不拖垮整个 `/dict.html`
- AI 首轮分析只在至少一类核心证据成功加载时自动触发
- 多字词未命中馆藏辞典时，前端明确提示：
  - 先看右侧官方参考
  - 或拆成关键单字继续查

### 内部词典数据契约

为了不让学生直接看到 OCR 误差，学生端优先使用“字头到页图”的轻索引，而不是全文展示。

推荐新增：

- `data/index/dict_headword_pages.json`

建议格式：

```json
{
  "entries": {
    "之": [
      {
        "dict_source": "xuci",
        "display_headword": "之",
        "page_numbers": [188, 189],
        "verified": true
      }
    ]
  }
}
```

运行规则：

- 若存在 `dict_headword_pages.json`，优先返回已核定页图索引
- 若不存在，则回退到 `dictionary_index.db`
- 两者都没有时，词典区显示“索引未导入”，不影响教材、真题和 AI

当前已补充一条完整的离线链路脚本：

- `platform/scripts/build_dict_headword_index.py`

它会生成三层文件：

- `data/index/dict_headword_candidates_xuci.jsonl`
  - 《古代汉语虚词词典》自动抽取的候选字头页
- `data/index/dict_headword_review.tsv`
  - 人工复核表
  - 只有 `verified=1` 的行会进入运行时
- `data/index/dict_headword_pages.json`
  - 前端和 API 直接读取的运行时索引

当前已落地策略：

- `xuci`
  - 用 `pdftotext -raw` 的阅读顺序检测字头起始页
  - 再按“下一个字头起始页”推回完整页码区间
- `changyong`
  - 用王力第 5 版 CSV 作为可信字头顺序底稿
  - 只 OCR 页眉字头列，不 OCR 全页正文
  - 用页眉命中的起始页，结合字头顺序推回完整页码区间

当前构建结果：

- `xuci = 1389` 条可运行时字头页图索引
- `changyong = 5509` 条可运行时字头页图索引
- `changyong` 对唯一单字头的当前覆盖率约 `82.03%`

脚本运行：

```bash
/Users/ylsuen/.venv/bin/python platform/scripts/build_dict_headword_index.py
```

### 外部资源接入判断

教育部辞典：

- 值得接入
- 当前阶段先做官方深链卡片
- 后续阶段建议导入教育部公眾授權 ZIP 做站内镜像，以获得稳定、低延迟、可缓存的结果

`zi.tools`：

- 值得接入，但只作为单字补充层
- 不做硬依赖，不把它作为判定学生答案或字义的核心来源

汉语多功能字库：

- 值得接入，但更适合作为深链
- 当前阶段不抓站，不镜像

### 上线与回滚

上线方式：

- 只改 `dict.html` 相关前后端代码
- 不改首页主视图代码路径
- 不改教材数据库结构

回滚方式：

- 前端回退 `frontend/dict.html`
- 前端回退 `frontend/assets/dict.css`
- 前端回退 `frontend/assets/dict.js`
- 后端回退 `backend/main.py` 中新增的 `/api/dict/references` 与 `page_images` 展示逻辑

## 2026-03-06 质检更新

线上来源复核结论：

- 再次检索后，仍未发现来源清晰、完整度可验证、可直接生产使用的《辞源》开源全文仓库
- 官方能确认的只有商业数字版，而不是可复用全文
- 因此《辞源》当前不能走“直接接入文本源”这条路

本机抽样结论：

- `古代汉语虚词词典.pdf`
  - 正文可从文字层抽取
  - 去掉拼音后，抽样页 `188 / 260` 的字头与正文短语可精确命中
- `古汉语常用字字典 第5版 by 王力.pdf`
  - 去掉拼音后，抽样页 `100` 的正文可局部命中，但字头仍不稳定
  - 这会直接影响词典检索页的字头查找，不适合直接上线

生产决策：

- 《辞源》暂缓，不进入当前上线范围
- 当前默认运行源已切到 `xuci + changyong`
- `xuci` 走文字层起始页索引
- `王力常用字` 走“CSV 顺序底稿 + 页眉 OCR”索引
- 学生端统一只显示原页图片，不显示 OCR 正文

重复质检脚本：

- `scripts/40_dictionary_ocr_qc.py`
- 运行命令：`/Users/ylsuen/.venv/bin/python scripts/40_dictionary_ocr_qc.py`
- 该脚本现在直接核对运行时页码索引与构建期 QC 摘要
- 当前固定样张覆盖：
  - `xuci`：`恭 / 躬 / 躬亲 / 会 / 会当 / 会须 / 正使 / 政 / 之 / 安 / 暗暗 / 不成`
  - `changyong`：`99 / 100 / 101` 页人工复核过的页眉字头样张

## 本机 PDF 实测

来源目录：

- `/Users/ylsuen/Books/books2stu/古汉语常用字字典 第5版 by 王力.pdf`
- `/Users/ylsuen/Books/books2stu/古代汉语虚词词典.pdf`

本机检查结论：

- `古代汉语虚词词典.pdf`
  - `pdfinfo` 显示 921 页
  - `pdffonts` 能读到大量字体
  - `pdftotext -layout` 可直接抽到正文
  - 结论：优先直接抽文字层，不先做 OCR
- `古汉语常用字字典 第5版 by 王力.pdf`
  - `pdfinfo` 显示 659 页
  - `pdffonts` 基本为空
  - 结论：基本是扫描件，需要 OCR
额外观察：

- 两本目标辞典都是双栏辞典版式
- 即便是 `古代汉语虚词词典` 的可抽取文本，也不能直接按整页文本建索引
- 必须保留页码，并按“字头 / 义项 / 引文”重建条目，否则会出现跨栏串行和上下条目粘连

## 公网文本源判断

已优先查找 GitHub / Gitee / 其他可复用的文本化版本。

结论：

- 未发现来源清晰、完整度可验证、可直接生产使用的目标版本全文仓库
- 因此不建议把未知来源 OCR 文本直接并入站点
- 词典数据仍应以本机 PDF 为主源构建

## 运行时契约

词典运行库建议单独放在：

- `data/index/dictionary_index.db`

这样对现有教材检索库 `data/index/textbook_mineru_fts.db` 没有写入风险。

若后续两本达到质检标准，运行库仍建议保持独立：

```sql
CREATE TABLE dict_entries (
  id INTEGER PRIMARY KEY,
  headword TEXT NOT NULL,
  headword_trad TEXT,
  dict_source TEXT NOT NULL,
  entry_text TEXT NOT NULL,
  page_start INTEGER,
  page_end INTEGER,
  sort_order INTEGER DEFAULT 0,
  page_urls_json TEXT
);
```

`dict_source` 约定：

- `changyong`
- `xuci`
默认启用源：

- `xuci`

排序约定：

- `changyong = 1`
- `xuci = 2`
- `ciyuan = 3` 仅保留给未来扩展，当前不启用

## 页面与 API 设计

### 1. `GET /api/dict/search`

返回当前已启用辞典的统一字头结果。

排序规则：

1. 头字精确命中
2. 同义字头 / 繁体字头命中
3. 条目正文命中
4. 固定顺序：按当前启用源返回；现阶段默认只有 `虚词`

返回字段重点：

- `headword`
- `headword_trad`
- `dict_source`
- `dict_label`
- `entry_text`
- `page_start`
- `page_end`
- `page_url`

### 2. `GET /api/dict/textbook`

只在现有教材库中查：

- `subject = '语文'`
- `source = 'mineru'`

并且额外做“古文 / 古诗词”过滤。

当前实现：

- 先按查询词做全文命中
- 再走古典文体启发式过滤
- 如果后续补了 `data/index/textbook_classics_manifest.json`，则优先使用该清单按页范围精确过滤

推荐后续补充的清单格式：

```json
{
  "高中_语文_普通高中教科书_语文必修_上册": [
    {
      "title": "赤壁赋",
      "kind": "古文",
      "page_start": 124,
      "page_end": 126
    }
  ]
}
```

### 3. `GET /api/dict/gaokao`

只在真题库中查：

- `source = 'gaokao'`
- `subject = '语文'`

并优先保留标题或分类中带以下提示的结果：

- `文言`
- `文言文`
- `古文`
- `古诗`
- `古诗文`
- `诗歌鉴赏`

### 4. `POST /api/dict/chat`

前端会把以下三块证据打包送入 Worker AI：

- 当前启用词典条目摘要
- 教材中古文 / 古诗词命中
- 真题中古文 / 古诗词命中

多轮对话记忆由前端 `history` 继续回传，后端不额外存储会话状态。

## 推荐离线建库流程

### Phase A. 页级抽取

目标产物：

- `data/dict_output/changyong/pages.jsonl`
- `data/dict_output/xuci/pages.jsonl`
建议策略：

- `古代汉语虚词词典`
  - 先走 `pdftotext -layout`
  - 每页存一条 JSONL
- `古汉语常用字字典`
  - 先走 MinerU
  - 以 `*_content_list.json` 的 `page_idx` 作为页级单元
  - 不要只用 Markdown 串页文本

页级 JSONL 建议字段：

```json
{
  "dict_source": "xuci",
  "page": 188,
  "method": "text_layer",
  "text": "..."
}
```

### Phase B. 条目重建

从页级 JSONL 解析为 `dict_entries.jsonl`。

建议处理：

- 按字头行拆条
  - 识别义项编号
  - 保留完整条目正文
  - 记录跨页范围 `page_start/page_end`

### Phase C. 词典页图

页图统一渲染到：

- `pages/dict_changyong/p{N}.webp`
- `pages/dict_xuci/p{N}.webp`

这样与现有教材页图 CDN 约定一致。

### Phase D. R2 / CDN

上传后前端直接访问：

- `https://img.rdfzer.com/pages/dict_changyong/p123.webp`
- `https://img.rdfzer.com/pages/dict_xuci/p188.webp`

## OCR 引擎建议

首选：

- 继续沿用本项目已经落地的 MinerU 工作流

原因：

- 现有仓库已经有稳定的 MinerU 批处理路径
- 已经适配 `content_list.json -> page_idx -> 索引` 这一套管线
- 对后续页码、页图、R2 上传复用成本最低

保守建议：

- `虚词` 可以先走文本层建库
- `王力常用字` 继续只做抽样 OCR 质检
- 在字头稳定前，不进入学生可见范围

## 本次已落地的代码

前端：

- `platform/frontend/dict.html`
- `platform/frontend/assets/dict.css`
- `platform/frontend/assets/dict.js`
- `platform/frontend/index.html`
- `platform/frontend/assets/app.js`
- `platform/frontend/assets/style.css`

后端：

- `platform/backend/main.py`

## 本地验证

开发环境：

```bash
cd /Users/ylsuen/textbook_ai_migration/platform
python -m compileall backend/main.py
```

若本地跑 FastAPI：

```bash
cd /Users/ylsuen/textbook_ai_migration/platform
uvicorn backend.main:app --reload --port 8080
```

接口检查：

```bash
curl -s "http://127.0.0.1:8080/api/dict/textbook?q=之&limit=5"
curl -s "http://127.0.0.1:8080/api/dict/gaokao?q=之&limit=5"
curl -s "http://127.0.0.1:8080/api/dict/search?q=之&limit=5"
curl -s "http://127.0.0.1:8080/api/dict/page-images?dict_source=xuci&page=188&context=2"
```

浏览器检查：

- 打开 `http://127.0.0.1:8080/dict.html`
- 搜索 `之`
- 确认左侧只出现教材古文 / 古诗词命中
- 确认右侧只返回当前启用并通过质检的词典源
- 点击教材 / 词典按钮可看页图
- AI 首轮会自动生成学习建议，之后支持多轮追问和复制全文

## 回滚

这次改动是纯新增和小范围挂接，回滚非常直接。

保留新增页面但停用入口：

- 删除 `index.html` 里的 `实虚词典` 导航链接

完全回滚页面：

- 删除 `platform/frontend/dict.html`
- 删除 `platform/frontend/assets/dict.css`
- 删除 `platform/frontend/assets/dict.js`
- 删除 `main.py` 中 `/dict.html` 和 `/api/dict/*` 路由

如果词典数据库导入有问题：

- 直接移走 `data/index/dictionary_index.db`
- 页面仍可打开，但右侧词典区会显示“词典数据尚未导入”
