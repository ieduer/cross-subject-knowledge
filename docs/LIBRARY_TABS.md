# SUN 的两个分页：「PDF 预览」与「教材下载」

任务 `sun-consolidation-20261010`，工作流 WS-B。本文件是这两个分页的运维说明：消费的数据契约、端点、nginx 要求、环境变量、限额、目录发行版切换与回退、排错。

## 1. 组成

| 部分 | 文件 | 说明 |
| --- | --- | --- |
| 后端 | `backend/library.py` | FastAPI `APIRouter`；只用 FastAPI／Starlette／标准库，不新增运行期依赖 |
| 挂载 | `backend/main.py` 两行（标 `# library-tabs`） | `import library as sun_library`、`sun_library.install(app, data_root=DATA_ROOT, frontend_dir=FRONTEND)` |
| 前端 | `frontend/assets/library.js`、`library.css` | 普通脚本（与 `app.js` 同样不用模块、无构建步骤、无内联事件、无 `eval`） |
| PDF.js | `frontend/vendor/pdfjs/` | pdfjs-dist 4.10.38 的 legacy 构建；版本、npm integrity、逐档 sha256 见 `VENDOR.json`；运行时不连 CDN |
| 页面挂载 | `index.html`、`chuzhong.html` 各 6 行（标 `<!-- library-tabs -->`）；`app.js` 1 行（标 `// library-tabs`） | 样式表、两个导航钮、两个空的 view 容器、脚本；检索结果加「在 PDF 中打开」的钩子 |
| 测试 | `tests/library/` | `python -m unittest discover -s tests/library` |
| 本机开发 | `scripts/dev_library_server.py` | 见 §8 |
| nginx 范例 | `docs/ops/nginx-library.conf.example` | 未安装，仅供审阅 |

`install()` 另把 `frontend/vendor/` 挂到 `/vendor`（`/assets` 已由 `main.py` 挂载）。目录缺失或损坏不会让 SUN 启动失败：两个分页显示「目录尚未发布／不可用」，其余功能不受影响。

两个分页不调用任何 AI 端点，不写 cookie，不记录查询。册内检索用的是既有的 `GET /api/search`（带 `book_key`），该端点原本就会写 `search_logs`，本模块没有新增记录。

## 2. 消费的数据契约（`sun-catalog-v1`）

位置：`${DATA_ROOT}/catalog/CURRENT`（单行 release id）→ `${DATA_ROOT}/catalog/releases/<release_id>/`。

| 文件 | 读取方式 |
| --- | --- |
| `SHA256SUMS` | 逐行 `<sha256>  <相对路径>`；列出的每个文件都重新计算并比对；必须涵盖 `catalog.json`、`downloads.json`、`RELEASE.json`；绝对路径或含 `..` 的条目使整个发行版无效 |
| `RELEASE.json` | `release_id` 必须等于目录名；`contract`（或 `schema`）若有，必须是 `sun-catalog-v1`；取 `generated_at` |
| `catalog.json` | `{contract, release_id, editions:[…]}`（也接受裸数组） |
| `downloads.json` | `{contract:"sun-catalog-v1/downloads", release_id, rows:[…]}`（`rows` 也可写作 `downloads`，也接受裸数组） |
| `page-maps/<edition_id>.json` | `{edition_id, pdf_sha256, page_map_version, pages:[…]}`；必须列在 `SHA256SUMS` 里 |

版次记录中实际使用的字段：

| 字段 | 用途与校验 |
| --- | --- |
| `edition_id` | 身份；`^[a-z0-9][a-z0-9._-]{2,95}$`；重复即无效 |
| `status` | `active`／`superseded`／`pending-verification`；书目只列 `active` |
| `pdf` | `{sha256(64 hex), bytes>0, pages 1–5000, has_text_layer}`；可为 `null`（已编目未取得，只能是非 `active`） |
| `storage.vps` | 非 `null` 才算「本站已存」＝可预览、可由本站下载。路径不读，文件名一律由 `pdf.sha256` 推得 |
| `storage.drive` | 仅当 `shared === true` 且网址是 `https://drive.google.com`（或 `drive.usercontent.google.com`、`docs.google.com`）才提供 |
| `legacy.sun_book_key` | 把 SUN 检索结果与旧 JC 链接对到版次；只按这个识别码，从不按书名 |
| `legacy.jc_short_key` | 12 位十六进制；与 `md5(sun_book_key)[:12]` 一并建索引 |
| `legacy.jks_slug`、`legacy.jks_pages` | 旧 JKS 链接；见 §3「页码」 |
| `corpus.relation` | 只有 `same-bytes`／`aligned`／`partial` 才允许用页对应表做旧页码换算 |
| `work_id` | 同一 `work_id` 的其他版次在阅读器里分开列出，不合并 |
| `stage`、`subject`、`title`、`volume`、`publisher`、`edition_label`、`region` | 展示与筛选；缺者为 `null`，不猜（`title` 缺时显示「未命名教材」） |

