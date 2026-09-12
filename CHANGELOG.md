# 更新日志

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