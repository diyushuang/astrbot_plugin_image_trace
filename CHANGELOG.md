# 更新日志

## 1.5.4（2026-09-15，修复 NapCat 超时被误判为发送失败导致同图重发）

- **修掉「一张图还是会发两遍」的漏网情形**：上一版把 `ActionFailed` 归入「协议端
  明确回报失败」，但 NapCat 会把**超时**也包装成 `ActionFailed`——实测形态是
  `retcode=1200`、消息体里写着 `Timeout: NTEvent ... sendMsg`。原判断顺序是
  「异常类型 → 类名 → retcode → 文案」，类名检查排在任何文案检查之前，于是这种
  语义上的「结果未知」被先一步判成「明确失败」，照样触发本地压缩回退，用户依旧
  收到两张相同的图。现改为「异常类型 → 消息内容里的超时线索 → 类名 → retcode →
  未连接线索」，**让语义压过类型名**：`ActionFailed` 里只要出现 `timeout` /
  `timed out` / `timedout` / `time out` / `超时` 任一线索，一律判 `UNKNOWN` 就此打住。
  - 新增 `_TIMEOUT_HINTS` 与 `_error_text()`：后者从 `str(exc)` 与 `exc.message`
    两处拼出小写文案供线索匹配（个别版本 `__str__` 只给简短摘要，会漏掉真正的失败
    原因）。`_error_text()` 对畸形异常完全健壮——它是在 `except` 块里被调用的
    「异常处理的异常处理」，`str(exc)` / `getattr` / `str(message)` 任一处抛异常
    （自定义 `__str__`、取值即抛的 property）都就地跳过、退化为空串，绝不让自身
    异常向上传播、掩盖真实错误。
  - `retcode` 比较按字符串归一化（`str(retcode).strip() != "0"`），让 `0` 与 `"0"`
    一视同仁。OneBot 里 `retcode=0` 表示成功，异常却带着成功码说明状态不明，
    宁可判 `UNKNOWN` 也不再重发。
  - 代码注释写明：`retcode=1200` 当前的判定**依赖**消息体里的 `Timeout` 线索；
    若将来出现「空 message + `retcode=1200`」这种没有任何超时线索的形态，会退回按
    类名判 `FAILED`（偏保守、允许一次重发），届时再评估是否给 1200 单独特判。
  - 未连接类错误（`not connected` / `connection closed` 等）仍判 `FAILED`，
    保留安全重发能力——请求根本没到协议端，重发不会产生重复。

## 1.5.3（2026-09-15，HEIC 支持、压缩标识按证据判定、图片不再重复发送、/原图 直查图床）

- **HEIC/HEIF 支持**：`requirements.txt` 新增 `pillow-heif`，`features.py` 顶部防御式
  `register_heif_opener()`，`image_mime` 白名单补 `HEIF → image/heic`，`IMAGE_EXTS`
  补 `.heic` / `.heif`（`/溯源重扫` 得以收录 HEIC 原图）。此前 iPhone 图片会走进
  「内置解析产物不是有效图片（…ftypheic/mif1）→ 改走兜底下载 → 兜底下载内容不是
  有效图片」这条必然失败的链路：产物其实是合法 HEIC，只是白名单里没有它、解码器
  也没装。
  - **坏下载与缺解码器分流**：`_resolve_local_file` 现在区分两种情况——认不出任何
    图片容器（图床错误体 / rkey 过期 / 防盗链）才走自带下载兜底；已经认得出容器却
    解不开（缺解码器、文件截断）说明链接指向同一份内容，直接带具体原因返回并提示
    「未安装 pillow-heif」，省掉一次注定失败的请求与一对误导性日志。
  - **QQ 图床 Referer 分域**：`multimedia.nt.qq.com.cn` 改用自身根域 Referer，
    `*.qpic.cn` 继续用 `gchat.qpic.cn`——跨族套用 Referer 本身就会被判成跨站盗用。