页对应表每页一笔，必须正好是 `pdf_page = 1…pdf.pages`：

| 字段 | 规则 |
| --- | --- |
| `printed_label`、`printed_verified` | 只有 `printed_verified === true` 的页才会把书页送到前端；`true` 而无 `printed_label` 视为无效 |
| `legacy_image_index` | 0 起算的 SUN／JC 页图序号；同册内不得重复；`null` 表示未证明 |
| `ocr_page` | 透传 |
| `jks_page`（可选） | 旧 JKS 阅读器的页码；有才用 |

下载记录：`{source_id, stage, subject, title, edition_label, publisher, cover, size_bytes, sources, edition_id, held_other_edition_ids?}`。

- `edition_id` 非 `null` 时必须指向本发行版里的版次；该版次本站已存时，「本站」来源由服务端生成（`/files/pdf/…?download=1`），记录里写的 `vps` 网址一律不用。
- `sources` 里的 `upstream` 网址只放行 `LIBRARY_UPSTREAM_HOSTS`（预设 `jks-ai.bdfz.net`、`smartedu.cn`、`ykt.cbern.com.cn` 及其子域）的 `https`；其余丢弃并计入 `counts.dropped_sources`。
- `cover` 只放行 `LIBRARY_COVER_HOSTS` 的 `https`，否则为 `null`（前端显示学科占位）。
- 本站已存、却没有任何下载记录指向的版次（例如官方平台已在同一识别码下换档），会自动成为独立的一笔下载记录，只有「本站」来源。不同版次从不并成同一笔。

任何一项校验失败，整个发行版不载入；`/api/library/release` 的 `reason`／`pending_error` 给出原因（不含本机路径）。

## 3. 页码

同一册有三种数字，互不换算：

| 名称 | 含义 | 出现处 |
| --- | --- | --- |
| `pdf_page` | PDF 中的第几页，1 起算 | 界面一律显示「PDF 第 N/M 页」；网址参数 `p` |
| `printed_label` | 书上印的页码 | 只在该页 `printed_verified` 时显示「书页 p.X · 已核对」；「跳到书页」只在本册有已核对页时出现 |
| `legacy_image_index` | SUN 检索结果的 `section`、JC 的 `#page`，0 起算 | 只作为查表的键，不显示 |

没有任何位移假设。旧页码只经由页对应表的一行换成 `pdf_page`；查不到就回 `available:false`（检索结果不出现「在 PDF 中打开」），旧链接则打开第 1 页并显示说明。两个现行版次都声称同一页时回 `ambiguous`，同样不猜。JKS 旧页码只有在页对应表带 `jks_page`，或版次声明 `legacy.jks_pages == "pdf_page"`（该 PDF 的页序就是 JKS 的页码）时才算确切。

前端另有一道防线：打开的 PDF 实际页数与目录不符时，停用书页与检索定位，只留 PDF 页序。

## 4. 端点

全部只读、无登入、同源。JSON 回应带 `X-Library-Release`，正文带 `release_id`。目录不可用时除 `/api/library/release` 外一律 `503`（`Retry-After: 60`）。

