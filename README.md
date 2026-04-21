# AI 中学教材

跨学科教材检索与 AI 解读平台，面向初高中 9 科教材、真题与查典场景。

在线体验：
- [sun.bdfz.net](https://sun.bdfz.net)
- [sun.bdfz.net/chuzhong.html](https://sun.bdfz.net/chuzhong.html)

## 核心能力

- 混合检索：`SQLite FTS + FAISS + supplemental page index + reranker`
- AI 解读：定义型、比较型、过程型问法优先走后端 precision path
- 查典：实词 / 虚词、教育部《重编国语辞典修订本》、教育部《成語典》
- 页图回链：教材结果可回到 CDN 页图，公开范围内的补充教材也支持映射
- 学段隔离：高中与初中数据、热门词、图谱与 AI 上下文分开处理

## 当前公开范围

- 主教材：`人教版全部`
- 补充公开版本：`英语·北师大版`、`化学·鲁科版`
- 其余并行版本保留在审计 / 运行时资产中，但不作为当前公开产品范围

## 本地运行

运行前先确认 `data/index/` 下已经有运行时资产，至少包括：

- `textbook_mineru_fts.db`
- `textbook_chunks.index`
- `textbook_chunks.manifest.json`
- `supplemental_textbook_pages.jsonl.gz`
- `supplemental_textbook_pages.manifest.json`

安装依赖并启动：

```bash
pip install -r requirements.runtime.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

快速检查：

```bash
curl -fsS http://127.0.0.1:8080/api/health | jq
```

## 仓库结构

- `backend/`: FastAPI 后端、检索逻辑、词典接口、健康检查
- `frontend/`: 静态前端页面与页图映射
- `data/index/`: 运行时 SQLite / FAISS / manifest 资产
- `scripts/`: 构建、校验、发布、manifest 与索引维护脚本
- `docs/`: 运维、数据沿革、发布规则与版本审计说明

## 运维与发布文档

- [维护手册](docs/MAINTENANCE_MANUAL.md)
- [运行时与运维边界](docs/runtime_operations_overview.md)
- [数据沿革记忆](docs/data_layer_lineage_memory.md)
- [发布维护设计](docs/release_maintenance_design.md)
- [教材版本审计](docs/textbook_identity_audit.md)

## 发布规则

- `main` 是生产发布分支
- GitHub Actions 从 clean checkout 执行 `scripts/deploy_vps.sh`
- `README.md` 和 `docs/**` 属于 docs-only 变更，不触发自动部署
- 运行时主检索库默认禁止启动时远端同步；数据更新必须显式同步后再部署

## 维护入口

日常维护、日志排查、健康检查、回滚与发布前核查，统一看：

- [docs/MAINTENANCE_MANUAL.md](docs/MAINTENANCE_MANUAL.md)

涉及数据重建、版本对账或“同一本 vs 不同版本”判断时，再继续看：

- [docs/data_layer_lineage_memory.md](docs/data_layer_lineage_memory.md)
- [docs/textbook_identity_audit.md](docs/textbook_identity_audit.md)
