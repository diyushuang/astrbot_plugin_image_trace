# 更新日志

## 1.3.7（2026-09-13，图片输入格式默认值修正）

- **`embed_image_input` 默认值改为 `nemotron-vl`**：旧默认 `qwen-vl` 把图片
  序列化成 content 数组发给 embedding 服务，而 NVIDIA
  `llama-nemotron-embed-vl` 系（`integrate.api.nvidia.com`）只接受裸
  dataURL 字符串，服务端逐项 `.strip()` 遇字典即报
  `HTTP 500: 'dict' object has no attribute 'strip'`——从 GitHub
  全新安装插件的实例开箱即命中此错。现在默认值、选项顺序（nemotron-vl
  提前）、schema hint、代码内非法值回退（`vector_search.py`）与 README
  配置表全部对齐 NVIDIA 实际行为；hint 补充该 500 签名便于自诊。
  使用 Qwen3-VL-Embedding 系模型的用户需手动改回 `qwen-vl`。
- **注意**：已有安装的实例配置不受 schema 默认值影响（AstrBot 保留现有
  配置值），升级后若仍报该 500，请在 WebUI 插件配置里把「图片输入序列化
  格式」改为 `nemotron-vl`（进阶项，需先开启 advanced 收纳开关）。
- README 版本徽章补升（1.3.6 时漏改，仍标 1.3.5）。

## 1.3.6（2026-09-13，响应体截断修复）

- **完整读取响应体**：`read_limited_bytes` 原先只做单次
  `content.read(limit + 1)`，而 aiohttp 的 `read(n)` 只承诺「最多 n
  字节」——缓冲区里有多少就返回多少、并不等待读满。embedding 响应
  39841 字节分块到达时只读到当前缓冲的 3893 字节就被送去解析 JSON，
  报「Expecting ',' delimiter」，且重试再次命中同一竞态。现在循环读到
  EOF 才返回，1 MiB 上限的超限即时中止与 Content-Length 预检保持不变；
  响应中途断流仍由 aiohttp 帧校验抛 `ClientPayloadError` 走既有错误
  分支。Qdrant、图床 API、S3 等所有走该函数的请求一并受益。
- **测试**：新增「响应分块到达仍返回完整响应体」「embedding 响应分
  两块到达仍解析成功」回归测试；测试桩的响应体读取改为读后消耗语义，
  与 aiohttp `StreamReader` 行为一致。

## 1.3.5（2026-09-13，坏图片与无效 JSON 修复）

- **坏下载拦截**：QQ 图床 URL 带 rkey 签名会过期、NTQQ
  （multimedia.nt.qq.com.cn）/ gchat.qpic.cn 有防盗链，失败时常返回几十~
  几百字节的错误体。现在内置解析与兜底下载的产物都会先经 Pillow 完整解码
  校验（`features.image_file_ok` / `features.image_mime`），截断、非图片或
  不受支持内容不再送进哈希/向量引擎；解码前按 50M 像素防解压炸弹上限
  拒绝超大图；日志记录字节数与头部片段便于定位。
- **真实 MIME 识别**：`VectorEngine.embed_file()` 改用 Pillow 按内容识别
  MIME 构造 data URL，不再依赖临时文件 `.jpg` 扩展名；扩展名错误的 PNG
  等图片可正常向量化。
- **QQ 图床防盗链**：兜底下载对 `*.nt.qq.com.cn` / `*.qpic.cn` 附带
  `Referer: https://gchat.qpic.cn/`（防盗链要求）。域名改为精确匹配
  `multimedia.nt.qq.com.cn`、`gchat.qpic.cn` 及其真实子域，避免
  `eviltqpic.cn` 这类伪造后缀误触发；每一跳按当前 URL 重新判断，重定向
  离开 QQ 图床域后不再携带。