| 端点 | 参数（上限） | 回应 |
| --- | --- | --- |
| `GET /api/library/release` | – | `available`、`release_id`、`generated_at`、`counts`、`coverage`、`stages`、`regions`、`pending_error` |
| `GET /api/library/editions` | `phase`、`subject`（各 ≤60 字）、`q`（≤80 字，空白分词，全部命中）、`region`、`offset`（≤100000）、`limit`（1–60，预设 60） | 本站已存且 `active` 的版次：`total`、`subjects`（该学段的学科计数）、`editions[]`（含 `badges`） |
| `GET /api/library/editions/{edition_id}` | – | 完整记录、`file`（预览与下载网址）、`page_map` 摘要、本版次的其他来源、同一 `work_id` 的其他版次 |
| `GET /api/library/editions/{edition_id}/pages` | `from`、`to`（跨度 ≤200） | 页对应表的行 |
| `GET /api/library/resolve` | 三种形式择一：`book_key`（≤400 字）＋`section`；`items=<shortkey>.<section>,…`（≤40 项，`shortkey` 取自检索结果的页图网址）；`edition_id`＋`sections=1,2,…`（≤40 项，册内检索用） | `available`、`edition_id`、`pdf_page`，或 `reason`：`unknown-book`／`no-preview`／`page-not-mapped`／`ambiguous` |
| `GET /api/library/legacy` | `kind=jc|jks`、`ref`（JC 的书 id＝`book_key`，或 JKS slug）、`page` | `available`、`exact`、`edition_id`、`pdf_page`、`reason` |
| `GET /api/library/downloads` | `stage`、`subject`、`publisher`、`q`、`region`、`offset`、`limit`（1–60，预设 30） | `total`、`downloads[]`（`sources` 依 本站→云端硬盘→官方平台 排序） |
| `GET /api/library/downloads/facets` | `stage`、`subject`、`region` | `stages`、`subjects`、`publishers`（各带计数） |
| `GET`／`HEAD /files/pdf/{edition_id}/{sha12}/{filename}` | `download=1` | 见下 |

PDF 端点：`edition_id` 必须在已载入的发行版内、本站已存，且 `sha12` 等于其 `pdf.sha256` 前 12 位，否则 `404`。`filename` 只是让网址好看，服务端不读；`Content-Disposition` 的文件名由书名与版次标签在服务端生成。磁盘路径只由目录里的 sha256 组成，请求中没有任何内容进入路径。

| `PDF_SERVE_MODE` | 行为 |
| --- | --- |
| `accel`（预设，正式环境） | 回 `200`、空正文、`X-Accel-Redirect: /_protected_pdf/<sha256>.pdf`，并带 `Content-Type: application/pdf`、`Content-Disposition`、`Cache-Control: public, max-age=31536000, immutable`。Range、`Content-Length`、校验器由 nginx 的内部位置负责。`LIBRARY_PDF_ROOT` 若在容器内可见，会先确认文件存在（缺则 `404 file missing`）；不可见则以目录为准 |
| `direct`（本机开发、测试） | 应用自己送档：`Accept-Ranges: bytes`、单一范围 `206`＋`Content-Range`、不合法范围 `416`、多范围退回整档 `200`、`ETag`＝sha256、`If-Range` 不符退回 `200`、`If-None-Match` 命中 `304`、支持 `HEAD` |

nginx 需要的内部位置与建议的 `limit_req` 见 `docs/ops/nginx-library.conf.example`。要点：`/_protected_pdf/` 必须是 `internal`，`alias /srv/sun-textbooks/pdf/`，该目录要让 nginx 的执行用户读得到（nginx 读不到 `/root`）。

## 5. 环境变量

