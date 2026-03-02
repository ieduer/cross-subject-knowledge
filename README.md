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

---

## 📊 数据规模

| 指标 | 数值 |
|------|------|
| 教材总数 | **316 本**（人教版高中全科） |
| 学科覆盖 | **9 科**：语文、数学、英语、物理、化学、生物学、历史、地理、思想政治 |
| 结构化语料 | **65,978 条** chunks |
| FTS 索引大小 | **148 MB**（SQLite FTS5） |
| Docker 镜像 | **336 MB**（含索引 + 代码 + 运行时） |

### 各学科语料分布

| 学科 | 语料数 | 学科 | 语料数 |
|------|--------|------|--------|
| 英语 | 15,425 | 化学 | 5,262 |
| 数学 | 12,567 | 思想政治 | 3,444 |
| 生物学 | 8,890 | 历史 | 3,150 |
| 物理 | 8,305 | 语文 | 1,228 |
| 地理 | 7,707 | | |

---

## 🏗️ 系统架构

```
用户浏览器
    │
    ├── HTTPS → sun.bdfz.net (nginx + Let's Encrypt)
    │           │
    │           └── Docker: textbook-knowledge
    │               ├── FastAPI 后端 (Python 3.13)
    │               │   ├── /api/search ── FTS5 全文搜索
    │               │   ├── /api/stats ─── 学科统计
    │               │   └── /api/cross-links ── 图谱数据
    │               ├── 前端 (HTML/CSS/JS)
    │               └── SQLite FTS5 索引 (148MB, baked in image)
    │
    └── HTTPS → Cloudflare Worker
                └── Gemini API → AI 跨学科综合解读
```

### Docker 内容（6 个文件，187MB）

```
/app/
├── backend/main.py           # FastAPI 应用
├── frontend/
│   ├── index.html            # 主页（搜索/图谱/关于）
│   └── assets/
│       ├── style.css         # 暗色主题
│       └── app.js            # 交互逻辑 + AI 调用
└── data/index/
    └── textbook_mineru_fts.db  # FTS5 索引（148MB）
```

> ⚠️ **Docker 中不包含**：原始 PDF、MinerU OCR 产物（Markdown + 图片）、旧解析数据。仅包含搜索索引。

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
- 🖼️ **图片提取**：自动裁切并保存引用
- 🇨🇳 **中文优化**：扫描件 OCR 准确率远超 Tesseract
- ⚡ **GPU 加速**：CUDA + PyTorch，处理速度 5-10x

**脚本**：`scripts/08_mineru_batch.py`

```python
# 核心参数
MINERU_BIN = ".venv-mineru/bin/mineru"
BACKEND = "pipeline"          # VLM 需要 >8GB VRAM，pipeline 适合 6GB 显卡
MAX_BOOKS_PER_RUN = 999       # 全量处理
NICE = 10                     # 低优先级，不影响系统
```

**特性**：
- **幂等执行**：JSON 状态文件 (`mineru_state.json`) 记录每本书的处理状态
- **失败熔断**：连续 3 本失败则跳过该批次
- **自动分块**：将 Markdown 按 `##` 标题拆分为 chunks，附带元数据（书名、学科、章节号）

**环境要求**：
- GPU: NVIDIA RTX 3060 (6GB VRAM) 或更高
- CUDA: 12.4
- Python: 3.13 + venv (`.venv-mineru`)
- 处理时间: **20.5 小时**（316 本，零失败）

**产物**：
| 目录 | 大小 | 内容 |
|------|------|------|
| `data/mineru_output/` | **101 GB** | 每本书的 Markdown + 提取的图片 |
| `data/index/mineru_chunks.jsonl` | **95 MB** | 65,978 条结构化 chunk（JSON Lines） |

**chunk 格式**：
```json
{
  "book_key": "高中_化学_普通高中教科书_化学必修_第一册",
  "subject": "化学",
  "title": "普通高中教科书·化学必修 第一册",
  "section": 42,
  "text": "原子核外电子排布与元素性质的关系..."
}
```

### Phase 3: 索引构建

**脚本**：`scripts/09_build_unified_index.py`

- 读取 `mineru_chunks.jsonl`
- 创建 SQLite FTS5 虚拟表（支持中文全文检索）
- 学科分类：从 `book_key` 路径自动提取（9 科全覆盖，0 条未分类）

**SQL Schema**：
```sql
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    subject TEXT,     -- 学科: 化学/物理/数学/...
    title TEXT,       -- 书名
    book_key TEXT,    -- 唯一标识: 学段_学科_书名
    section INTEGER,  -- 章节序号
    text TEXT          -- 正文内容
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content=chunks, content_rowid=id);
```

