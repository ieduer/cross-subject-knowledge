# 实虚词典上线前检查单

## 当前结论

- 代码链路已基本打通：
  - `/dict.html`
  - `GET /api/dict/status`
  - `GET /api/dict/search`
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

## 当前阻塞项

### 阻塞 1：馆藏辞典页图 CDN 仍未上线

当前抽查全部返回 `404`：

- `https://img.rdfzer.com/pages/dict_xuci/p188.webp`
- `https://img.rdfzer.com/pages/dict_xuci/p844.webp`
- `https://img.rdfzer.com/pages/dict_changyong/p99.webp`
- `https://img.rdfzer.com/pages/dict_changyong/p101.webp`

结论：

- 代码与页码索引已就绪
- 但学生端上线后会看到“页图待导入”或加载失败
- 在页图上传到 R2/CDN 之前，不能算可上线状态

### 阻塞 2：生产环境尚未部署新页面与新数据

当前只完成本地代码、索引和质检。

仍未确认：

- 生产后端是否已加载新 `main.py`
- 生产前端是否已提供 `/dict.html`
- 生产数据目录是否已同步：
  - `dict_headword_pages.json`
  - `dict_headword_qc.json`

## 本轮核查覆盖范围

### 前端逻辑

- 导航入口：
  - 首页保留原有搜索 / 真题 / 数据 / 图谱
  - 新增 `实虚词典` 链接，不干扰原视图切换
- `/dict.html` 页面结构：
  - 教材
  - 馆藏辞典原页
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

建议一并留档：

- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_candidates_xuci.jsonl`
- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_candidates_changyong.jsonl`
- `/Users/ylsuen/textbook_ai_migration/data/index/dict_headword_review.tsv`

必须上传到 R2/CDN：

- `pages/dict_xuci/p{N}.webp`
- `pages/dict_changyong/p{N}.webp`

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
- 生产上线：`NO-GO`

当前唯一明确阻塞项是：

- 馆藏辞典页图尚未上传到生产 CDN

在这一步完成之前，不建议部署。
