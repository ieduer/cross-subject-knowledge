# 实虚词典上线前检查单

## 当前结论

- 代码链路已基本打通：
  - `/dict.html`
  - `GET /api/dict/status`
  - `GET /api/dict/search`
  - `GET /api/dict/moe-revised`
  - `GET /api/dict/exam/xuci`
  - `GET /api/dict/exam/shici`
  - `GET /api/dict/exam/questions`
  - `GET /api/dict/exam/xuci-detail`
  - `GET /api/dict/textbook`
  - `GET /api/dict/gaokao`
  - `GET /api/dict/references`
  - `GET /api/dict/page-images`
  - `POST /api/dict/chat`
- 运行时页图索引已生成：
  - `xuci = 1389`
  - `changyong = 5509`
- 当前固定样张质检通过：
  - `xuci = 12 / 12`
  - `changyong = 23 / 23`
- `changyong` 当前唯一单字头覆盖率约 `82.03%`
  - 这不是错页问题，是“部分字头暂未覆盖”
  - 前端空状态与外部参考兜底逻辑已存在
- OCR 详情层当前状态：
  - `dict_exam_xuci_details.json` 与 `/api/dict/exam/xuci-detail` 保留
  - 但因识别质量未达上线标准，前端暂未启用 OCR 提要 / 义项 / 思维导图展示
  - 当前真题页只启用原图、教育部修订本原文、教材例句与年度真题全文
- `2026-03-11 14:05 UTC` 生产已完成 clean release 修复：
  - 当前镜像：`textbook-knowledge:build-36617c8-20260311_140512`
  - 手动回滚锚点：`textbook-knowledge:manual-pre-r30-20260311_0705`
  - 当前公开前端版本：`2026.03.11-r30`
  - 主站搜索结果已重新返回非空 `page_url`
  - `https://img.rdfzer.com/pages/47e3538c5b76/p12.webp` 与 `https://img.rdfzer.com/pages/dict_xuci/p844.webp` 现场抽查均返回 `200`

## 当前剩余风险

### 风险 1：OCR 详情层仍处于保留未启用状态

当前线上页面已经切到“原图 / 原文优先”：

- 真题虚词 / 真题实词详情页只展示：
  - 年份分布
  - 年度真题全文
  - 教材例句与原图
  - 两本馆藏辞典原图
  - 教育部修订本原文
- 年度真题全文当前已随运行时考表 JSON 打包
  - 不再依赖生产 `gaokao` 库回查全文
- `dict_exam_xuci_details.json` 与 `/api/dict/exam/xuci-detail` 继续保留
- 待人工校对并提供精确替换版本后，再恢复 OCR 义项层

### 风险 2：`HEAD /dict.html` 仍返回 `405`

当前线上：

- `GET /dict.html` 正常返回 `200`
- `HEAD /dict.html` 返回 `405 Method Not Allowed`

结论：

- 页面本身已可用
- 但运维或外部监控探针若使用 `HEAD`，会误判失败
- 当前应统一改用 `GET` 作为页面可用性检查

### 风险 3：前端仍保留未启用的 OCR 详情渲染代码

当前状态：

- `frontend/assets/dict.js` 仍保留 `renderExamXuciDetailSupplement()` 与 `loadExamXuciDetail()`
- 但页面已无 `exam-xuci-supplement` 挂载点，也没有启用调用链

结论：

- 这不是当前线上功能问题
- 但后续若有人直接恢复挂载点，旧 OCR 文本 UI 会重新暴露
- 在人工校对版未到位前，不应恢复这条渲染链

### 风险 4：主站原图链路依赖 `book_map.json` 随镜像进包

`frontend/assets/pages/book_map.json` 当前是主站“查看原文”恢复的关键文件：

- 页面图片本身仍在 CDN
- 但 live `page_url` 生成仍依赖镜像内的 `book_map.json`
- 若未来 clean release 漏掉该文件，即使 CDN 图片仍存在，主站也会再次退化成 `page_url=null`

### 风险 5：`latest` 不能视为天然回滚锚点

本轮事故暴露出：