**产物**：`data/index/textbook_mineru_fts.db` → **148 MB**

### Phase 4: 平台搭建

**后端** (`backend/main.py`)：
- FastAPI + uvicorn
- 3 个 API：`/api/search`、`/api/stats`、`/api/cross-links`
- 搜索结果按学科分组，自动检测跨学科关联并生成提示

**前端** (`frontend/`)：
- 原生 HTML/CSS/JS，无框架依赖
- 暗色主题 + glassmorphism 设计
- SVG 知识图谱（无 D3 依赖）
- AI 解读面板：通过 Cloudflare Worker 调用 Gemini API

### Phase 5: 部署

```bash
# 构建 Docker 镜像（FTS 索引 baked in）
docker build -t textbook-knowledge .

# 部署到 VPS
docker run -d --name textbook-knowledge \
  --restart unless-stopped \
  -p 8080:8080 textbook-knowledge

# nginx 反向代理 + Let's Encrypt SSL
certbot --nginx -d sun.bdfz.net
```

---

## 📦 完整数据清单

### 本机（开发/处理机）

| 路径 | 大小 | 用途 | 可重建? |
|------|------|------|---------|
| `data/raw_pdf/` | **31 GB** | 316 本原始 PDF | ❌ 需重新下载 |
| `data/mineru_output/` | **101 GB** | MinerU OCR 产物（MD + 图片） | ✅ 从 PDF 重新生成（~20h） |
| `data/parsed/` | **42 GB** | 旧 PyMuPDF 产物（已弃用） | 🗑️ 可删除 |
| `data/index/` | **308 MB** | FTS 索引 + chunks JSONL | ✅ 从 MinerU 产物重建 |
| `scripts/` | **< 1 MB** | 处理脚本 | ✅ GitHub |
| `platform/` | **187 MB** | 代码 + 索引副本 | ✅ GitHub |
| **合计** | **~209 GB** | | |

### VPS

| 项 | 大小 |
|---|---|
| Docker 镜像 | **695 MB** |
| 运行时数据 | **~200 MB** |
| 磁盘总用量 | **29 GB / 99 GB** |

### Google Drive 备份

| 路径 | 大小 | 状态 |
|------|------|------|
| `textbook_ai_backup/raw_pdf/` | 31 GB | 上传中 |
| `textbook_ai_backup/index/` | 308 MB | 上传中 |
| `textbook_ai_backup/scripts/` | < 1 MB | ✅ 完成 |

---

## 🚀 快速开始

### 本地运行（需要 FTS 索引）

```bash
pip install fastapi uvicorn
# 将 textbook_mineru_fts.db 放到 data/index/ 目录
cd platform
uvicorn backend.main:app --host 0.0.0.0 --port 8080
# 访问 http://localhost:8080
```

### Docker 运行

```bash
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

# 5. 部署
docker build -t textbook-knowledge platform/
docker run -p 8080:8080 textbook-knowledge
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
# 1. 在新 VPS 安装 Docker
curl -fsSL https://get.docker.com | sh

# 2. 传输镜像
scp textbook-knowledge.tar.gz root@新IP:/tmp/

# 3. 加载并运行
cd /tmp && gunzip -c textbook-knowledge.tar.gz | docker load
docker run -d --name textbook-knowledge --restart unless-stopped -p 8080:8080 textbook-knowledge

# 4. 安装 nginx + SSL
apt install -y nginx certbot python3-certbot-nginx
# 配置 nginx server block → proxy_pass http://127.0.0.1:8080
certbot --nginx -d sun.bdfz.net

# 5. 更新 DNS
# Cloudflare: sun.bdfz.net A → 新 IP
```

---

## 🛠️ 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| OCR 引擎 | [MinerU](https://github.com/opendatalab/MinerU) | v2.7.6 |
| 全文检索 | SQLite FTS5 | - |
| 后端 | FastAPI + uvicorn | Python 3.13 |
| 前端 | Vanilla HTML/CSS/JS | 无框架 |
| AI 解读 | Gemini (via Cloudflare Worker) | - |
| 容器 | Docker | 29.2 |
| 反代 / SSL | nginx + Let's Encrypt | - |
| 数据备份 | rclone → Google Drive | v1.73 |

---

## 👤 作者

**孙玉磊** · 北大附中

- 🏫 [bdfz.net/posts/sun/](https://bdfz.net/posts/sun/)
- 📥 教材下载：[jks.bdfz.net](https://jks.bdfz.net/)

## 📄 License

MIT
