# 🔗 跨学科教材知识平台

> 发现高中 9 科教材中**隐藏的跨学科联系**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## ✨ 特点

- 🔍 **跨学科搜索** — 搜索一个概念，看到它在不同学科的呈现
- 💡 **关联提示** — 自动发现横跨多学科的知识关联
- 🗺️ **知识图谱** — 可视化学科间的概念连接
- 📊 65,978 条结构化语料，覆盖 316 本高中教材

## 🚀 快速开始

### 本地运行

```bash
pip install fastapi uvicorn
cd platform
uvicorn backend.main:app --host 0.0.0.0 --port 8080
# 访问 http://localhost:8080
```

### Docker

```bash
cd platform
docker build -t textbook-knowledge .
docker run -p 8080:8080 textbook-knowledge
```

## 📁 项目结构

```
platform/
├── backend/main.py      # FastAPI 后端
├── frontend/            # 前端界面
│   ├── index.html
│   └── assets/
├── data/                # FTS 索引数据库
├── Dockerfile
└── README.md
scripts/                 # 数据处理脚本
├── 08_mineru_batch.py   # MinerU 批量 OCR
└── 09_build_unified_index.py
```

## 🛠️ 技术栈

| 组件 | 技术 |
|------|------|
| OCR 引擎 | [MinerU](https://github.com/opendatalab/MinerU) (55k⭐) |
| 全文检索 | SQLite FTS5 |
| 后端 | FastAPI (Python) |
| 前端 | Vanilla HTML/CSS/JS |

## 📄 License

MIT