- **无效 JSON 重试与诊断**：embedding 返回 HTTP 200 但 JSON 无效/截断时，
  记录响应长度、`Content-Length`、`Content-Type`、解析位置及错误位置前后
  片段，退避 2 秒重试一次；两次仍失败时提示「向量服务返回无效 JSON
  （已重试一次）」。该重试与既有 5xx 重试共享最多两次请求的总限制，
  不叠加消耗配额。Schema 缺失、空向量、非数值、`NaN` / `Infinity`
  等确定性错误不重试，继续给出明确错误。
- **错误提示**：embedding 返回 400 且响应体含 `Cannot identify image`
  时，提示改为「图片内容无效（下载不完整、链接已过期或格式不受支持）」，
  不再误导用户去改模型配置。

## 1.3.4（2026-09-13，向量检索韧性与错误诊断）

- **瞬时故障韧性**：embedding 返回 500/502/503/504 时自动退避 2 秒单次重试
  （仅一次，格式不匹配类确定性错误不重试），缓解 NVIDIA 等网关对中等体积
  请求的偶发 500（`Missing request extension`）/502 抖动。
- **错误诊断**：embedding 服务返回 500 且响应体含
  `'dict' object has no attribute 'strip'` 时，报错直接提示
  `embed_image_input` 与模型不匹配（服务端把 `input` 当纯文本逐项解析，
  实际收到了 content 数组字典），并指引改为模型/入库侧实际使用的格式。

## 1.3.3（2026-09-13，规范与 Qdrant 兼容修复）

- **Qdrant 1.19 兼容**：向量检索优先使用官方 Query API
  `POST /collections/{collection}/points/query`（请求体 `query` / `limit` /
  `with_payload`）；当服务端返回 404 时自动回退旧版 `points/search`，继续支持
  Qdrant 1.0~1.9，同时兼容 1.10~1.19。集合名统一 URL 编码，embedding 向量
  增加非空与有限数值校验。
- **AstrBot 插件规范**：移除已废弃的 `@register` 装饰器，完全以
  `metadata.yaml` 为元数据来源；版本号改为无 `v` 前缀的 SemVer `1.3.3`；
  数据目录改用官方 `get_astrbot_data_path()` 推导，不再依赖进程当前目录。
- **网络与资源**：新增共享 `GuardedHttpClient`，向量检索、图片下载、图床、
  CloudFlare-ImgBed 与 R2/OCI S3 请求复用同一个启用 IP pinning 的
  `aiohttp` 会话；卸载时逐项清理资源，单项失败不阻断后续释放。
- **数据一致性**：登记原图在数据库失败或并发重复时补偿清理本次上传对象；
  空目录重扫不再提前返回，`force` 与失效索引清理可正常执行；跨进程唯一索引
  冲突后重新查询重复行；Windows 路径统一绝对路径与大小写归一化。
- **图像处理**：不再修改 Pillow 进程级 `MAX_IMAGE_PIXELS`，改为显式像素上限；
  DCT 矩阵缓存改用线程安全的 `functools.lru_cache`；动图先取首帧再处理
  EXIF 方向。
- **配置与文档**：`hash_size` 下拉补齐 4~32 的全部 4 倍数；哈希阈值与
  `top_n` 做安全钳制；文档补充 Qdrant 兼容矩阵并更新发版流程。

## v1.3.2（2026-09-13，配置面板重构）

- **配置项重新排序**：面板按使用顺序重排——检索引擎 → 本地图库目录 →
  向量检索设置 → 哈希相似度阈值 → 储存桶设置 → 图床设置 → 行为开关 →
  进阶项，常用项在前、调优项沉底。
- **按需显隐（WebUI condition）**：储存桶组内 `r2_*` 字段仅在
  `mode = cloudflare_r2` 时显示、`oci_*` 字段仅在 `mode = oracle_oci` 时
  显示；图床组内 generic_http 字段与 `cfi_*` 字段同样随所选模式展开。
  未选模式时两组各只显示一行「模式」下拉，默认面板从约 48 行减到约 19 行。
  被隐藏字段的已填值保留，切换模式不丢失；旧版 WebUI 不支持 condition
  时自动降级为全部显示（不影响使用）。