- **压缩标识改为按证据判定**：新增 `CompressionEvidence`（已压缩 / 原图 / 说不准）
  与 `image_delivery.verify_scaled` 开关（默认开启）。修复「文字显示已压缩、实际
  收到的是原图」：此前压缩标识由「URL 是否被改写」推导，而图床在图片不超过
  `max_side`、格式不可处理或图像处理不可用时会按 `fallback=original` 原样返回。
  现在只有实测（HEAD，退化 `Range: bytes=0-0` 比对 Content-Length）缩放版确实更小、
  或本地压缩确实缩了字节，配文才写「已压缩」；不超过 `max_side` 的图直接发原图 URL、
  不再追加缩放参数，顺带省掉一次无意义的图床处理。
- **一张图只会发一次**：`_send_via_onebot` 返回三态 `SendOutcome`
  （`SENT` / `FAILED` / `UNKNOWN`），只有协议端**明确回报失败**（`ActionFailed` 等）
  才允许换通道回退；超时属于「结果未知」——消息很可能已经送达，一律就此打住，并给
  事件打上「已投递」标记，后续任何回退路径都不得再发。同时删除「缩放 URL 直发失败
  → 改发原图 URL」的第二次直发。修复「原图被重复发送两遍」：原图体积大、协议端要
  自行下载图床 URL，直发最容易超时，此前超时被当成失败，于是同一张图被发两次。
- **修掉同一张图被溯源两次**：`_extract_images` 的去重键从 `url or file` 改为
  收集 `url` / `file` / `path` 三个标识求交——同一张图在消息链里只有 `url`、在被
  引用消息里只有 `file` 时，此前会被判成两张，`/溯源` 于是对同一张图跑两遍、回传两遍。
- **`/原图` 可直接按文件名到图床取原图**：会话历史未命中时，按
  `{图床地址}/file/{文件名}` 拼直链（复用 ImgBed 公开直链口径）、探测存在后直传
  原图，不再要求「本会话先回传过」。图床地址取 `image_bed.cfi_base_url`
  （`cloudflare_imgbed`）或 `random_media.base_url`；无参 `/原图` 在历史为空时退化为
  「按消息/引用图的文件名直查」。新增纯函数 `build_imgbed_file_url`：拒绝 `..`、
  `//`、绝对 URL 与空名，中文/空格按 URL 规则转义，允许带上传目录。找不到时提示会
  附上尝试过的完整直链。**仍未接入**的查找来源：Qdrant 向量库按 `file_name` 过滤、
  本地 SQLite 图库按文件名匹配、会话历史落盘。
- **非本插件问题的日志指引**：README 常见问题新增 `pillowmd ... OSError: cannot open
  resource` 的定位与修复（堆栈里的 `astrbot_plugin_outputpro` 是另一个插件，转图时
  打不开配置里的字体文件），附三条修复路径。
- 版本号 1.5.2 → 1.5.3（`metadata.yaml`、README 徽章）；测试 116 → 137 条，ruff 全绿。

## 1.5.2（2026-09-14，相似图消息改为图文交替）

- **相似图消息改为图文交替排列**：合并回传那条消息现在按「配文 → 图片 → 配文 →
  图片」交替排列，每张图的文件名紧贴在自己那张图的上方。此前 QQ `aiocqhttp`
  路径是把全部文字拼成一段放在开头、全部图片追加在末尾，多图命中时文字挤成
  一团、图片也挤成一团，无法分辨哪句配文属于哪张图。标准消息链路径原本就是
  交替排列，本次把 OneBot 直发路径对齐，两条发送通道观感一致。
- **顺带删掉一个失效的文本拼接助手**：`_onebot_text` 的唯一用途是拼出"标题 +
  全部配文"那一段文本，交替排列后不再需要，已随本次改动移除。

## 1.5.1（2026-09-14，向量命中改为先提示后一条合并回传）

- **回传结构改为两条消息**：`/溯源` 向量命中先单独发送命中提示，随后用**一条**
  消息回传全部相似图。此前是每张相似图各发一条消息，多命中时会刷屏，且提示与
  图片混在一起不易扫读。实现上删除了专用的逐图回传路径（约 105 行），改由
  `_yield_delivery` 这个统一回传入口承担，`/溯源` 由此和 `/随机图`、哈希命中
  走同一条发送链路。
- **修掉配文重复两遍**：QQ `aiocqhttp` 路径下每张相似图的文件名文案会显示两遍。
  根因是调用方把 block 的文案同时当 `header` 传给 OneBot 直发函数，而该函数内部
  会把 `header` 与 `block["text"]` 一起拼接，同一句被算了两遍。改为合并回传后
  该调用方式不再存在，并补了断言出现次数的回归用例。
