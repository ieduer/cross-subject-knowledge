# 🔗 AI 高中教材

> 这世界真的有学科吗？ — 发现高中 9 科教材中**隐藏的跨学科联系**，让 AI 帮你综合解读

**在线体验 → [sun.bdfz.net](https://sun.bdfz.net)**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## ✨ 核心功能

- 🔍 **跨学科搜索** — 搜索一个概念，按学科分组展示不同教材中的内容
- 💡 **自动关联提示** — 检测到概念横跨多学科时，自动提示跨学科联系
- ✨ **AI 跨学科解读** — 一键调用 Gemini，综合多学科教材内容生成带出处的解读
- 🗺️ **知识图谱** — D3.js 力导向交互式图谱，可视化 784 个学术概念在 9 学科间的关联网络，支持缩放/拖拽/悬停高亮
- 📊 **数据洞察** — 720 个精选学术术语的词频分析、学科关联热力图、考试覆盖分析、概念广度排名
- 📚 **教材下载** — 全部 316 本高中教材 PDF 可从 [jks.bdfz.net](https://jks.bdfz.net/) 下载

### 高级检索

- ⚙️ **高级搜索面板** — 按教材筛选、按排序方式切换（相关度 / 跨学科数 / 含图优先）
- 🔗 **相关概念推荐** — 搜索后自动推荐共现频率最高的相关概念，点击即搜
- 📷 **图片标注** — 搜索结果显示图片数量 badge，展开即可查看教材原图
- 📖 **教材筛选** — 按学科分组的 316 本教材下拉选择器，精准定位特定教材内容

---

## 📊 数据规模

| 指标 | 数值 |
|------|------|
| 教材总数 | **316 本**（人教版高中全科） |
| 学科覆盖 | **9 科**：语文、数学、英语、物理、化学、生物学、历史、地理、思想政治 |
| 结构化语料 | **70,007 条** chunks (包含 65,978 条教材知识 + **4,029 道高考真题**) |
| 高考真题 | **4,029 道** (2010-2024 全国/地方卷 + gk.bdfz.net 北京卷语文，含 904 张多模态题图) |
| 学术概念图谱 | **784 个**有效概念，1,714 条学科映射，83 个跨学科聚类 |
| 精选术语 | **720 个**精选学术术语，2,825 条频次统计 |
| 教材插图 | **87,156 张**（3.4 GB，由 R2 CDN 全球分发） |
| FTS 索引大小 | **142 MB**（SQLite FTS5） |
| Docker 镜像 | **467 MB**（仅代码 + 索引，图片走 CDN） |

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
    │               │   ├── BAAI/bge-small-zh-v1.5 ── 中文语义向量 (512D)
    │               │   ├── FAISS ── 65,978 向量稠密检索
    │               │   └── Jieba ── 中文分词 + 词性标注
    │               ├── 前端 (HTML/CSS/JS + D3.js + KaTeX)
    │               └── SQLite FTS5 索引 + 概念图谱 (142MB)
    │
    ├── HTTPS → img.rdfzer.com (Cloudflare R2 CDN)
    │           └── 87,156 张教材原图（3.4GB，全球加速，免费出站）
    │
    └── HTTPS → ai.bdfz.net (Cloudflare Worker)
                └── Gemini API → AI 跨学科综合解读
```

### Docker 内容

```
/app/
├── backend/main.py             # FastAPI 应用
├── frontend/
│   ├── index.html              # 主页（搜索/真题/图谱/数据/关于）
│   └── assets/
│       ├── style.css           # 暗色主题 + 响应式（640px/380px）
│       └── app.js              # D3.js 图谱 + 高级搜索 + AI 解读
└── data/index/
    ├── textbook_mineru_fts.db  # FTS5 索引 + 概念图谱 (142MB)
    └── textbook_chunks.index   # FAISS 向量索引 (61MB, 15,652 vectors)
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

**产物**：`data/index/textbook_mineru_fts.db` → **142 MB**

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
# 构建 Docker 镜像（仅含代码 + FTS 索引，不含图片）
docker build -t textbook-knowledge .

# 部署
docker run -d --name textbook-knowledge \
  --restart unless-stopped \
  -p 8080:8080 textbook-knowledge
```

---

## 📦 完整数据清单

### 本机（开发/处理机）

| 路径 | 大小 | 用途 | 可重建? |
|------|------|------|---------| 
| `data/raw_pdf/` | **31 GB** | 316 本原始 PDF | ❌ 需重新下载 |
| `data/mineru_output/` | **101 GB** | MinerU OCR 产物 | ✅ 从 PDF 重新生成（~20h） |
| `data/images/` | **3.4 GB** | 87K 张提取的教材图片 | ✅ 从 MinerU 产物提取 |
| `data/index/` | **308 MB** | FTS 索引 + chunks JSONL | ✅ 从 MinerU 产物重建 |

### 云端

| 服务 | 内容 | 大小 |
|------|------|------|
| VPS | Docker 容器（代码 + 索引） | 467 MB |
| Cloudflare R2 (`img.rdfzer.com`) | 87,156 张教材原图 | 3.4 GB |
| GitHub | 源代码 | < 1 MB |

---

## 🚀 快速开始

### 本地运行

```bash
pip install fastapi uvicorn sentence-transformers faiss-cpu jieba

# 将 textbook_mineru_fts.db 和 textbook_chunks.index 放到 data/index/ 目录
uvicorn backend.main:app --host 0.0.0.0 --port 8080
# 访问 http://localhost:8080
```

### Docker 运行

```bash
# 需要将 FTS 数据库和 FAISS 索引放到 data/ 目录
docker build -t textbook-knowledge .
docker run -p 8080:8080 textbook-knowledge
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
docker build -t textbook-knowledge .
docker run -d -p 8080:8080 --restart unless-stopped textbook-knowledge
```

---

## 🔄 VPS 迁移指南

### 最低配置

| 参数 | 最低 | 推荐 |
|---|---|---|
| CPU | 2 核 | 4 核 |
| 内存 | 2 GB | 4 GB |
| 磁盘 | 5 GB | 10 GB |
| OS | Ubuntu 22.04+ | Ubuntu 24.04 |
| Docker | 必须 | ✅ |

### 迁移步骤

```bash
# 1. 克隆仓库
git clone https://github.com/ieduer/cross-subject-knowledge.git
cd cross-subject-knowledge

# 2. 获取 FTS 数据库（从旧容器或本机复制）
docker cp textbook-knowledge:/app/data/index/textbook_mineru_fts.db data/

# 3. 获取 FAISS 向量索引
docker cp textbook-knowledge:/app/data/index/textbook_chunks.index data/

# 4. 构建并运行
docker build -t textbook-knowledge .
docker run -d --name textbook-knowledge --restart unless-stopped -p 8080:8080 textbook-knowledge

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
| AI 解读 | Gemini (via Cloudflare Worker) | `ai.bdfz.net` |
| 容器 | Docker | 单文件部署 |
| 数据备份 | rclone → Google Drive / R2 | |

### 后端及发掘技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| 中文向量模型 | `BAAI/bge-m3` | 1024D 多语言/长文本嵌入，2.2GB，全面提升语义理解度 |
| 向量检索 | `faiss-cpu` | 15,652 向量 IndexIDMap，61MB |
| API 缓存 | `cachetools` | TTLCache (5min, maxsize=64) 加速读密集型高频 API |
| 中文分词 | `jieba` + POS tagging | 启动时自动加载 `curated_keywords` 的 720 个学术术语为高权重用户词典，精准切词 |
| 自动部署 | GitHub Actions | 提交触发 CI/CD 自动连入 VPS 拉取并在 Docker 重建 |
| 概念图谱 | SQLite `concept_map` | 784 个学术概念，跨学科自动发现 |
| 全文检索 | SQLite FTS5 | Porter 分词器，OR 组合查询 |
| 评分算法 | 自定义 `_score_result` | IDF 加权词项匹配 + 概念命中 + 同学科加分，阈值 ≥15 |

**关联检索流程** (3 层混合)：
1. **概念图谱** → `_match_concepts` 从 concept_map 匹配学科核心概念
2. **IDF 加权 FTS** → `_extract_weighted_terms` Jieba 分词后按 IDF 权重排序，FTS5 搜索
3. **稠密向量** → `FAISS` 编码查询文本为 1024D 向量，搜索 top-K 近邻 (cosine > 0.55)

---

## 📋 更新日志

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