- **进阶设置收纳开关**：新增顶层 `advanced_settings` 与
  `vector_search.advanced` 两个开关（默认关），分别收纳哈希精度 / 候选数 /
  单次图片数 / 大小上限与图片输入格式 / `input_type` / `top_k` / 请求超时
  等调优项；开启后才在面板展开，均为纯面板收纳开关，后端不读写。
  `embed_input_type` 仅在同时开启进阶且图片输入格式为 `nemotron-vl` 时显示。
- **必填 / 选填全标注**：全部配置项的标签加注【必填】/【选填】及模式限定
  （【R2 必填】【OCI 必填·二选一】【generic_http 必填】【imgbed 必填】
  【按需必填】等），面板上一眼可辨哪些必须填、哪些保持默认即可。
- 无功能逻辑改动，不修改任何现有配置键；新增的 2 个开关键由 AstrBot 自动
  补默认值，老配置无需迁移。

## v1.3.1（2026-09-12，安全加固）

- **S3 错误响应解析加固**：`s3_store` 解析对象存储错误 XML 前先拒绝含
  `<!DOCTYPE` / `<!ENTITY` 的响应（S3 错误 XML 从不包含 DTD），杜绝标准库
  ElementTree 的内部实体展开（billion laughs）类内存 DoS；命中时回退展示
  原文前 200 字符，正常错误诊断不受影响。响应体本就经 `read_limited_bytes`
  限长（1 MiB），本条为纵深防御。

## v1.3.0（2026-09-12，储存桶与图床配置拆分）

- **配置面板重构：储存桶与图床分设两组**。Cloudflare R2 / 甲骨文 OCI 从
  `image_bed` 模式中独立为单独的「储存桶设置」（`storage_bucket`）配置组
  （`mode`：`none` / `cloudflare_r2` / `oracle_oci`），`image_bed` 组回归纯图床
  设置（`local` / `generic_http` / `cloudflare_imgbed`），两类字段不再在面板里
  混排。
- **储存桶优先**：`storage_bucket` 配置完整（必填项齐全且公开直链条件满足）
  时优先于图床设置；`mode=none` 或配置不完整时自动改用图床设置（日志提示
  缺项），`/溯源状态` 的「存储方式」一行标明当前生效来源。
- **老配置自动迁移（幂等）**：v1.2.x 填在 `image_bed.mode = cloudflare_r2 /
  oracle_oci` 及 `r2_*` / `oci_*` 字段的配置，在插件启动时自动搬到
  `storage_bucket` 组，图床模式归位 `local`，无需手动操作。
- **`embed_api_key` 放宽为可留空**：向量引擎启用判定不再要求 API Key
  （网关未开启鉴权时无需再填占位值），留空则 embedding 请求不带鉴权头；
  `qdrant_url` / `embed_base_url` / `embed_model` 三项仍为向量引擎必填。
- 文档：`vector_search` 组与 README 补充"为什么要填向量 AI"说明（Qdrant
  只存向量，查询图需插件侧用同一模型向量化）；FAQ 新增向量 AI 必填原因、
  储存桶与图床并存优先级、v1.2.x 升级迁移三条。

## v1.2.1（2026-09-12，结构与健壮性修复）

- **修复兜底下载失效**：`_download` 的 prepare 回调按 url_guard 的三参契约
  （url, method, cross_origin）补齐签名——此前该路径每次必然 TypeError 被吞，
  `convert_to_file_path` 失败时的 URL 下载兜底从未真正工作过。
- **修复依赖下限**：`aiohttp>=3.8.0` → `>=3.10`（url_guard 使用的
  `aiohttp.abc.ResolveResult` 为 3.10 新增，低版本插件无法加载）。
- **修复 hash_size 隐性故障**：pHash 十六进制长度统一由
  `features.phash_hex_len()` 计算（此前 main/library 各自推导且仅在
  hash_size 为 4 的倍数时正确，其它取值会导致全库索引静默清空）；
  配置校验收紧为"4~32 之间 4 的倍数"。
- **`/溯源删除` 清理更完整**：同步删除插件侧写入的 Qdrant 向量点（按内容
  指纹派生，幂等）与本地副本文件，不再残留死链可被向量引擎命中。
