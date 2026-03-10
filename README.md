# 🔗 AI 高中教材

> 这世界真的有学科吗？ — 发现高中 9 科教材中**隐藏的跨学科联系**，让 AI 帮你综合解读

**在线体验 → [sun.bdfz.net](https://sun.bdfz.net)**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## ✨ 核心功能

- 🔍 **跨学科搜索** — 搜索一个概念，按学科分组展示不同教材中的内容
- 🎯 **精准概念检索** — “晶体的定义” 这类定义型问法会走意图识别、混合召回与重排，不再只按高频词散搜
- 💡 **自动关联提示** — 检测到概念横跨多学科时，自动提示跨学科联系
- ✨ **AI 教材解读** — 一键调用 Gemini，支持精确问法优先走后端 agent 检索，也保留跨学科综合解读
- 🗺️ **知识图谱** — D3.js 力导向交互式图谱，可视化 788 个学术概念在 9 学科间的关联网络，支持缩放/拖拽/悬停高亮
- 📊 **数据洞察** — 726 个精选学术术语的词频分析、学科关联热力图、考试覆盖分析、概念广度排名
- 📚 **教材下载** — 全部 316 本高中教材 PDF 可从 [jks.bdfz.net](https://jks.bdfz.net/) 下载

数据层、运行时资产与部署边界的长期说明见 [docs/data_layer_lineage_memory.md](docs/data_layer_lineage_memory.md) 和 [docs/runtime_operations_overview.md](docs/runtime_operations_overview.md)。任何数据重建、检索排障、部署或回滚前，先看 `data_layer_lineage_memory.md`。

### 高级检索

- ⚙️ **高级搜索面板** — 按教材筛选、按排序方式切换（相关度 / 跨学科数 / 含图优先）
- 🔗 **相关概念推荐** — 搜索后自动推荐共现频率最高的相关概念，点击即搜
- 📷 **图片标注** — 搜索结果显示图片数量 badge，展开即可查看教材原图
- 📖 **教材筛选** — 按学科分组的 193 本可检索教材下拉选择器，精准定位特定教材内容

---

## 🧭 2026-03-10 检索升级复盘与公开约束

### 本轮已完成的关键更新

- 修复“潜热”等教材原词无结果问题：补充教材索引已按版本和书目身份重建，发布后可检索教材扩展到 **193 本**（主库 69 本 + 补充独立书目 124 本）
- `/api/search` 升级为 **hybrid + rerank**：词法命中、FAISS 语义召回、补充教材页索引兜底、CrossEncoder 重排共同参与排序
- 定义型查询单独做意图识别与降噪，例如“晶体的定义”会优先召回“是什么 / 是指 / 称为”类正式定义句，降低“晶体”“的定义”这类高频词噪声
- AI 搜索改为精确问法优先走后端 precision agent；列表搜索与 AI 卡片分层保留，前者给原文证据，后者给过滤后的答案
- 前端结果卡新增检索通道标识，区分“精确命中 / 全文命中 / 向量召回 / 备份教材兜底”，便于诊断检索来源与排序行为
- 补充教材向量索引已完成重建并纳入发布资产，生产部署增加补充教材索引/向量同步、reranker 预热与健康闸门；`/api/health` 现在会同时暴露 `supplemental`、`supplemental_vectors` 与 `reranker` 状态

### 公开运维约束

- 补充教材索引必须按“等价覆盖、不做摘要”重建；上线前 manifest 里的 `unresolved_books` 和 `unresolved_pages` 必须都为 `0`
- 部署时必须同步：`textbook_mineru_fts.db`、`textbook_chunks.index`、`textbook_chunks.manifest.json`、`supplemental_textbook_pages.jsonl.gz`、`supplemental_textbook_pages.manifest.json`、`supplemental_textbook_pages.index`、`supplemental_textbook_pages.vector.manifest.json`
- reranker 不能只在代码里启用；生产发布必须预热 `BAAI/bge-reranker-base`，并通过健康检查确认 `reranker.loaded=true`
- 对定义型、区别型、过程型问法，公开口径应以 hybrid + rerank 为主链；不要再把词法兜底误写成“语义搜索”
- 大体积运行时资产不要写死走某一条链路；每次发布前都要先实测“工作站直传 VPS”和“工作站 -> R2 -> VPS curl”两条路径，再选当次更稳更快的方案
- GitHub 公开文档可以记录可公开的架构、流程和统计，但不要提交 SSH 主机细节、密钥、令牌、私有路径或任何敏感运行信息

### 当前公开已知限制

- 列表搜索已经从纯关键词升级为混合检索，但主体仍是 chunk / page 级证据，不是句级定义抽取
- 精确问法的 AI 卡片体验通常优于原始结果列表，但 `/api/search` 仍保留可解释、可翻阅的原文检索属性，不以摘要替代证据

---

## 🧭 2026-03-08 更新复盘与公开约束

### 本轮已完成的关键更新

- 新增并行教材版本数据：鲁科版化学 5 本、北师大版英语 7 本，与既有人教版等版本并列接入
- 教材相关 UI 与 API 全面补齐版本标注，避免不同出版社教材混淆；知识图谱与数据洞察继续使用合并语料口径
- 古文 / 古诗词改为“清单优先、启发式兜底”，降低误收误漏
- 实虚词典改为优先使用已核页图锚点，展示“书页 + PDF 页”，并修复高频单字命中排序
- 热门词过滤掉烟测 / 并发压测查询，并增加实时结果校验
- 英语学科的数据洞察与学科图谱改为英语专用词项抽取，避免落回中文概念表
- 学科关联热力图改为按底色亮度自适应文字颜色，修复黄底白字可读性问题
- 恢复实虚词典 R2 页图，并补上页图同步的防误删约束

### 公开运维约束

- 新增教材版本时，必须同步更新：版本清单、页图映射、检索库、向量索引、真题教材对位、前端展示文案
- 教材结果需要区分版本；知识图谱和数据洞察默认使用合并语料，不按版本拆分
- 英语学科的词频分析和学科图谱不能复用中文精选术语表，必须走英语专用抽取逻辑
- 热门词不能只依赖历史日志频次，必须排除 smoke / concurrency / benchmark 之类测试查询，并校验当前确实有结果
- 教材古文 / 古诗词识别不能只靠启发式；应优先使用明确清单，再用兜底规则补漏
- 书内页码与 PDF 页码必须显式区分；任何词典、教材原页、页图弹窗都不能混用这两个口径
- 上传 `pages/` 到 Cloudflare R2 时，不能只同步教材页图源；必须把教材页图与词典页图一起合并后再同步，否则会删掉远端现有的 `dict_xuci` / `dict_changyong`
- 每次重建教材语料或向量索引后，README、runtime overview、线上版本说明里的关键统计口径必须一起更新
- GitHub 公开文档只记录可公开的架构、流程和约束；不要提交 SSH 主机细节、密钥、令牌、私有路径或任何敏感运行信息

---

## 📊 数据规模

| 指标 | 数值 |
|------|------|
| 可检索教材 | **193 本**（主库 **69** 本 + 补充独立书目 **124** 本） |
| PDF 下载库 | **316 本**（独立教材下载区） |
| 学科覆盖 | **9 科**：语文、数学、英语、物理、化学、生物学、历史、地理、思想政治 |
| 结构化语料 | **21,925 条**（主库教材 **17,896** + 高考真题 **4,029**） |
| 补充教材页索引 | **22,844 页**（来自 **251** 份 OCR 源文件，按页全文匹配） |
| 补充教材向量 | **22,844 条**（`BAAI/bge-m3`，与当前补充页源 fingerprint / sha256 一致） |
| 高考真题 | **4,029 道**（`2002-2025`，其中 **651** 道含图题） |
| 学术概念图谱 | **788 个**概念，1,723 条学科映射，83 条跨学科聚合记录 |
| 精选术语 | **726 个**精选学术术语 |
| 教材插图 | **87,156 张**（3.4 GB，由 R2 CDN 全球分发） |
| FTS 索引大小 | **56 MB**（SQLite FTS5 运行库） |
| FAISS 索引大小 | **70 MB**（`BAAI/bge-m3`，17,896 向量） |
| Docker 运行镜像 | **2.07 GB**（CPU-only，运行时数据和缓存走宿主机挂载） |

### 概念图谱各学科分布

| 学科 | 概念数 | 示例 |
|------|--------|------|
| 🧬 生物学 | 246 | 孟德尔(45)、达尔文(16)、培养基(49)、噬菌体(12) |
| 📜 历史 | 237 | 汉武帝(29)、拿破仑(19)、五四运动(14)、工业革命(62) |
| ⚛️ 物理 | 222 | 牛顿(71)、爱因斯坦(30)、α粒子(3)、γ射线(2) |
| 🧪 化学 | 217 | 阿伏加德罗(19)、摩尔(34)、σ键(2) |
| ⚖️ 思想政治 | 206 | 社会主义(553)、马克思(86)、中国共产党(115) |
| 📐 数学 | 185 | — |
| 📖 语文 | 180 | 鲁迅(30)、杜甫(19)、李白(14)、意象(14)、意境(12) |
| 🗺️ 地理 | 154 | — |
| 🌍 英语 | 67 | — |

> **跨学科概念 TOP 5**：古希腊(9科)、现代化(8科)、环境保护(8科)、地震(8科)、火山(8科)

### 各学科语料分布

| 学科 | 语料数 | 含图率 | 学科 | 语料数 | 含图率 |
|------|--------|--------|------|--------|--------|
| 🌍 英语 | 15,425 | 21.0% | 🧪 化学 | 5,262 | 39.5% |
| 📐 数学 | 12,567 | 38.0% | ⚖️ 思想政治 | 3,444 | 22.2% |
| 🧬 生物学 | 8,890 | 38.9% | 📜 历史 | 3,150 | 33.9% |
| ⚛️ 物理 | 8,305 | 44.6% | 📖 语文 | 1,228 | 13.9% |
| 🗺️ 地理 | 7,707 | **56.9%** | | | |

### 图片数据

| 区间 | 数量 | 占比 | 说明 |
|------|------|------|------|
| < 1 KB | 109 | 0.1% | 极小碎片 |
| 1-5 KB | 22,032 | 25.3% | 公式符号、小图标 |
| 5-20 KB | 28,811 | 33.1% | 简单示意图、表格 |
| 20-100 KB | 27,150 | 31.1% | 中等插图、电路图 |
| 100-500 KB | 8,745 | 10.0% | 大型地图、实验图 |
| > 500 KB | 309 | 0.4% | 全页彩色地图 |

> **中位数 13.6 KB** — 58% 的图片 < 20 KB，直接使用原图，不做缩略图处理。

---

## 🏗️ 系统架构

```
用户浏览器
    │
    ├── HTTPS → sun.bdfz.net (VPS)
    │           │
    │           └── Docker: textbook-knowledge
    │               ├── FastAPI 后端 (Python 3.13)
    │               ├── NLP/ML 引擎
    │               │   ├── BAAI/bge-m3 ── 多语言语义向量 (1024D)
    │               │   ├── FAISS ── 17,896 条教材向量稠密检索
    │               │   ├── BAAI/bge-reranker-base ── precision / hybrid 重排
    │               │   └── Jieba ── 中文分词 + 词性标注
    │               ├── 前端 (HTML/CSS/JS + D3.js + KaTeX)
    │               ├── SQLite FTS5 检索库 (56MB)
    │               ├── 补充教材页索引 (22,844 页 gzip + manifest)
    │               ├── 补充教材向量 (22,844 条，FAISS)
    │               └── 宿主机挂载 data/index + state/cache
    │
    ├── HTTPS → img.rdfzer.com (Cloudflare R2 CDN)
    │           └── 87,156 张教材原图（3.4GB，全球加速，免费出站）
    │
    └── HTTPS → ai.bdfz.net (Cloudflare Worker custom domain)
                └── service: apis / production → Gemini API 最大 key 池
```

### AI 网关约定

- 本项目外部 AI 入口统一使用 `https://ai.bdfz.net/`
- `ai.bdfz.net` 是 Cloudflare Worker custom domain，实际绑定到 service `apis` / `production`
- 在 Cloudflare Dashboard 里看到的 `apis` 是服务名，不是这个项目应优先暴露给用户的 canonical 域名
- 详细说明见 [docs/ai_gateway_rule.md](docs/ai_gateway_rule.md)
- 运行时与运维总览见 [docs/runtime_operations_overview.md](docs/runtime_operations_overview.md)

### Docker 内容

```
/app/                           # 镜像内代码
├── backend/main.py             # FastAPI 应用
├── frontend/
│   ├── index.html              # 主页（搜索/真题/图谱/数据/关于）
│   └── assets/
│       ├── style.css
│       └── app.js
├── requirements.runtime.txt    # 运行时 Python 依赖
└── scripts/deploy_vps.sh       # 生产发布脚本

/data/index/                    # 宿主机挂载的运行时检索资产
├── textbook_mineru_fts.db      # FTS5 索引 + 概念图谱
├── textbook_chunks.index       # FAISS 向量索引
├── textbook_chunks.manifest.json
├── supplemental_textbook_pages.jsonl.gz
├── supplemental_textbook_pages.manifest.json
├── supplemental_textbook_pages.index
└── supplemental_textbook_pages.vector.manifest.json

/state/cache/                   # 宿主机挂载的运行时缓存
└── huggingface/
    └── hub/                    # HF / Transformers / sentence-transformers 共享模型快照
```

> 📷 **图片不在 Docker 中** — 87K 张原图托管在 Cloudflare R2（`img.rdfzer.com`），前端通过 CDN URL 直接加载。

---

## 🔧 完整数据处理流水线

### Phase 1: 教材获取

**来源**：国家中小学智慧教育平台（smartedu）

**脚本**：`scripts/01_download_textbooks_via_images.py`

- 通过智慧教育平台 API 直接下载原始教材 PDF
- 按 `学段/学科/书名` 目录结构存储

**产物**：`data/raw_pdf/` → **31 GB**，316 本 PDF

### Phase 2: OCR 结构化提取

**引擎**：[MinerU](https://github.com/opendatalab/MinerU) v2.7.6（55k⭐）

**选型理由**（vs Tesseract、PyMuPDF）：
- 📐 **结构保持**：标题/段落/列表层级完整
- 📊 **表格识别**：直接输出 HTML `<table>`
- 🧮 **公式提取**：数学公式转 LaTeX
- 🖼️ **图片提取**：自动裁切并保存引用（87,156 张）
- 🇨🇳 **中文优化**：扫描件 OCR 准确率远超 Tesseract
- ⚡ **GPU 加速**：CUDA + PyTorch，处理速度 5-10x

**脚本**：`scripts/08_mineru_batch.py`

**特性**：
- **幂等执行**：JSON 状态文件记录每本书的处理状态
- **失败熔断**：连续 3 本失败则跳过该批次
- **自动分块**：将 Markdown 按 `##` 标题拆分为 chunks

**产物**：
| 目录 | 大小 | 内容 |
|------|------|------|
| `data/mineru_output/` | **101 GB** | 每本书的 Markdown + 提取的图片 |
| `data/index/mineru_chunks.jsonl` | **95 MB** | 65,978 条结构化 chunk |

### Phase 3: 索引构建

**脚本**：`scripts/09_build_unified_index.py`

```sql
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    subject TEXT,     -- 学科: 化学/物理/数学/...
    title TEXT,       -- 书名
    book_key TEXT,    -- 唯一标识: 学段_学科_书名
    section INTEGER,  -- 章节序号
    text TEXT          -- 正文内容（含 Markdown 图片引用）
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content=chunks, content_rowid=id);
```

**产物**：

- `data/index/textbook_mineru_fts.db` → **110 MB**
- `data/index/textbook_chunks.index` → **65 MB**
- `data/index/textbook_chunks.manifest.json` → 运行时向量校验清单

### Phase 4: 概念图谱构建

**脚本**：`scripts/27_rebuild_concepts.py`

- 从 996 个精心策划的学术术语出发，经过 Unicode 规范化 + 通用词过滤后保留 875 个
- 逐条匹配 19,723 条 chunk 文本，生成 `concept_map`（学科-概念-频次映射）
- 同时生成 `curated_keywords`、`keyword_counts`、`cross_subject_map`
- 支持希腊字母（σ键、α粒子、γ射线）的 Unicode NFKC 规范化匹配

**产物**：
| 表 | 行数 | 说明 |
|------|------|------|
| `concept_map` | 1,714 | 学科-概念-频次三元组 |
| `curated_keywords` | 720 | 精选学术术语 |
| `keyword_counts` | 2,825 | 术语按学科和来源的频次统计 |
| `cross_subject_map` | 83 | 跨学科概念聚类 |

### Phase 5: 图片上传

```bash
# 使用 rclone 批量上传到 Cloudflare R2
rclone sync data/images/ r2:textbook-images/orig/ --transfers 16 --progress
# 87,156 张图片，3.4 GB，R2 免费额度内
```

**R2 成本**：完全免费（3.4GB 存储在 10GB 免费额度内，出站流量永远免费）

### Phase 6: 部署

```bash
# 生产推荐：运行带健康闸门和回滚的发布脚本
chmod +x scripts/deploy_vps.sh
RUNTIME_ROOT=/root/cross-subject-knowledge ./scripts/deploy_vps.sh
```

---

## 📦 完整数据清单

### 本机（开发/处理机）

| 路径 | 大小 | 用途 | 可重建? |
|------|------|------|---------| 
| `data/raw_pdf/` | **31 GB** | 316 本原始 PDF | ❌ 需重新下载 |
| `data/mineru_output/` | **101 GB** | MinerU OCR 产物 | ✅ 从 PDF 重新生成（~20h） |
| `data/images/` | **3.4 GB** | 87K 张提取的教材图片 | ✅ 从 MinerU 产物提取 |
| `data/index/` | **175 MB** | 运行时 FTS 库 + FAISS 索引 + manifest | ✅ 从 MinerU 产物重建 |

### 云端

| 服务 | 内容 | 大小 |
|------|------|------|
| VPS | Docker 镜像 + 容器（代码） | 约 2.1 GB |
| Cloudflare R2 (`img.rdfzer.com`) | 87,156 张跨学科教材原图及页面图 | 4.2 GB |
| GitHub | 源代码 | < 1 MB |

---

## VPS 推荐规格

当前线上运行时实测：

- 应用容器常驻内存约 **1.2 GiB**
- 宿主机总内存 **5.8 GiB** 时运行稳定
- 运行时数据目录约 **< 1 GiB**
- 生产风险点主要不在数据库，而在 **Docker 镜像体积** 和 **历史镜像堆积**

推荐规格：

- **最低可用**：2 vCPU / 4 GB RAM / 25 GB SSD
- **推荐生产**：4 vCPU / 8 GB RAM / 60 GB SSD
- **如果同机还跑别的服务或要在 VPS 本机 `docker build`**：建议 4 vCPU / 8 GB RAM / 80 GB SSD

说明：

- 本项目生产不需要 GPU
- FAISS 重建和批量数据加工继续放在离线机器，本 VPS 只承担运行时检索与对话服务
- 模型缓存现在建议落在宿主机 `state/cache/`，不要再烘进镜像层

---

## 🏗️ 架构与部署逻辑 (CI/CD)

本项目采用了**「代码库与大体积数据彻底剥离」**的设计原则。

### 1. 资源存储隔离
*   **源代码 (GitHub)**：前端页面、后端 API、Dockerfile、各种配置。**绝对不含**庞大的数据库和图片。
*   **图片资源 (R2 CDN)**：所有的教材原图、单页截图等，托管在 Cloudflare R2 (`img.rdfzer.com`)，全球加速分发，不消耗部署服务器 (VPS) 的带宽。
*   **检索数据库 (VPS 本地)**：`textbook_mineru_fts.db`、`textbook_chunks.index`、对应 manifest，以及页级补充教材索引 `supplemental_textbook_pages.jsonl.gz` 和补充向量 `supplemental_textbook_pages.index`，存放于 VPS 本地 `data/index/`，通过 Docker 挂载提供服务。
*   **模型缓存 (VPS 本地)**：Hugging Face、Transformers、sentence-transformers 统一共享 VPS 本地 `state/cache/huggingface/hub/`，不再烘进镜像，也避免重复存两份模型权重。

### 2. 自动化部署 (GitHub Actions)
项目利用 GitHub Actions 实现了完全自动化的持续部署：
1.  开发者在本地修改代码后，`git push` 到 GitHub `main` 分支。
2.  GitHub Actions 自动触发，SSH 连入生产服务器 (VPS: `sun.bdfz.net`)。
3.  在 VPS 上创建临时的干净 release checkout，避免生产目录里历史热补丁或临时改动阻塞发布。
4.  在 VPS 上先构建新镜像，再停旧容器，避免“构建失败直接打挂线上”。
5.  部署脚本会同步补充教材索引与补充向量到运行时目录，并预热 `BAAI/bge-reranker-base`，避免首个精确查询才触发冷启动。
6.  新容器通过 `/api/health` 健康检查后才算部署成功；当前健康闸门要求 DB、FAISS、补充教材索引可用，且在启用 reranker 时确认 `reranker.loaded=true`；失败则自动回滚到上一镜像。
7.  运行时模型缓存保存在宿主机 `state/cache/`，避免每次发版都把 Hugging Face 缓存烘进镜像。
8.  部署完成后自动清理悬空镜像，只保留最近几份 `pre-*` 回滚镜像，并删除历史 `build-*` tag。

### 3. 服务器 (VPS) 迁移指南
由于大头数据 (4GB+ 图片) 都在云端 CDN，如果未来需要更换服务器提供商，迁移将极其简单轻量：
1.  **准备环境**：在新 VPS 安装 Git 和 Docker。
2.  **拉取代码**：`git clone` 本仓库。
3.  **搬运核心库**：把本地或旧服务器上的运行时检索资产复制到新服务器的 `data/` 目录，包括 `textbook_mineru_fts.db`、`textbook_chunks.index`、`textbook_chunks.manifest.json`、`supplemental_textbook_pages.jsonl.gz`、`supplemental_textbook_pages.manifest.json`、`supplemental_textbook_pages.index`、`supplemental_textbook_pages.vector.manifest.json`。
4.  **启动**：执行 `docker build` 和 `docker run`（或配置新的 GitHub 部署密钥触发自动流）。
5.  **切换域名**：在 Cloudflare 中将 `sun.bdfz.net` 的 A 记录指向新 VPS 的 IP。

---

## 🚀 快速开始

### 本地运行

```bash
pip install -r requirements.runtime.txt
pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.10.0+cpu"

# 将 textbook_mineru_fts.db 和 textbook_chunks.index 放到 data/index/ 目录
uvicorn backend.main:app --host 0.0.0.0 --port 8080
# 访问 http://localhost:8080
```

### Docker 运行

```bash
# 需要先准备运行时数据目录
docker build -t textbook-knowledge .
docker run -d --name textbook-knowledge \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PROJECT_ROOT=/app \
  -e DATA_ROOT=/data \
  -e STATE_ROOT=/state \
  -e HF_HOME=/state/cache/huggingface \
  -e HF_HUB_CACHE=/state/cache/huggingface/hub \
  -e SENTENCE_TRANSFORMERS_HOME=/state/cache/huggingface/hub \
  -e TRANSFORMERS_CACHE=/state/cache/huggingface/hub \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/state:/state" \
  textbook-knowledge
```

### 从头处理数据

```bash
# 1. 下载教材 PDF（需要智慧教育平台访问）
python scripts/01_download_textbooks_via_images.py

# 2. 安装 MinerU（需要 NVIDIA GPU + CUDA）
pip install mineru[all]

# 3. 批量 OCR（约 20 小时）
python scripts/08_mineru_batch.py

# 4. 构建索引
python scripts/09_build_unified_index.py

# 5. 构建概念图谱
python scripts/27_rebuild_concepts.py

# 6. 上传图片到 R2
rclone sync data/images/ r2:textbook-images/orig/ --transfers 16

# 7. 处理高考 PDF 真题 (北京卷/2025卷)
python scripts/17_process_beijing_gaokao.py

# 8. 部署
chmod +x scripts/deploy_vps.sh
RUNTIME_ROOT=/root/cross-subject-knowledge ./scripts/deploy_vps.sh
```

---

## 🔄 VPS 迁移指南

### 运行规格

| 参数 | 最低 | 推荐 |
|---|---|---|
| CPU | 2 核 | 4 核 |
| 内存 | 4 GB | 8 GB |
| 磁盘 | 25 GB | 60 GB |
| OS | Ubuntu 22.04+ | Ubuntu 24.04 |
| Docker | 必须 | ✅ |

> 如果同机还需要执行 `docker build`、承载别的服务，或希望给模型缓存和回滚镜像留余量，建议直接用 **4 vCPU / 8 GB RAM / 80 GB SSD**。

### 迁移步骤

```bash
# 1. 克隆仓库
git clone https://github.com/ieduer/cross-subject-knowledge.git
cd cross-subject-knowledge

# 2. 获取运行时目录
mkdir -p /root/cross-subject-knowledge/data/index /root/cross-subject-knowledge/state/cache

# 3. 复制运行时检索资产
cp /old-host/data/index/textbook_mineru_fts.db /root/cross-subject-knowledge/data/index/
cp /old-host/data/index/textbook_chunks.index /root/cross-subject-knowledge/data/index/
cp /old-host/data/index/textbook_chunks.manifest.json /root/cross-subject-knowledge/data/index/
cp /old-host/data/index/supplemental_textbook_pages.jsonl.gz /root/cross-subject-knowledge/data/index/
cp /old-host/data/index/supplemental_textbook_pages.manifest.json /root/cross-subject-knowledge/data/index/
cp /old-host/data/index/supplemental_textbook_pages.index /root/cross-subject-knowledge/data/index/
cp /old-host/data/index/supplemental_textbook_pages.vector.manifest.json /root/cross-subject-knowledge/data/index/

# 4. 运行发布脚本
cd cross-subject-knowledge
chmod +x scripts/deploy_vps.sh
RUNTIME_ROOT=/root/cross-subject-knowledge ./scripts/deploy_vps.sh

# 5. 可选：nginx + SSL
apt install -y nginx certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

---

## 🛠️ 技术栈

| 组件 | 技术 | 版本/说明 |
|------|------|------|
| 数据库 | SQLite + FTS5 | 全文检索 + 概念图谱 |
| 后端 | FastAPI + uvicorn | Python 3.13 |
| 前端 | Vanilla HTML/CSS/JS | 无框架 |
| 知识图谱 | D3.js v7 | 力导向交互式图谱（缩放/拖拽/悬停） |
| 公式渲染 | KaTeX | LaTeX 数学公式 |
| 图片 CDN | Cloudflare R2 | `img.rdfzer.com` |
| AI 解读 | Gemini (via Cloudflare Worker service `apis` / `production`) | `ai.bdfz.net`（custom domain） |
| 容器 | Docker | CPU-only 运行镜像，运行时数据/缓存走宿主机挂载 |
| 数据备份 | rclone → Google Drive / R2 | |

### 后端及发掘技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| 中文向量模型 | `BAAI/bge-m3` | 1024D 多语言/长文本嵌入，2.2GB，全面提升语义理解度 |
| 重排模型 | `BAAI/bge-reranker-base` | CrossEncoder，用于 precision / hybrid 查询最终重排 |
| 向量检索 | `faiss-cpu` | 17,896 向量 IndexIDMap，70MB |
| API 缓存 | `cachetools` | TTLCache (5min, maxsize=64) 加速读密集型高频 API |
| 中文分词 | `jieba` + POS tagging | 启动时自动加载 `curated_keywords` 的 726 个学术术语为高权重用户词典，精准切词 |
| 自动部署 | GitHub Actions + `deploy_vps.sh` | 干净 release checkout 构建、健康检查、失败回滚 |
| 概念图谱 | SQLite `concept_map` | 788 个学术概念，跨学科自动发现 |
| 全文检索 | SQLite FTS5 | Porter 分词器，OR 组合查询 |
| 补充教材兜底 | `supplemental_textbook_pages.jsonl.gz` | 22,844 页全文匹配，覆盖主库缺失的教材原词与并行版本页 |
| 补充教材向量 | `supplemental_textbook_pages.index` | 22,844 条 `BAAI/bge-m3` 补充页向量，参与 hybrid semantic recall |
| 评分算法 | Hybrid retrieval + custom rerank | 词法命中 + 向量召回 + 补充页索引 + 定义意图重排 |

**教材检索流程**（hybrid + rerank）：
1. **查询意图识别** → 识别定义 / 区别 / 过程等精确问法，生成 precision term plan
2. **概念图谱 + 词法检索** → `_match_concepts` 与 FTS / LIKE 召回主库 chunk
3. **稠密向量召回** → `FAISS` 使用 `BAAI/bge-m3` 检索语义相近教材段落
4. **补充教材页索引兜底** → 直接匹配 `supplemental_textbook_pages.jsonl.gz` 中的页级全文
5. **CrossEncoder 重排** → `BAAI/bge-reranker-base` 按查询意图重排候选，提升定义句、结论句、关键原文的排序稳定性

---

## 📋 更新日志

### 2026-03-10: 补充教材兜底修复 + hybrid rerank 搜索上线

**数据与覆盖**
- ✅ 重建并上线页级补充教材索引，`251/251` 份 OCR 源文件已入索引，`unresolved_books=0`、`unresolved_pages=0`
- ✅ 线上可检索教材扩展到 `193` 本（主库 `69` 本 + 补充独立书目 `124` 本），补充页索引规模 `22,844` 页
- ✅ 补充教材向量索引已完成重建并通过 source fingerprint / sha256 校验，发布后可参与补充教材语义召回
- ✅ 修复 `潜热` 等“教材原词存在但主库缺失”时无法命中的问题，补充教材原文现在能被直接兜底检索

**检索与 AI**
- ✅ `/api/search` 从词法主导升级为 `lexical + semantic + supplemental + rerank` 的混合主链
- ✅ 定义型查询新增 precision 模式，`晶体的定义` 这类问法会优先召回正式定义句，而不是把“晶体”和“的定义”拆开散搜
- ✅ AI 教材解读新增后端 precision agent 路由，精确问法优先走服务端检索和重排，再生成答案
- ✅ 多学科联系继续保留知识图谱 / GraphRAG 提示层，但主证据链仍以教材原文检索和重排为准

**前端与运维**
- ✅ 结果卡区分 `精确命中 / 全文命中 / 向量召回 / 备份教材兜底`，方便诊断召回来源
- ✅ 部署脚本新增补充索引/向量同步、reranker 预热与健康闸门；`/api/health` 现在直接暴露 `supplemental`、`supplemental_vectors` 和 `reranker` 状态
- ✅ 前端版本标记更新到 `2026.03.10-r22`

### 2026-03-05: 页面对齐重建 + 交互链路加固

**数据底座重建（可回滚）**
- ✅ 使用 `33_rebuild_mineru_chunks_from_content_list.py`（`page_idx` 真值）重建教材 chunks，替代历史启发式页码修复
- ✅ 重建参数固定：`--include-discarded --max-chars 750 --min-chars 140`
- ✅ 对齐闸门从基线错配率 `2.089% (259/12400)` 降至 `0.138% (12/8690)`，`risky_count` 从 `13` 降至 `0`
- ✅ 重建前执行物理备份与审计快照（`logs/migration_baseline/backups/` + `snapshots/`）
- ✅ 保全 `search_logs` / `ai_batch_jobs`：重建后行数保持不变（用于热门与检索行为分析）
- ✅ 新增 FAISS 一致性闸门：若向量数量与 DB 行数不一致，自动降级禁用向量检索，避免错 ID 召回

**前端与后端改造**
- ✅ 原文查看容错窗口从 `±2` 升级为 `±4`（总计最多 9 页）
- ✅ 搜索结果新增“精确命中 / 语义召回”双通道标识与“图文来源可信度 + 关联路径”视图
- ✅ 关于页新增“反馈问题/提交建议”按钮，直达 GitHub issue 创建页
- ✅ 底部文案改为“前端重构版本”动态显示（`frontend/assets/version.json`），便于核验前端是否更新
- ✅ AI 跨学科解读升级为“有记忆对话流”，支持多轮追问与一键复制全部对话

### 2026-03-04: 技术栈全面升级

**AI与检索**
- ✅ 向量模型：升级为强大的多语言模型 `BAAI/bge-m3` (1024D 嵌入) 替换原有的 bge-small
- ✅ 向量索引：使用新模型重新编码 15,652 条核心教学切片至 1024D FAISS IndexIDMap (61MB)
- ✅ 智能分词：启动时自动提取 `curated_keywords` 中 720 个核心学术术语（如"共价键"）装载进 Jieba 自定义词典，避免错误切词

**性能与架构**
- ✅ 接口缓存：引入 `cachetools.TTLCache` 为 `stats`、`keywords` 等只读 API 提供 5 分钟短效缓存，重复请求耗时降至 <1ms
- ✅ 高可用监控：新增 `/api/health` 探针接口，结合 Docker `HEALTHCHECK` 实现容器挂死自动重启
- ✅ CI/CD：添加 GitHub Actions Workflow，实现代码 Push 主分支后自动触发 VPS 侧热更重启

### 2026-03-04: 知识图谱全面重建 + D3.js 交互式可视化

**概念提取重建**
- ✅ 概念字典从 ~300 扩展至 996 个精心策划的学术术语（过滤后 875 个）
- ✅ 有效概念 641→784，精选术语 311→720，概念映射条目 →1,714
- ✅ 语文学科概念从 62 扩展至 180（意象/意境/鲁迅/杜甫/李白/陶渊明/比兴 等）
- ✅ 生物学 141→246（孟德尔/达尔文/培养基 等）、历史 119→237（汉武帝/拿破仑 等）
- ✅ 物理 130→222（牛顿/爱因斯坦 等）、化学 124→217（阿伏加德罗/摩尔 等）
- ✅ 100% 清除垃圾概念（"是不是"/"老师"/"com"/"想一想" 等均已移除）
- ✅ 新增 60+ 通用词过滤规则，防止"词/曲/平面/速度"等非学术术语泄漏
- ✅ Unicode NFKC 规范化：正确捕获 σ键、α粒子、γ射线 等希腊字母术语

**D3.js 交互式知识图谱**
- ✅ `d3.forceSimulation()` 力导向物理布局，节点自动排列
- ✅ `d3.zoom()` 平滑缩放 & 平移（0.3× 至 5×）
- ✅ `d3.drag()` 拖拽节点重排
- ✅ 悬停高亮：非关联节点淡化 + 关联连线加粗 + 工具提示浮窗
- ✅ 重置按钮：一键恢复视角并重启布局

**数据洞察更新**
- ✅ 概念广度排名：古希腊(9科) → 现代化(8科) → 环境保护(8科)
- ✅ 词频分析、学科关联热力图、考试覆盖分析 全部基于新数据

### 2026-03-03: 语义关联引擎升级 + UI 重构

**关联发掘引擎**
- ✅ 引入 FAISS + `BAAI/bge-small-zh-v1.5`，65,978 条教材向量索引，实现稠密语义检索
- ✅ Jieba 中文分词替代暴力正则，搜索词质量大幅提升
- ✅ 清洗概念图谱数据库，删除 122 条 OCR/LaTeX 噪声概念
- ✅ IDF 加权评分 + 最低质量阈值 (≥15)，过滤无效关联
- ✅ KaTeX 公式渲染，所有学科公式正确显示

**前端 UI**
- ✅ 品牌重命名：跨学科知识平台 → **AI 高中教材**
- ✅ 首页标语更新：「这世界真的有学科吗？」
- ✅ 动态概念轮播：从 API 拉取高频跨学科概念，每 3 秒轮换 4 个
- ✅ 真题页新增教材下载入口 → jks.bdfz.net
- ✅ 关于页精简，移除重复的下载区域

**基础设施**
- ✅ Dockerfile 升级：安装 sentence-transformers + faiss-cpu + jieba
- ✅ Docker 镜像内置 BGE 模型（构建时预下载），启动即用

---

## 👤 作者

**孙玉磊** · 北大附中

- 🏫 [bdfz.net/posts/sun/](https://bdfz.net/posts/sun/)
- 📥 教材下载：[jks.bdfz.net](https://jks.bdfz.net/)

## 📄 License

MIT
