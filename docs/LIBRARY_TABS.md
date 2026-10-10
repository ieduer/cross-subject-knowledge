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
| `storage.vps` | 非 `null`（修订 2 为 `{path}`）才算「本站已存」＝可预览、可由本站下载。`path` 不读，文件名一律由 `pdf.sha256` 推得 |
| `storage.drive` | 仅当 `shared === true` 且网址是 `https://drive.google.com`（或 `drive.usercontent.google.com`、`docs.google.com`）才提供 |
| `legacy.sun_book_key` | 把 SUN 检索结果与旧 JC 链接对到版次；只按这个识别码，从不按书名 |
| `legacy.jc_short_key` | 12 位十六进制；与 `md5(sun_book_key)[:12]` 一并建索引 |
| `legacy.jks_slug`、`legacy.jks_pages` | 旧 JKS 链接；见 §3「页码」 |
| `corpus.relation` | 只有 `same-bytes`／`aligned`／`partial` 才允许用页对应表做旧页码换算 |
| `work_id` | 同一 `work_id` 的其他版次在阅读器里分开列出，不合并 |
| `platform_current`（修订 2；旧版在 `source.platform_current`） | `false`＝本站这份文件不是官方平台目前提供的那一份。界面显示「非平台现行版」；未提供则为 `null`，不显示 |
| `newer_edition_id`、`supersedes_edition_id`（修订 2） | 指向本发行版内的另一个版次；指向不存在的版次时当作 `null`。界面显示「官方平台已有更新版本」并连到那个版次自己的下载条目 |
| `corpus.text_identity`（修订 2） | `same-file`／`page-aligned-other-printing`／`null`。未提供时：`same-bytes`→`same-file`，`aligned`／`partial`→`page-aligned-other-printing`（从严） |
| `source.id_basis`（旧名 `source_id_basis`）、`metadata_basis`（修订 2） | 透传到版次详情 |
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

两份文件、一个书名的处理（审查 M3）：本站保存的旧文件与官方平台的现行文件永远是两笔下载条目。本站那一笔只有「本站」来源，标「非平台现行版」与取得年月；平台那一笔只有「官方平台」来源，标「官方平台现行文件 · 未存于本站」；两边互相指向对方，并写明「不是同一份文件」。预览页的下载钮只送出本站保存的这一版次。界面与程序里不出现「最新」字样。

检索文字与 PDF 的关系（审查 M1）：`text_identity` 为 `page-aligned-other-printing` 时，「在 PDF 中打开」改为「在 PDF 中打开对应页」并附注「检索文字来自较早印次，可能与此 PDF 的文字略有出入，以 PDF 为准」；册内检索结果一律标为「检索库片段」，不当作 PDF 页面的引文；页面上的高亮只来自 PDF 自己的文字层，找不到时明说「本页 PDF 文字层中没有找到」。

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
| `GET /api/library/downloads` | `stage`、`subject`、`publisher`、`q`、`region`、`offset`、`limit`（1–60，预设 30）、`row`（单一条目的 `row_id`） | `total`、`downloads[]`（`sources` 依 本站→云端硬盘→官方平台 排序） |
| `GET /api/library/downloads/facets` | `stage`、`subject`、`region` | `stages`、`subjects`、`publishers`（各带计数） |
| `GET`／`HEAD /files/pdf/{edition_id}/{sha12}/{filename}` | `download=1` | 见下 |

PDF 端点：`edition_id` 必须在已载入的发行版内、本站已存，且 `sha12` 等于其 `pdf.sha256` 前 12 位，否则 `404`。`filename` 只是让网址好看，服务端不读；`Content-Disposition` 的文件名由书名与版次标签在服务端生成。磁盘路径只由目录里的 sha256 组成，请求中没有任何内容进入路径。

| `PDF_SERVE_MODE` | 行为 |
| --- | --- |
| `accel`（预设，正式环境） | 回 `200`、空正文、`X-Accel-Redirect: /_protected_pdf/<sha256>.pdf`，并带 `Content-Type: application/pdf`、`Content-Disposition`、`Cache-Control: public, max-age=31536000, immutable`。Range、`Content-Length`、校验器由 nginx 的内部位置负责。`LIBRARY_PDF_ROOT` 若在容器内可见，会先确认文件存在（缺则 `404 file missing`）；不可见则以目录为准 |
| `direct`（本机开发、测试） | 应用自己送档：`Accept-Ranges: bytes`、单一范围 `206`＋`Content-Range`、不合法范围 `416`、多范围退回整档 `200`、`ETag`＝sha256、`If-Range` 不符退回 `200`、`If-None-Match`（含列表与 `*`）命中 `304`、支持 `HEAD`。文件在开始送出时才打开，客户端断线即关闭 |