- 运行中的容器镜像
- `textbook-knowledge:latest`
- 最近一次 `pre-*` 备份标签

这三者不一定始终指向同一份镜像。

结论：

- 手工发布前必须先核对运行中容器的 image digest
- 若需要人工回滚锚点，应先手动 tag 运行中 digest
- 不能直接把 `latest` 当作“当前线上版本”

## 本轮核查覆盖范围

### 前端逻辑

- 导航入口：
  - 首页保留原有搜索 / 真题 / 数据 / 图谱
  - 新增 `实虚词典` 链接，不干扰原视图切换
- `/dict.html` 页面结构：
  - 教材
  - 馆藏辞典原页
  - 教育部修订本只读结果区
  - 官方与外部参考
  - 真题
  - AI 多轮对话
- 搜索并发：
  - 已补 query token 防止旧请求覆盖新搜索结果
  - 已把“自动首轮 AI”从搜索主流程解耦，避免用户被 AI 响应卡住
- 降级逻辑：
  - 教材 / 馆藏辞典 / 外部参考 / 真题四路独立容错
  - 单路失败不拖垮整页

### 后端逻辑

- 默认启用源已切到：
  - `xuci`
  - `changyong`
- `dict/status` 已返回：
  - `verified_headwords`
  - `candidate_headwords`
  - `coverage_ratio`
  - `student_safe_mode = page_images_only`
- `dict/search` 当前优先走：
  - `dict_headword_pages.json`
  - 再回退 `dictionary_index.db`
- `dict/moe-revised` 当前走：
  - 教育部《重编国语辞典修订本》授权包本地 SQLite
  - 学生端只读展示，不改写原文
- `dict/references` 已接：
  - 教育部《重编国语辞典修订本》
  - 教育部《国语辞典简编本》
  - 教育部《异体字字典》
  - `zi.tools`
  - 汉语多功能字库

### 数据逻辑

- `xuci`
  - 通过 `pdftotext -raw` 检测字头起始页
  - 再按下一个字头起始页推完整页码区间
- `changyong`
  - 通过 CSV 单字头顺序底稿 + 页眉 Vision OCR
  - 只 OCR 页眉，不 OCR 全页正文
- 学生端统一只展示原页图片，不展示 OCR 正文

## 上线门槛

必须同时满足以下条件，才算可上线：

1. 生产 CDN 上 dict 页图可访问
2. 生产后端已部署新代码
3. 生产数据目录已同步最新索引文件
4. 生产 `/dict.html` 页面可访问
5. 生产 API 烟测通过
6. 页面端到端手工点击通过

任一项不满足，均为 `NO-GO`

## 部署前数据同步清单

必须同步：

- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_pages.json`
- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_qc.json`
- `/Users/ylsuen/textbook_ai_migration/data/index/dict_moe_revised.db`

建议一并留档：

- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_candidates_xuci.jsonl`
- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_candidates_changyong.jsonl`
- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_review.tsv`

必须上传到 R2/CDN：

- `pages/dict_xuci/p{N}.webp`
- `pages/dict_changyong/p{N}.webp`

运维约束：

- 不要把教材页图目录单独对 `r2:textbook-images/pages/` 做根级 `sync`
- 页图上传前必须把教材页图和 `data/dict_pages/` 一起合并到同一个 staging tree，否则远端现有的 `dict_xuci` / `dict_changyong` 会被删除

## 部署前代码同步清单

必须同步：

- `/Users/ylsuen/textbook_ai_migration/platform/backend/main.py`
- `/Users/ylsuen/textbook_ai_migration/platform/frontend/dict.html`
- `/Users/ylsuen/textbook_ai_migration/platform/frontend/assets/dict.css`
- `/Users/ylsuen/textbook_ai_migration/platform/frontend/assets/dict.js`

建议随仓保留：

- `/Users/ylsuen/textbook_ai_migration/platform/scripts/build_dict_headword_index.py`
- `/Users/ylsuen/textbook_ai_migration/platform/scripts/vision_ocr.swift`
- `/Users/ylsuen/textbook_ai_migration/scripts/40_dictionary_ocr_qc.py`

## 生产烟测

### 接口烟测

必须通过：

```bash
curl -sS https://sun.bdfz.net/api/dict/status
curl -sS "https://sun.bdfz.net/api/dict/search?q=之"
curl -sS "https://sun.bdfz.net/api/dict/moe-revised?q=之"
curl -sS "https://sun.bdfz.net/api/dict/exam/xuci"
curl -sS "https://sun.bdfz.net/api/dict/exam/shici"
curl -sS "https://sun.bdfz.net/api/dict/exam/questions?kind=xuci&headword=以"
curl -sS "https://sun.bdfz.net/api/dict/exam/questions?kind=shici&headword=道"
curl -sS "https://sun.bdfz.net/api/dict/exam/xuci-detail?headword=之"
curl -sS "https://sun.bdfz.net/api/dict/search?q=觇"
curl -sS "https://sun.bdfz.net/api/dict/search?q=长"
curl -sS "https://sun.bdfz.net/api/dict/search?q=所以"
curl -sS "https://sun.bdfz.net/api/dict/references?q=斯民"
curl -sS "https://sun.bdfz.net/api/dict/textbook?q=之"
curl -sS "https://sun.bdfz.net/api/dict/gaokao?q=之"
curl -sS "https://sun.bdfz.net/api/dict/page-images?dict_source=changyong&page=99&context=2"
curl -sS "https://sun.bdfz.net/api/dict/page-images?dict_source=xuci&page=188&context=2"
```

通过标准：

- `status` 返回 `enabled_sources` 包含 `xuci, changyong`
- `search?q=之` 返回 `changyong + xuci`
- `moe-revised?q=之` 返回教育部修订本本地只读结果
- `exam/xuci` 与 `exam/shici` 返回真题词表 JSON
- `exam/questions?kind=xuci&headword=以` 返回按年份聚合的真题全文
- `exam/questions?kind=shici&headword=道` 返回实词真题全文
- `exam/xuci-detail?headword=之` 返回保留中的 OCR 详情层 JSON
- `search?q=觇` 返回 `changyong`
- `search?q=所以` 返回 `xuci`
- `references?q=斯民` 返回外部参考卡片
- `page-images` 返回真实 URL，不是空数组

### CDN 烟测

必须通过：

```bash
curl -sSI https://img.rdfzer.com/pages/dict_xuci/p188.webp
curl -sSI https://img.rdfzer.com/pages/dict_xuci/p844.webp
curl -sSI https://img.rdfzer.com/pages/dict_changyong/p99.webp
curl -sSI https://img.rdfzer.com/pages/dict_changyong/p101.webp
```

通过标准：

- HTTP `200`
- `content-type` 为图片类型

### 页面手测

至少手测这些词：

- `之`
- `觇`
- `长`
- `所以`
- `斯民`

必须确认：

- 页面能打开 `/dict.html`
- 左栏教材只出现古文 / 古诗词
- 右栏馆藏辞典能打开正确页图
- 右栏教育部修订本能看到只读原文结果，并能打开教育部原站
- `之` 同时能看到王力本和虚词本
- `斯民` 在馆藏未命中时，右栏外部参考正常出现
- 真题区正常返回
- AI 首轮自动触发
- 新搜索不会被上一次 AI 卡住

## 回滚点

### 最小回滚

- 保留页面，但把后端环境变量 `DICT_ENABLED_SOURCES` 收回 `xuci`

### 代码回滚

- 回退：
  - `backend/main.py`
  - `frontend/dict.html`
  - `frontend/assets/dict.css`
  - `frontend/assets/dict.js`

### 数据回滚

- 回退：
  - `data/index/dict_headword_pages.json`
  - `data/index/dict_headword_qc.json`

## 当前推荐判断

- 代码：`GO`
- 数据索引：`GO`
- 质检：`GO`
- 生产上线：`GO`

当前无硬阻塞项。

当前仅有两项已知剩余风险：

- OCR 详情层因识别质量问题暂不启用
- 监控探针需使用 `GET /dict.html`，不要使用 `HEAD`