| 变量 | 预设 | 说明 |
| --- | --- | --- |
| `LIBRARY_CATALOG_ROOT` | `${DATA_ROOT}/catalog` | 目录根 |
| `LIBRARY_PDF_ROOT` | `/srv/sun-textbooks/pdf` | `<sha256>.pdf` 所在；`accel` 模式下容器内可以不挂载 |
| `PDF_SERVE_MODE` | `accel` | 见 §4 |
| `LIBRARY_ACCEL_PREFIX` | `/_protected_pdf/` | 须与 nginx 内部位置一致 |
| `LIBRARY_RELOAD_SECONDS` | `60` | 重新读取 `CURRENT` 的最短间隔；`0` 表示只在进程启动时载入 |
| `LIBRARY_PREVIEW_POLICY` | `catalog` | `catalog`：只认 `storage.vps`；`present`：载入时在 `LIBRARY_PDF_ROOT` 找得到 `<sha256>.pdf` 也算已存（本机开发用） |
| `LIBRARY_RATE_JSON_BURST`／`_PER_SECOND` | `240`／`8` | 每个来源地址的 JSON 端点令牌桶 |
| `LIBRARY_RATE_PDF_BURST`／`_PER_SECOND` | `1200`／`40` | 每个来源地址的 PDF 请求令牌桶（PDF.js 每页会发数个 64 KiB 的 Range 请求；一个班可能共用一个出口地址） |
| `LIBRARY_TRUSTED_PROXIES` | 空 | 除回环地址外，另外可信任其 `X-Real-IP` 的对端地址，逗号分隔 |
| `LIBRARY_UPSTREAM_HOSTS` | `jks-ai.bdfz.net,smartedu.cn,ykt.cbern.com.cn` | 官方平台来源的主机白名单（含子域） |
| `LIBRARY_COVER_HOSTS` | `ykt.cbern.com.cn,smartedu.cn,img.rdfzer.com,img.bdfz.net` | 封面主机白名单 |
| `LIBRARY_UPSTREAM_TEMPLATE` | 空 | 下载记录的 `upstream` 来源没写网址时才用，例如 `https://jks-ai.bdfz.net/pdf/{source_id}` |

### 限流的来源地址（发布前必须确认）

键＝TCP 对端地址；只有对端是回环地址，或列在 `LIBRARY_TRUSTED_PROXIES` 里时，才改用 `X-Real-IP`。IPv6 以 /64 归并。超限回 `429` 与 `Retry-After`。

现行容器以 `-p 8080:8080` 发布：nginx 经 `127.0.0.1:8080` 进来的连线，容器内看到的对端是 Docker 网桥闸道（通常 `172.17.0.1`），不是回环地址。不设定的话，所有访客会共用同一个桶。发布时二选一：

1. 设 `LIBRARY_TRUSTED_PROXIES=<网桥闸道地址>`（先在容器内确认实际地址）。直接打公网 8080 的连线，来源地址是真实地址，不会被当成代理。
2. 容器改用 host 网络或把 uvicorn 绑在回环地址，使 nginx 成为回环对端。

应用内限流是单进程内存状态，重启即清空；nginx 的 `limit_req` 是外层保护，两者都要有。

## 6. 发行版的载入、切换与回退

- 进程内只有一个已载入的发行版对象（不可变）。检索结果换算、预览、下载三者每次请求都取同一个对象，不会出现混合状态。
- 载入时机：进程启动；之后每次请求最多每 `LIBRARY_RELOAD_SECONDS` 秒读一次 `CURRENT`。id 没变就不做任何事；id 变了，就完整读入并校验新发行版，全部通过后才以一次赋值换上。
- 新发行版无效时，旧的继续服务，`/api/library/release` 的 `pending_error` 显示原因。

切换步骤：

1. 把新发行版完整写到 `releases/<新 id>/`（先写到暂存名再改名亦可）。不要改动已发布的目录。
2. 在该目录执行 `sha256sum -c SHA256SUMS`（或 `python scripts/catalog/validate_release.py releases/<新 id>`）。
3. 确认新发行版引用、且 `storage.vps` 非空的每个 `<sha256>.pdf` 都已在 `/srv/sun-textbooks/pdf/`。
4. 原子替换指标：`printf '%s\n' '<新 id>' > CURRENT.tmp && mv CURRENT.tmp CURRENT`。
5. 最多等 `LIBRARY_RELOAD_SECONDS` 秒，`curl -s https://sun.bdfz.net/api/library/release` 应显示新 `release_id` 且 `pending_error` 为 `null`。

回退：把 `CURRENT` 以同样方式改回上一个 id。旧发行版目录与旧 PDF 在确认不再回退前不要删除。PDF 网址含内容哈希，换版不会让浏览器缓存到旧档。

## 7. 前端行为摘要