长效缓存头（`immutable`）只出现在 `200`／`206`；`304` 不带，`404`／`416` 为 `no-store`。

nginx 需要的内部位置与建议的 `limit_req`、`limit_conn` 见 `docs/ops/nginx-library.conf.example`。要点：`/_protected_pdf/` 必须是 `internal`，`alias /srv/sun-textbooks/pdf/`，该目录要让 nginx 的执行用户读得到（nginx 读不到 `/root`）。

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
| `LIBRARY_TRUSTED_PROXIES` | 空 | 除回环地址外，另外可信任其 `X-Real-IP` 的对端：逗号分隔的地址或 CIDR（写法同 `AI_TRUSTED_PROXIES`）。无法解析的项目、比 /8（IPv4）或 /16（IPv6）更宽的网段一律忽略 |
| `LIBRARY_UPSTREAM_HOSTS` | `jks-ai.bdfz.net,smartedu.cn,ykt.cbern.com.cn` | 官方平台来源的主机白名单（含子域） |
| `LIBRARY_COVER_HOSTS` | `jks-ai.bdfz.net,ykt.cbern.com.cn,smartedu.cn,img.rdfzer.com,img.bdfz.net` | 封面主机白名单 |
| `LIBRARY_UPSTREAM_TEMPLATE` | 空 | 下载记录的 `upstream` 来源没写网址时才用，例如 `https://jks-ai.bdfz.net/pdf/{source_id}` |

### 运行期配置档（`scripts/deploy_vps.sh`）

`deploy_vps.sh` 的两个 `docker run`（启动与回退）都会：

- 以 `-p 127.0.0.1:8080:8080` 发布（`PUBLISH_ADDR` 只接受 `127.0.0.1`；启动后再以 `docker port` 核对，发现 `0.0.0.0`／`[::]` 即失败）；
- 若 `SUN_ENV_FILE`（预设 `/etc/sun/sun.env`）存在，就以 `--env-file` 传入。该档必须在仓库与 `RUNTIME_ROOT` 之外、属 root、权限 `600` 或 `400`，否则脚本在动到现行容器之前就停止。脚本不读取、不显示其内容；
- 该档不存在时照常启动，全部走程序预设：AI 关闭，分页用上表预设值；
- `/srv/sun-textbooks`（`SUN_TEXTBOOK_ROOT`）存在时以只读挂入容器，供 `accel` 模式先确认文件存在。

该档是 Docker 的 env-file 格式：一行一个 `NAME=value`，不加引号、不做变量展开，`#` 开头为注释。脚本用 `-e` 固定的变量（`PROJECT_ROOT`、`DATA_ROOT`、`STATE_ROOT`、`RUNTIME_DB_SYNC_MODE`、`EMBEDDER`、`RERANKER`、`RERANKER_ENABLED`、`RERANKER_PRELOAD`、`HF_HOME`、`HF_HUB_CACHE`、`SENTENCE_TRANSFORMERS_HOME`、`TRANSFORMERS_CACHE`）优先于档内同名项。

档内可出现的变量名（只列名称；数值由站长决定，机密来自既有的密钥保管处，不写进仓库）：

| 用途 | 变量名 |
| --- | --- |
| 分页 | `PDF_SERVE_MODE`、`LIBRARY_ACCEL_PREFIX`、`LIBRARY_PDF_ROOT`、`LIBRARY_CATALOG_ROOT`、`LIBRARY_RELOAD_SECONDS`、`LIBRARY_PREVIEW_POLICY`、`LIBRARY_TRUSTED_PROXIES`、`LIBRARY_RATE_JSON_BURST`、`LIBRARY_RATE_JSON_PER_SECOND`、`LIBRARY_RATE_PDF_BURST`、`LIBRARY_RATE_PDF_PER_SECOND`、`LIBRARY_UPSTREAM_HOSTS`、`LIBRARY_COVER_HOSTS`、`LIBRARY_UPSTREAM_TEMPLATE` |
| AI（定义、预设与启用顺序见 `docs/AI_GUARD.md`；不设即关闭） | `AI_ENABLED`、`AI_SESSION_KEY`、`AI_DAILY_REQUEST_BUDGET`、`AI_SUBJECT_DAILY_CAP`、`AI_TRUSTED_PROXIES`、`AI_ALLOWED_ORIGINS`、`AI_INTERNAL_TOKEN`、`AI_SERVICE_URL`、`AI_SERVICE_PROJECT`、`AI_SERVICE_ORIGIN`、`AI_SERVICE_MODEL`、`AI_SERVICE_TIMEOUT_SEC`、`AI_SERVICE_RETRIES`，以及该文件列出的其余 `AI_*` 上限 |
| 既有 | `CORS_ALLOW_ORIGINS`、`CORS_ALLOW_ORIGIN_REGEX` |