- **压缩图提示改为用法说明**：配文里的 `已压缩 /原图` 改为
  `已压缩，可发送 /原图 获取原图`，不再重复文件名——`/原图` 不带参数即取最近
  一张，文案里再抄一遍文件名属于冗余。

## 1.5.0（2026-09-14，整合随机图与 /原图）

- **整合随机图**：把独立随机图插件的 `/随机图`、`/随机视频` 命令与 LLM 工具
  `sendRandomMedia` 合入本插件，新增 `random_media` 配置组维护图床地址与接口
  参数；取回后的回传策略直接复用既有 `image_delivery`，随机图不再自带一套
  发送配置，插件面板少一组重复项。
- **新增 `/原图 [文件名]` 命令**：补上历史缺口——此前压缩回图文案已经提示
  「已压缩 /原图」，但插件其实并没有这个命令，用户照着提示发 `/原图` 只会
  没有反应；现在它按会话级原图历史找回最近回传图片的原图，`/溯源` 与
  `/随机图` 的命中结果共用同一份历史，回图文案里的提示终于名副其实。
- **`/原图` 按 CloudFlare-ImgBed 读取口径取原图**：剥离直链上的 `width` /
  `height` / `fit` / `fallback` 处理参数、强制原图 URL 直传，不下载也不本地
  中转——否则所谓「原图」仍是图床缩放后的版本，名不符实。
- **消除重复实现**：原随机图插件与 `image_delivery` / `main` 重叠的等比缩放、
  格式嗅探、本地压缩、OneBot 直传、配置解析等约 470 行改为复用，新增
  `random_media.py`（随机图接口客户端与响应解析纯函数）与 `media_history.py`
  （会话级原图历史）两个模块，模块数 10 → 12。
- **随机图出网纳入统一 SSRF 防护**：随机图接口请求同样经 `url_guard` 校验、
  走共享 `GuardedHttpClient`，图床必须公网可达，不再使用裸 `aiohttp` 会话。
- **修复错误页文本被误当图片直链**：图床出错时可能回 200 + 一段纯文本
  （HTML 错误页、`server is busy` 等），此前 `urljoin` 会把它拼成一条语法合法
  的 URL 当图片直链发进群，用户看到破图；现在拼接前先判定文本是否「像链接」，
  不像的直接判失败并给出可诊断的提示。

## 1.4.1（2026-09-14，向量命中逐图回传）

- **向量命中拆分消息**：先发送命中提示与“图片发送可能有延迟”，随后每张相似图
  独立发送文件名和图片；QQ `aiocqhttp` 的 OneBot 原生直传也逐条调用，不再把
  多张相似图合并在同一条消息里。
- **去重信息只写日志**：合并重复的数量、保留代表图与被合并 ID 记录到机器人
  日志，用户消息不再显示“合并重复 X 张”。
- **压缩标识前置**：缩放或压缩回图时，在文件名前标注“已压缩 /原图”，文件名
  只出现一次。

## 1.4.0（2026-09-14，URL 直传、图片压缩与同图去重）

- **图片回传改为 URL 直传优先**：QQ `aiocqhttp` 平台通过 OneBot 原生
  `send_group_msg` / `send_private_msg` 直接下发图片 URL，避免 AstrBot
  适配器把图片统一转 base64 造成协议端解析宽高失败；非该平台继续使用
  标准消息链。
- **加入图床缩放与本地压缩回退**：新增 `image_delivery` 配置组。默认
  `scaled-url` 模式只给 CloudFlare-ImgBed 标准 `/file/` 直链附加官方
  `width` / `height` / `fallback=original` 等比缩放参数；URL 直传失败后
  下载并本地压缩（EXIF 转正、等比缩放、透明铺白底、JPEG 重编码），
  动图保持原样。也支持 `original-url` 与 `local-compress`。
- **向量命中同图去重**：Qdrant 查询携带 `with_vector=true`，先按
  `src` / `image_url` 精确去重，再按候选向量 cosine 相似度合并
  （默认阈值 `0.995`），每组只回传相似度最高的一张并显示合并数量；
  Qdrant 未返回向量时仅做精确去重，兼容现有数据。

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