- 网址状态（可分享、重新整理后仍在）：`?view=pdf&subject=&t=`、`?view=pdf&edition=<edition_id>&p=<pdf_page>&q=<检索词>`、`?view=downloads&stage=&subject=&publisher=&q=&page=`（`stage=all` 表示全部学段；不带 `stage` 时预选本页的学段）。`?view=gaokao|insights|graph` 也一并生效（由 `library.js` 点按既有导航钮实现，未改 `app.js` 的切换逻辑）。
- 旧链接：`#book=<JC 书 id>&page=<n>` 与 `?jks=<slug>&page=<n>` 经 `/api/library/legacy` 解析；确切则直达该页，否则打开第 1 页并显示说明；找不到则回书目并说明。
- 预览：先以 `HEAD` 确认状态与 `Accept-Ranges`，不支持 Range 就停止（不整本下载）；PDF.js 以 `disableAutoFetch`、`disableStream`、`rangeChunkSize 65536` 打开；一次渲染一页，完成后预热前后各一页；换页、关闭、切换分页时取消进行中的渲染与请求。
- 高亮：页面有文字层时在 PDF.js 文字层上标出检索词；没有则显示「此页无文字层，无法高亮。」
- 键盘：`←`／`→` 翻页，`/` 聚焦检索框，`Esc` 先收起侧栏、再回书目。
- 下载分页：同源来源以串流显示进度，可取消、可重试；超过 150 MB 或浏览器不支持串流时交给浏览器直接下载；跨源来源在新标签页打开。同时最多两册在页面内下载。
- 地区：API 与状态都带 `region`（现值 `CN`），界面不显示地区选择。

已知特性：没有线性化、页面物件分散在档案各处的扫描版 PDF，PDF.js 打开时要为每一页取一个区块（实测 40 页、15 MB 的样例：44 个请求、约 3.0 MB，之后每翻一页约一页的大小）。区块取 64 KiB 是为了压低这个成本。真实档案取得后应抽样量一次首屏的请求数与字节数。

## 8. 本机开发与测试

```sh
# 测试（74 项；需要 fastapi、httpx、pymupdf）
python -m unittest discover -s tests/library

# 合成目录＋小 PDF（另含一册约 15 MB 的无文字层样例）
python scripts/dev_library_server.py --synthetic

# 用 WS-A 产出的发行版与 PDF 目录
python scripts/dev_library_server.py --release-dir <…/releases/<id>> --pdf-dir <含 <sha256>.pdf 的目录>
```

开发服务器真实执行 `backend/library.py`，静态提供 `frontend/`，其余 `/api/*` 的 GET 只读转发到 `https://sun.bdfz.net`；POST 一律在本机回 `405`（因此不可能触发 AI）；转发的 `/api/search` 有次数上限（预设 6）。它不修改发行版目录。合成资料的书名都带「【样例】」，页对应是编造的，不得用于正式环境。

## 9. 排错

| 现象 | 检查 |
| --- | --- |
| 两个分页显示「目录尚未发布／不可用」 | `curl -s …/api/library/release` 看 `reason`：`no CURRENT pointer`、`release directory not found`、`sha256 mismatch: <档>`、`invalid JSON: <档>`、`page map length does not equal PDF page count: <版次>` 等 |
| 切换后仍是旧发行版 | 看 `pending_error`；`CURRENT` 是否单行、无多余字符；是否已过 `LIBRARY_RELOAD_SECONDS` |
| 书目是空的，下载分页正常 | 版次的 `storage.vps` 是否已写入；`counts.previewable` 是否为 0 |
| 打开某册显示「文件缺失」 | `/srv/sun-textbooks/pdf/<sha256>.pdf` 是否存在、nginx 用户是否可读；`curl -sI` PDF 网址 |
| 显示「服务器不支持分段读取」 | `HEAD` 回应缺 `Accept-Ranges: bytes`：检查 `/_protected_pdf/` 位置是否生效、中间是否有代理移除了它 |
| PDF 网址回 `200` 但正文是空的 | 回应里看得到 `X-Accel-Redirect` 表示请求没有经过 nginx（例如直连 8080），或 nginx 没有该内部位置 |
| 访客普遍收到 `429` | 见 §5「限流的来源地址」；`LIBRARY_TRUSTED_PROXIES` 多半没设 |
| 检索结果没有「在 PDF 中打开」 | 该页在页对应表里没有 `legacy_image_index`、`corpus.relation` 不在可换算之列、或该册未存于本站；`/api/library/resolve?book_key=…&section=…` 的 `reason` 会说明 |
| 中文 PDF 出现方块字 | `/vendor/pdfjs/cmaps/` 是否可取得（`VENDOR.json` 列出全部文件与 sha256） |