首次发布建议：档案只放 `LIBRARY_TRUSTED_PROXIES`（见下），或干脆不建档。

### 限流的来源地址（发布前必须确认）

键＝TCP 对端地址；只有对端是回环地址，或落在 `LIBRARY_TRUSTED_PROXIES` 的地址／网段内时，才改用 `X-Real-IP`。IPv6 以 /64 归并。超限回 `429` 与 `Retry-After`。桶表上限 20,000 个键，超过时淘汰最久未用的键（不会整表清空）。

nginx 经 `127.0.0.1:8080` 进来的连线，容器内看到的对端是 Docker 网桥闸道（通常 `172.17.0.1`），不是回环地址。因此：

- **8080 仍对外发布（`0.0.0.0`／`[::]`）时，不得信任任何代理。** 公网直连 8080 的请求——尤其 IPv6，经 docker-proxy 进来时来源同样会显示为网桥闸道——可以自带 `X-Real-IP`，一旦信任闸道，来源键就由对方任意指定。此时只能接受「所有访客共用一个桶」。
- 支持的顺序只有一种：**先**把发布改为回环（本版 `deploy_vps.sh` 已固定如此，并在启动后核对），确认公网连不上 8080；**然后**才在配置档里设 `LIBRARY_TRUSTED_PROXIES=<网桥闸道>`（在容器内确认实际地址，例如看预设路由；可写单一地址或 `/32`）。`AI_TRUSTED_PROXIES` 同理。
- nginx 的每个代理位置都要写 `proxy_set_header X-Real-IP $remote_addr;`（范例已补齐）。

应用内限流是单进程内存状态，重启即清空，而且只数请求、不数字节或连线；nginx 的 `limit_req` 与 `/files/pdf/` 上的 `limit_conn`（范例值 48）是外层保护，三者都要有。

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
- 下载分页：同源来源预设直接交给浏览器下载 `?download=1` 网址（不经 JavaScript 缓冲）；只有大小已知且不超过 25 MB 的文件才在页面内显示进度（可取消、可重试，同时只有一册）。跨源来源在新标签页打开。
- 画布：每页画布的总像素上限为手机与低内存设备 5,000,000、其余 8,000,000（与缩放和屏幕像素比无关，超出时降低清晰度而不是放大内存）；换页时旧画布立即归零释放。
- 封面：先显示学科占位；卡片接近可视范围才开始取图，同时最多 3 张，20 秒未到就放弃并保留占位；浏览器要求省流量时不取。官方封面每张约 1 MB、首字节约 6–9 秒（实测），所以封面只是点缀，不影响下载。
- 地区：API 与状态都带 `region`（现值 `CN`），界面不显示地区选择。

已知特性：没有线性化、页面物件分散在档案各处的扫描版 PDF，PDF.js 打开时要为每一页取一个区块（实测 40 页、15 MB 的样例：44 个请求、约 3.0 MB，之后每翻一页约一页的大小）。区块取 64 KiB 是为了压低这个成本。真实档案取得后应抽样量一次首屏的请求数与字节数。

## 8. 本机开发与测试

```sh
# 测试（需要 fastapi、httpx、pymupdf；含发布清单与 deploy_vps.sh 的静态检查，不连任何主机）
python -m unittest discover -s tests/library

# 合成目录＋小 PDF（另含一册约 15 MB 的无文字层样例）
python scripts/dev_library_server.py --synthetic

# 用 WS-A 产出的发行版与 PDF 目录
python scripts/dev_library_server.py --release-dir <…/releases/<id>> --pdf-dir <含 <sha256>.pdf 的目录>
```

