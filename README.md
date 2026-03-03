# 🔗 跨学科教材知识平台

> 发现高中 9 科教材中**隐藏的跨学科联系**，让 AI 帮你综合解读

**在线体验 → [sun.bdfz.net](https://sun.bdfz.net)**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## ✨ 核心功能

- 🔍 **跨学科搜索** — 搜索一个概念，按学科分组展示不同教材中的内容
- 💡 **自动关联提示** — 检测到概念横跨多学科时，自动提示跨学科联系
- ✨ **AI 跨学科解读** — 一键调用 Gemini，综合多学科教材内容生成带出处的解读
- 🗺️ **知识图谱** — 可视化 30+ 核心概念在 9 学科间的关联网络
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
| 结构化语料 | **65,978 条** chunks |
| 教材插图 | **87,156 张**（3.4 GB，由 R2 CDN 全球分发） |
| FTS 索引大小 | **187 MB**（SQLite FTS5） |
| Docker 镜像 | **467 MB**（仅代码 + 索引，图片走 CDN） |

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
    ├── HTTPS → sun.bdfz.net (VPS 23.19.231.173)
    │           │
    │           └── Docker: textbook-knowledge (467MB)
    │               ├── FastAPI 后端 (Python 3.13)
    │               │   ├── /api/search ─── FTS5 全文搜索（支持筛选/排序）
    │               │   ├── /api/books ──── 316 本教材列表（按学科分组）
    │               │   ├── /api/related ── 相关概念推荐（共现分析）
    │               │   ├── /api/stats ──── 学科统计
    │               │   └── /api/cross-links ── 知识图谱数据
    │               ├── 前端 (HTML/CSS/JS)
    │               └── SQLite FTS5 索引 (187MB, baked in image)
    │
    ├── HTTPS → img.rdfzer.com (Cloudflare R2 CDN)
    │           └── 87,156 张教材原图（3.4GB，全球加速，免费出站）
    │
    └── HTTPS → ai.bdfz.net (Cloudflare Worker)
                └── Gemini API → AI 跨学科综合解读
```

### API 概览

| 端点 | 参数 | 说明 |
|------|------|------|
| `GET /api/search` | `q`, `subject`, `book_key`, `sort`, `has_images`, `limit`, `offset` | 全文搜索 + 跨学科分组 |
| `GET /api/books` | — | 全部教材列表（按学科分组） |
| `GET /api/related` | `q`, `limit` | 相关概念推荐（基于共现频率） |
| `GET /api/stats` | — | 各学科语料统计 |
| `GET /api/cross-links` | — | 知识图谱节点与连接 |

### Docker 内容

```
/app/
├── backend/main.py             # FastAPI 应用（5 个 API）
├── frontend/
│   ├── index.html              # 主页（搜索/图谱/关于）
│   └── assets/
│       ├── style.css           # 暗色主题 + 响应式（640px/380px）
│       └── app.js              # 高级搜索 + AI 调用 + 图谱
└── data/index/
    └── textbook_mineru_fts.db  # FTS5 索引（187MB）
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

**产物**：`data/index/textbook_mineru_fts.db` → **187 MB**

### Phase 4: 图片上传

```bash
# 使用 rclone 批量上传到 Cloudflare R2
rclone sync data/images/ r2:textbook-images/orig/ --transfers 16 --progress
# 87,156 张图片，3.4 GB，R2 免费额度内
```

**R2 成本**：完全免费（3.4GB 存储在 10GB 免费额度内，出站流量永远免费）

### Phase 5: 部署

```bash
# 构建 Docker 镜像（仅含代码 + FTS 索引，不含图片）
docker build -t textbook-knowledge .

# 部署到 VPS
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
| VPS (23.19.231.173) | Docker 容器（代码 + 索引） | 467 MB |
| Cloudflare R2 (`img.rdfzer.com`) | 87,156 张教材原图 | 3.4 GB |
| GitHub | 源代码 | < 1 MB |

---

## 🚀 快速开始

### 本地运行

```bash
pip install fastapi uvicorn

# 将 textbook_mineru_fts.db 放到 data/index/ 目录
uvicorn backend.main:app --host 0.0.0.0 --port 8080
# 访问 http://localhost:8080
```

### Docker 运行

```bash
# 需要将 FTS 数据库放到 data/ 目录
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

# 5. 上传图片到 R2
rclone sync data/images/ r2:textbook-images/orig/ --transfers 16

# 6. 部署
docker build -t textbook-knowledge .
docker run -d -p 8080:8080 --restart unless-stopped textbook-knowledge
```

---

## 🔄 VPS 迁移指南

### 最低配置

| 参数 | 最低 | 推荐 |
|---|---|---|
| CPU | 1 核 | 2 核 |
| 内存 | 512 MB | 1 GB |
| 磁盘 | 2 GB | 5 GB |
| OS | Ubuntu 22.04+ | Ubuntu 24.04 |
| Docker | 必须 | ✅ |

### 迁移步骤

```bash
# 1. 克隆仓库
git clone https://github.com/ieduer/cross-subject-knowledge.git
cd cross-subject-knowledge

# 2. 获取 FTS 数据库（从旧容器或本机复制）
docker cp textbook-knowledge:/app/data/index/textbook_mineru_fts.db data/

# 3. 构建并运行
docker build -t textbook-knowledge .
docker run -d --name textbook-knowledge --restart unless-stopped -p 8080:8080 textbook-knowledge

# 4. 可选：nginx + SSL
apt install -y nginx certbot python3-certbot-nginx
certbot --nginx -d sun.bdfz.net
```

---

## 🛠️ 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| OCR 引擎 | [MinerU](https://github.com/opendatalab/MinerU) | v2.7.6 |
| 全文检索 | SQLite FTS5 | — |
| 后端 | FastAPI + uvicorn | Python 3.13 |
| 前端 | Vanilla HTML/CSS/JS | 无框架 |
| 图片 CDN | Cloudflare R2 | `img.rdfzer.com` |
| AI 解读 | Gemini (via Cloudflare Worker) | `ai.bdfz.net` |
| 容器 | Docker | 29.2 |
| 数据备份 | rclone → Google Drive / R2 | v1.73 |

---

## 👤 作者

**孙玉磊** · 北大附中

- 🏫 [bdfz.net/posts/sun/](https://bdfz.net/posts/sun/)
- 📥 教材下载：[jks.bdfz.net](https://jks.bdfz.net/)

## 📄 License

MIT
