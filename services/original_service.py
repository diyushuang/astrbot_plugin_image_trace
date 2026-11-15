"""R1 服务层：原图查找流程：按名反查、缩略图反查、图床直链探测。

服务通过构造注入的 host 门面访问 library / vector / storage / 图床等
依赖，禁止反向 import main；main 中的同名方法只做薄委托。
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from astrbot.api import logger

from ..image_delivery import build_imgbed_file_url, build_original_url
from ..random_media import clean_display_name, is_thumbnail_url


class OriginalService:
    def __init__(self, *, host) -> None:
        self.host = host

    async def _reverse_original(self, fetch, *, action: str, level: str = "warning") -> tuple[str, str]:
        """执行一次原图反查，返回 (payload 原名, 还原后的原图直链)。

        三个反查入口（缩略图直链 / 缩略图名 / 裸文件名）共用这段“不可用判定 →
        调用 → 异常只记日志 → 取 url → 按原图口径剥离处理参数”的骨架；各自的
        展示名回退顺序不同，留在调用点处理，避免把三种文案揉进一个函数。
        查不到/不可用一律返回 ("", "")，永不抛错。
        """
        if not self.host.vector.qdrant_ready:
            return "", ""
        try:
            info = await fetch()
        except Exception as exc:  # 反查方法自身已兜错，这里再保一层
            getattr(logger, level)(f"{action}失败: {exc}")
            return "", ""
        if not isinstance(info, dict):
            return "", ""
        original = str(info.get("url") or "").strip()
        if not original:
            return "", ""
        return str(info.get("file_name") or "").strip(), build_original_url(original)

    async def _original_from_thumbnail(self, thumb_ref: str) -> tuple[str, str]:
        """把「指向缩略图的直链或名字」换成它对应的原图 (名称, 直链)。

        为什么必须在这里拦：`/原图` 的直查分支只会拿一个名字去拼
        `{base}/file/{name}`。当这个名字来自「用户引用了上一条 /随机图 发出的
        缩略图」时，它指向 `thumbnails/`，拼出来必然命中缩略图，于是
        `/原图` 忠实地把 720px 小图又发了一遍——线上报的故障就是这个。

        反查走 Qdrant：缩略图与原图落在**同一个点**上（payload 里
        `thumb_url` 与 `image_url` 并存），按 thumb_url 即可取回原图。
        这里特意**不按名字倒推点 id**——缩略图被图床改过名（前置毫秒时间戳、
        `@` 换 `_`），倒推做不到。

        **host 差异由反查侧吸收，这里不强行归一化**。实测该部署的 payload：
            thumb_url = http://10.0.0.5:7658/file/thumbnails/178965…_【微博_…】.jpg
            image_url = http://10.0.0.5:7658/file/12%E3%80%81…%E5%89%A7%E7%85%A7.jpg
        两者都记**内网 IP**；而插件运行期手上的直链可能来自公网反代
        `bed.example.com`（OpenResty → `10.0.0.6:8080`，同一图床）。故这里只把
        编码形态还原成原始形态（`unquote`，缩略图那侧存的是**未编码**串），
        实际匹配交给 `original_info_for_thumb`：它先试整串、再退回按 `/file/`
        之后的**对象键**匹配，从而与 host 无关。

        查不到时返回 ("", "")，调用方据此给出可读提示；宁可说「找不到」，也
        绝不把缩略图当原图发出去。
        """
        if not self.host.vector.qdrant_ready:
            return "", ""
        reference = str(thumb_ref or "").strip()
        if not reference:
            return "", ""
        base = self.host._imgbed_base()
        # 统一成「与 payload 同形」的完整缩略图直链：
        # 1) 有站点前缀就直接用；2) 只有路径段则补上前缀；3) 两者都无则作罢
        if reference.lower().startswith(("http://", "https://")):
            full = reference
        elif base:
            full = f"{base}/file/{reference.lstrip('/')}"
        else:
            return "", ""
        # 关键：还原到**原始未编码**形态（payload.thumb_url 就是这么存的），
        # 同时去掉可能残留的处理参数（width/height/fit/fallback）
        thumb_url = unquote(build_original_url(full))
        display, url = await self.host._reverse_original(
            lambda: self.host.vector.original_info_for_thumb(thumb_url),
            action="缩略图反查原图",
        )
        if not url:
            return "", ""
        # 展示名**必须**优先取 payload 里的 file_name：缩略图名的 `@`→`_` 不可逆，
        # 光清洗缩略图名还原不出真名（实测 payload.file_name 才是原始名）
        return display or clean_display_name(url) or clean_display_name(reference) or "", url

    async def _original_from_thumb_name(self, name: str) -> tuple[str, str]:
        """按「缩略图改名后的文件名」反查原图 (名称, 直链)；查不到返回 ("", "")。

        与 `_original_from_thumbnail` 的分工：那个认**带 `thumbnails/` 段**的直链，
        这个只认**文件名**，用于目录段已经在流转中丢掉的场景（QQ 把机器人发的图
        转存到自己的 CDN 后，插件拿到的只有裸文件名）。两者都是反查，返回的都
        是同一个 Qdrant 点上的原图直链。

        展示名取 payload.file_name（图床登记真名，`@` 完好）；反查失败返回空，
        由调用方继续走裸名探测与可读提示——**绝不把缩略图当原图发出去**。
        """
        if not self.host.vector.qdrant_ready:
            return "", ""
        query = str(name or "").strip()
        if not query:
            return "", ""
        display, url = await self.host._reverse_original(
            lambda: self.host.vector.original_info_for_thumb_name(query),
            action="按缩略图名反查原图",
            level="debug",
        )
        if not url:
            return "", ""
        return display or clean_display_name(url) or query, url

    async def _lookup_imgbed_file(self, name: str, *, quoted: str = "") -> tuple[str, str, str]:
        """按文件名到图床直查原图，返回 (名称, 直链, 失败提示)。

        **查询顺序：先向量库按名反查，再退回裸名拼 URL + 探测。** 反查在前是
        必须的，不是优化：图床的 `/file/{path}` 只认**完整对象键**（含各级目录），
        裸文件名拼上去**必然 404**。线上报的

            图床里没有找到「【微博@潮流合伙人】20200207-04：赵今麦海报.jpg」

        就是这个：原图实际在 `/file/7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》
        图集/` 下面，而插件拼的是 `/file/【微博@潮流合伙人】…jpg`。实测 A/B：
        裸名 404 / 带目录 200（内网与公网域名结果一致）。Qdrant 的 payload
        `image_url` 正是入库时登记的**带完整目录**的权威直链，拿名字反查它即可。

        反查非「恰好 1 命中」时（同名文件在多目录下有副本、或该图未进索引）不硬猜，
        退回原有的裸名拼 URL + 探测逻辑：那一步虽然对多级目录无能为力，但对
        「图床根目录下的文件」以及**用户直接给完整相对路径**的情形仍然有效，
        且探测结果能给出可读提示。

        探测为「确定不存在」（4xx）时返回空直链并给出提示，同时附上尝试过的
        完整直链，便于用户核对图床里的目录层级；探测结果未知（网络异常）不拦，
        仍把直链交给回传——网络抖动不该被当成「图床没有这张图」。

        name 指向缩略图时（`thumbnails/...`，典型来自引用上一条随机图消息）先
        反查真正的原图直链再返回，绝不把缩略图当原图交付。quoted 是引用图里的
        **原始直链**（保留 `thumbnails/` 段），有它时优先用它反查——name 可能
        已被清洗掉时间戳前缀，而 strip 之后的形态与 Qdrant 里存的并不完全一致。
        """
        base = self.host._imgbed_base()
        if not base:
            return (
                "",
                "",
                "未配置图床站点地址：请在「图床设置」（cloudflare_imgbed）或「随机图」里"
                "填写站点地址后重试，或先 /溯源 命中再发 /原图。",
            )
        # 判定要用「原始引用直链」与「名字」两者：任一指明 thumbnails/ 都算命中
        if is_thumbnail_url(quoted) or is_thumbnail_url(name):
            original_name, original_url = await self.host._original_from_thumbnail(quoted or name)
            if original_url:
                logger.info(
                    f"/原图 直查命中缩略图，已反查回原图: {quoted or name} -> {original_url}"
                )
                return original_name, original_url, ""
            return (
                "",
                "",
                f"「{name}」是图床里的缩略图，且查不到它对应的原图；"
                "请改用原图文件名，或先 /溯源 命中该图后再发 /原图。",
            )
        # ① 先按文件名反查：拿回入库时登记的、**带完整目录**的权威直链。
        #    这一步解决「裸名拼 URL 必然 404」——图床 /file/ 只认完整对象键。
        #    注意判据是 `found[1]` 而**不是** `if found:`：`_original_by_name`
        #    以 `("", "")` 表示落空，而**非空元组恒为真**——写成 `if found:`
        #    会在落空时也走进来，直接返回空直链（调用方拿到空 url 会当成
        #    「查不到」），恰好把兜底路径②整个跳过。这是个静默错误：
        #    没有异常、没有日志，只是所有直查都失效。
        found = await self.host._original_by_name(name)
        if found[1]:
            original_name, original_url = found
            logger.info(f"/原图 按名反查命中原图: {name} -> {original_url}")
            return original_name, original_url, ""
        # ①.5 名字本身是「缩略图被改名后的文件名」时，按缩略图名反查原图。
        #     这一段要放在 ② 之前：这类名字（`1789…_【微博_…】.jpg`）在图床里
        #     只存在于 `thumbnails/` 下，裸名拼 URL 必然 404，探测只会给出
        #     「图床里没有找到」这种让人白核对的提示。
        #
        #     为什么 ① 拦不住它：① 按 payload.file_name 精确匹配，而缩略图名
        #     被图床改过（前置毫秒时间戳、`@` 换成 `_`，且 `_` 不可逆）。
        #     为什么 quoted 也可能拦不住：用户引用机器人消息时，QQ 会把图转存到
        #     自己的 CDN，插件手上的直链里**没有 `thumbnails/` 段**，
        #     `is_thumbnail_url` 只能返回 False，于是走到这里。
        #
        #     只对「裸文件名」生效：入参自带目录时（`/原图 目录/海报.jpg`）说明
        #     用户给的是**原图路径**，而缩略图名反查只按 basename 比对，会把别的
        #     目录下同名缩略图的原图当成本次要取的文件——比查不到更糟。
        thumb_hit = ("", "")
        if "/" not in name:
            thumb_hit = await self.host._original_from_thumb_name(name)
        if thumb_hit[1]:
            original_name, original_url = thumb_hit
            logger.info(f"/原图 按缩略图名反查回原图: {name} -> {original_url}")
            return original_name, original_url, ""
        # ② 反查未命中（未进索引 / 同名多份 / 向量检索关闭）→ 退回裸名拼 URL + 探测，
        #    对根目录文件与「用户直接给完整相对路径」两种情形仍然管用
        candidate = build_imgbed_file_url(base, name)
        if not candidate:
            return (
                "",
                "",
                f"「{name}」不像图床里的文件名，无法直接取原图；"
                "可先用 /溯源 命中该图后再发 /原图。",
            )
        exists, _length = await self.host._probe_url(candidate)
        if exists is False:
            # 落空原因不同，提示必须不同：库里压根没这个名字（未进索引）时让用户
            # 去 /溯源；而同名多份且内容不同时图是**确实存在**的，报「图床里没有
            # 找到」只会让人白核对一遍。命中数只在失败路径上多查一次。
            matches = await self.host._file_name_matches(name)
            if matches and matches > 1:
                return (
                    "",
                    "",
                    f"「{name}」在图库里有 {matches} 份同名文件且内容不同，"
                    "无法确定你要哪一张。请带上目录再试（如："
                    f"/原图 目录名/{name}）。",
                )
            # 失败文案只带路径、不带主机：图床地址可能配置成内网 IP/内网域名，
            # 群聊里不应出现；完整直链留在日志，便于管理员核对。
            candidate_path = unquote(urlsplit(candidate).path)
            logger.info(f"/原图 图床直查未命中: {name} -> {candidate}")
            return "", "", f"图床里没有找到「{name}」，已尝试路径：{candidate_path}"
        return name, candidate, ""

    async def _file_name_matches(self, name: str) -> int | None:
        """问向量库「这个名字有几个点」；用于失败文案的诊断，查不到返回 None。

        对齐库内 `VectorEngine.file_name_matches`——本方法存在的意义只是把
        「引擎未启用 / 异常」收敛成 None，让调用方不必到处判 self.host.vector。
        """
        if not self.host.vector.qdrant_ready:
            return None
        try:
            return await self.host.vector.file_name_matches(name)
        except Exception as exc:  # 诊断失败不能影响原本的失败提示
            logger.debug(f"同名命中数诊断失败: {exc}")
            return None

    async def _original_by_name(self, name: str) -> tuple[str, str]:
        """按裸文件名到向量库反查原图 (名称, 直链)；查不到返回 ("", "")。

        与 `_original_from_thumbnail` 的区别：那个按**缩略图直链**反查（用于拦
        「引用随机图发出来的图」），这个按**文件名**反查（用于 `/原图 文件名`
        这条「没有历史、只有名字」的路径）。

        名字里可能只有裸文件名，而图床里它在多级目录下——payload.image_url 存的
        是入库时的完整直链，故必须由库来告诉我们目录，不能靠猜。

        跳过 name 自带目录（含 `/`）的情形：那说明调用方已经拿到了相对路径，
        直接交给图床按路径取更准，不必多此一举反查（反查只匹配裸 file_name，
        带目录的名字在这一步本来就命中不了）。

        展示名用 payload.file_name（图床登记真名，`@` 完好），比入参更可信。
        本方法**永不抛错**：反查只是获取目录层级的捷径，失败由调用方走兜底。
        """
        query = str(name or "").strip()
        if not query or "/" in query or not self.host.vector.qdrant_ready:
            return "", ""
        display, url = await self.host._reverse_original(
            lambda: self.host.vector.original_by_file_name(query),
            action="按文件名反查原图",
        )
        if not url:
            return "", ""
        return display or query, url