开发服务器真实执行 `backend/library.py`，静态提供 `frontend/`，其余 `/api/*` 的 GET 只读转发到 `https://sun.bdfz.net`；POST 一律在本机回 `405`（因此不可能触发 AI）；转发的 `/api/search` 有次数上限（预设 6）。它不修改发行版目录。合成资料的书名都带「【样例】」，页对应是编造的，不得用于正式环境。

## 9. 发布清单与清单档

- `scripts/stage_clean_release.py` 的 `RELEASE_FILES`＋`RELEASE_TREES`（整棵 `frontend/vendor/pdfjs`，逐档对照 `VENDOR.json`）与 `scripts/build_release_manifest.py` 的 `SOURCE_ASSETS`＋`SOURCE_TREES` 是发布内容的两份清单；`tests/library/test_release_bundle.py` 把清单原样铺到临时目录后在子进程 `import main`，并检查四个页面、样式表、脚本（含 `library.js` 拼出的 PDF.js 路径）引用的每个本地文件都在里面，也检查 `backend/`、`frontend/` 下每个受版本控制的文件都在清单内。新增运行期文件而没加进清单，测试会红。
- `release_manifest.json` 必须在最后一次改动代码之后重新生成。持有运行期数据（`data/index`）的机器照旧执行 `python scripts/build_release_manifest.py`；只改了代码、手边没有运行期数据时用 `python scripts/build_release_manifest.py --reuse-runtime-from release_manifest.json`：重算全部源码条目，运行期条目原样沿用并在档内注明（`runtime_assets_carried_from`），`deploy_vps.sh` 仍会在 VPS 上逐项核对。运行期数据有变动时不得用这个参数。
- `deploy_vps.sh` 在停掉现行容器之前，会在候选映像内确认前端文件齐全、PDF.js 目录与 `VENDOR.json` 逐档相符、`import ai_guard, library` 成功。
- 页脚版本：`frontend/assets/version.json` 为 `2026.10.10-r38`，`app.js` 的后备值 `DEFAULT_FRONTEND_VERSION` 同步；资源缓存键 `?v=20261010a`。之后再改 `library.js`／`library.css` 须同时更换四个页面里的缓存键。

## 10. 排错

| 现象 | 检查 |
| --- | --- |
| 两个分页显示「目录尚未发布／不可用」 | `curl -s …/api/library/release` 看 `reason`：`no CURRENT pointer`、`release directory not found`、`sha256 mismatch: <档>`、`invalid JSON: <档>`、`page map length does not equal PDF page count: <版次>` 等 |
| 切换后仍是旧发行版 | 看 `pending_error`；`CURRENT` 是否单行、无多余字符；是否已过 `LIBRARY_RELOAD_SECONDS` |
| 书目是空的，下载分页正常 | 版次的 `storage.vps` 是否已写入；`counts.previewable` 是否为 0 |
| 打开某册显示「文件缺失」 | `/srv/sun-textbooks/pdf/<sha256>.pdf` 是否存在、nginx 用户是否可读；`curl -sI` PDF 网址 |
| 显示「服务器不支持分段读取」 | `HEAD` 回应缺 `Accept-Ranges: bytes`：检查 `/_protected_pdf/` 位置是否生效、中间是否有代理移除了它 |
| PDF 网址回 `200` 但正文是空的 | 回应里看得到 `X-Accel-Redirect` 表示请求没有经过 nginx（例如直连 8080），或 nginx 没有该内部位置 |
| 访客普遍收到 `429` | 见 §5「限流的来源地址」；`LIBRARY_TRUSTED_PROXIES` 多半没设（前提是 8080 已只绑回环） |
| 部署在「candidate image failed the pre-cutover check」停住 | 候选映像缺文件或 `import ai_guard, library` 失败；现行容器未被停止。先跑 `python -m unittest discover -s tests/library -p 'test_release_bundle.py'` |
| 检索结果没有「在 PDF 中打开」 | 该页在页对应表里没有 `legacy_image_index`、`corpus.relation` 不在可换算之列、或该册未存于本站；`/api/library/resolve?book_key=…&section=…` 的 `reason` 会说明 |
| 中文 PDF 出现方块字 | `/vendor/pdfjs/cmaps/` 是否可取得（`VENDOR.json` 列出全部文件与 sha256） |
