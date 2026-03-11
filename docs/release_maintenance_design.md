# 发布维护设计

## 目标

这份设计只解决一个问题：

- 让未来的发布与维护不再依赖“记得别从 VPS runtime repo 出包”这种口头约束

不做架构重写，不改变当前 `main -> GitHub Actions -> VPS clean checkout -> deploy_vps.sh` 的主发布路径。

## 已暴露的问题

`2026-03-11` 的事故已经证明，当前系统最大的维护风险不是功能实现本身，而是发布源漂移：

- 生产运行目录 `/root/cross-subject-knowledge` 可能长期脏工作树
- `textbook-knowledge:latest` 不一定等于当前运行中的容器镜像
- 主站“查看原文”虽然真正图片在 CDN，但运行中的 `page_url` 仍依赖镜像内 `frontend/assets/pages/book_map.json`
- 一旦人工紧急发布绕过 clean release source，就可能把旧前端壳和新局部文件拼成错误镜像

这类问题不能靠“以后小心点”解决，必须把错误路径变成默认失败。

## 设计原则

### 1. `main` 继续是唯一公开部署分支

- `main` 仍对应可部署状态
- GitHub Actions 仍只从 `main` 构建生产镜像
- `README.md / docs/**` 继续作为 docs-only 例外，不触发生产部署

这条不变，避免引入新的发布拓扑。

### 2. 手工发布必须有标准 clean release source

新增脚本：

- `platform/scripts/stage_clean_release.py`
- `platform/scripts/build_release_manifest.py`
- `platform/scripts/verify_release_manifest.py`

作用：

- 从本机源头状态生成 `release_manifest.json`
- 从当前 repo 明确抽取 runtime 需要的最小文件集合
- 生成一个干净目录
- 在发布前核对 source tree 与 VPS runtime data 是否和本次 manifest 一致
- 可选再打成 `tar.gz`

这样，手工紧急修复不再依赖“自己记得复制哪些文件”。

### 2.5. 运行时主检索库不再允许隐式同步

这次排查还暴露了另一条真正会制造状态漂移的链路：

- 容器启动时自动运行 `backend/sync_db.py`
- `sync_db.py` 会尝试从 R2 `db-sync/` 路径下载 `textbook_mineru_fts.db`

这条链路直接破坏了四端对齐模型：

- 本机、GitHub、VPS、R2 可以在没有显式发布动作的情况下继续分叉
- 网站有效并不能证明 VPS `data/index/` 仍等于本轮验收版本
- 代码发布会夹带运行时数据改写，回滚与验收都失去边界

因此本轮的固定约束是：

- 生产容器默认 `RUNTIME_DB_SYNC_MODE=disabled`
- `deploy_vps.sh` 强制要求 `RUNTIME_DB_SYNC_MODE=disabled`
- `sync_db.py` 只保留为显式应急工具，必须手动设置 `RUNTIME_DB_SYNC_MODE=r2_textbook_mineru` 才会执行

未来运行时主检索库的唯一正常更新方式是：

1. 本机生成或确认工件
2. 显式同步到 VPS `data/index/`
3. 再部署或重启容器

而不是让容器在启动时自行改写数据盘。

另一个必须同时固定下来的点是 `textbook_mineru_fts.db` 的“活文件”属性：

- 线上流量会继续写 `search_logs` / `ai_chat_logs`
- 因此整库 SHA 不能长期作为唯一发布对账依据

本轮已把 manifest / verify 规则收成：

- 保留整库 SHA 作为信息字段
- 对真正的发布校验，改用稳定的 `runtime_identity`
- `runtime_identity` 只覆盖检索和知识运行所需的核心内容表与 FTS 影子表计数，不把运行时日志表计入错版判断

### 3. 部署脚本必须拒绝坏包

`platform/scripts/deploy_vps.sh` 现在新增硬性拦截：

- source tree 缺 `release_manifest.json` 时直接失败
- source tree 缺 `frontend/assets/pages/book_map.json` 时直接失败
- source tree 缺 `frontend/assets/version.json` 时直接失败
- source tree 或 VPS runtime data 与 `release_manifest.json` 对账失败时直接失败
- `docker build` 完成后，若镜像内缺：
  - `/app/frontend/assets/pages/book_map.json`
  - `/app/frontend/assets/version.json`
  - `/app/frontend/index.html`
  - `/app/frontend/dict.html`
  则直接失败

这意味着未来即使有人再从错误源构建，也会在 cutover 前被 deploy script 拦下。

### 4. 回滚锚点必须显式化

发布前若需要人工回滚点，规则固定为：

1. 先取当前 running container 的 image digest
2. 再额外打一个人工 tag
3. 不把 `latest` 视为当前线上等价物

这条写入报告，也作为之后的固定口径。

## 新的维护方式

### 常规发布

1. 在功能分支完成开发与校验
2. 合并到 `main`
3. 由 GitHub Actions 在 VPS 上做 clean checkout 并发布

### docs-only 更新

1. 只改 `README.md / docs/**`
2. 推 `main`
3. workflow 因 `paths-ignore` 不触发生产部署

### 紧急人工发布

1. 本地先运行 `platform/scripts/build_release_manifest.py`
2. 再运行 `platform/scripts/stage_clean_release.py`
3. 生成 clean release 目录或压缩包
4. 若需要，先给 running image digest 打人工 rollback tag
5. 在 VPS 的临时目录中解包 clean release
6. 从这个临时目录执行 `scripts/deploy_vps.sh`

这里的关键不是“临时目录”本身，而是：

- 发布源必须来自 clean release bundle
- 不能再从 runtime repo 就地派生

## 当前最小落地结果

本次已落地的，不是未来才做的方案，而是现在已经可用的维护脚手架：

- `platform/scripts/stage_clean_release.py`
- `platform/scripts/build_release_manifest.py`
- `platform/scripts/verify_release_manifest.py`
- `platform/scripts/deploy_vps.sh` 的坏包拦截
- 各报告对 `book_map.json`、`latest`、manual rollback anchor 的显式说明

## 后续可继续增强，但不是本轮必做

如果后面继续收敛发布维护，还可以增加：

- PR 或 `main` 上的 release-readiness check
- 机器可读的 runtime artifact manifest 与 hash 对账
- 将 emergency release bundle 的生成与验证变成单命令封装

这些是增强项，不是当前修复站点和维持可维护性的前置条件。