- 健壮性：`rescan`/`溯源删除` 增加顶层异常兜底；`/溯源状态` 不再把含
  内网地址/Key 的原始错误发进群聊；`_bool_cfg` 对 null 配置回退默认值
  （此前 `register_admin_only` 会被静默关闭）；重扫对"目录临时掉线"只
  跳过该目录、不再清空其索引；R2/OCI 公开直链 base 校验 http(s) 前缀；
  CloudFlare-ImgBed 删除分支统一按 urlparse 匹配（直链带 query 也能删）；
  未知图床模式显式告警；向量引擎 IP 字面量地址也纳入钉扎。
- 数据与性能：图库 schema 升级 v2（phash 去重 + 唯一索引，登记查重与
  插入合并为原子操作）；SQLite 重操作与本地副本复制移入线程池，不再阻塞
  事件循环；内存缓存整体替换 + 快照读，兑现跨线程安全契约；S3 客户端
  会话跨请求复用；S3 错误响应解析 XML Code/Message 便于排障。
- 文档：README 补充 `embed_input_type` 配置行与 `nemotron-vl` 输入格式、
  hash_size 取值约束、dHash/aHash 预留说明，并新增「项目结构与开发」章节
  （模块职责、两条核心数据流、出网/配置解析等开发约束）；
  修正 _conf_schema 中 `nemotron-vl` 输入格式的过时描述（与实现对齐）。
- 文档（GitHub 通用要素）：README 增加版本/协议/环境徽章、目录与双引擎流程图，
  安装章节明确「从仓库安装」与「手动安装」两种途径；新增 LICENSE（AGPL-3.0）
  与「参与贡献」「许可证」「致谢」章节；发布包白名单 13 → 14 文件（加入 LICENSE）；
  README 顶部与工作区文档加入 AI 生成代码声明（ZCode / GLM 辅助生成、人工审查）。

## v1.2.0（2026-09-09）

- **新增向量检索引擎（双引擎）**：`vector_search` 配置节对接 Qdrant + 多模态向量 AI
  （OpenAI 兼容 `/v1/embeddings`，图片 dataURL 直传），`/溯源` 用 cosine 相似度检索
  图床入库的图片；`search_engine=auto` 时向量优先，未命中/引擎不可用自动回退 pHash。
- 向量库与图床侧自动同步：图床每张上传经服务器钩子实时计算向量写入 Qdrant（与
  图床入库侧同协议），本插件只做查询侧；`/登记原图` 也支持插件侧幂等写入。
- `/溯源状态` 显示当前引擎、向量库集合与点数、Embed 模型；配置缺项时给出明确提示。
- 注意：切换向量模型后需对库内全部图片**全量重嵌入**（服务器执行强制回填）。

## v1.1.0（2026-09-09，随 v1.2.0 首次发布）

- 新增 **CloudFlare-ImgBed** 官方 REST API 对接（`POST /upload`、token/authCode 鉴权、
  渠道/目录参数、`/溯源删除` 远端删除同步），泛用图床接口再添一档；
- 新增 **Cloudflare R2** 与 **甲骨文 OCI Object Storage** 对象存储模式（纯标准库实现
  AWS Signature V4，按官方文档对接，公开直链缺失时自动回退本地副本）；
- `url_guard` SSRF 防护加固：禁用自动重定向、逐跳校验（含 CGNAT 段），修复远端删除
  never-awaited 协程 bug；
- 登记/重扫批量入库性能优化（消除 O(N²)）。

## v1.0.0（2026-09-06）

- 初始版本：群内发图 / `/溯源`，pHash/dHash/aHash 感知哈希本地比对，达标回传原图；
- 泛用图床接口（local / generic_http 适配 Lsky Pro、EasyImages、Chevereto 等）；
- 本地目录建库 `/溯源重扫`、`/登记原图`、`/溯源状态`、`/溯源删除`、可选的视觉大模型
  AI 复核（`ai_verify`）。
