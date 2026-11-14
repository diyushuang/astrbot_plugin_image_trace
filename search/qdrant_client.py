"""Qdrant 客户端：points 请求头/URL、检索/翻页/计数、payload 反查与写入。

只负责「与 Qdrant REST 交互」，配置读取与 Qdrant 是否可用的判定留在
`vector_search.VectorEngine` 门面。所有出网请求经注入的 `request_json`
回调（门面的 `_request_json`）发出，从而统一走 SSRF 校验并把 HTTP 状态码
收敛为 `HttpError`。

反查（thumb_url / file_name → 原图 payload）是本模块的主要复杂度来源：
图床把同一张图以多种 host/改名形式暴露，判据必须落在 payload 上，且命中
非唯一时一律放弃，绝不赌一个。
"""

from __future__ import annotations

import math
import os
import time
import uuid
from urllib.parse import quote, unquote, urlparse, urlsplit

try:
    from astrbot.api import logger as _module_logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    _module_logger = logging.getLogger("search.qdrant_client")

from ..features import image_mime
from ..random_media import strip_thumbnail_prefix
from .errors import HttpError, VectorEngineError

# 同名多份时，最多取回多少个候选做「是否同一张图」的判定。
# 实测本部署重名组最多 4 份，50 有足够余量；设上限是为了防止极端脏数据
# （某个名字挂着成百上千个点）把一次反查拖成大批量拉取。
DUPLICATE_SCAN_CAP = 50


def _mime_for(path: str) -> str:
    return image_mime(path) or "image/jpeg"


def thumb_path_key(url) -> str:
    """取直链里 `/file/` 之后的对象键（相对路径），用作反查的 host 无关判据。

    图床把「同一张图」以两种 host 暴露：内网 `10.0.0.5:7658` 与公网反代
    `bed.example.com`（OpenResty → `10.0.0.6:8080`）。`payload.thumb_url` 里存的是
    **入库当时**的那一种（实测本部署全是内网 IP），而插件运行期手上的直链可能是
    另一种，于是整串精确匹配永远 0 命中。对象键与 host 无关，只按它匹配即可。

    编码差异同样要吸收：库里 `thumb_url` 存**原始未编码**形态（含中文），插件手上
    的直链通常已 percent-encode，故先 `unquote` 再取路径。取不到 `/file/` 段时
    退回整条路径（去掉前导 `/`），保证本函数对非常规直链也可用。
    """
    try:
        path = unquote(urlsplit(str(url or "")).path)
    except Exception:
        return ""
    if not path:
        return ""
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    if "file" in segments:
        segments = segments[segments.index("file") + 1 :]
    return "/".join(segments)


class QdrantClient:
    """Qdrant points 的读取与写入客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        collection: str,
        top_k: int,
        request_json,
        embed_file,
        logger=None,
    ):
        """request_json / embed_file: 门面注入的回调（延迟解析，便于替换/打桩）。"""
        self.base_url = base_url
        self.api_key = api_key
        self.collection = collection
        self.top_k = top_k
        self._request_json = request_json
        self._embed_file = embed_file
        self._logger = logger or _module_logger

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def headers(self) -> dict:
        return {"api-key": self.api_key} if self.api_key else {}

    def points_url(self, operation: str = "") -> str:
        collection = quote(self.collection, safe="")
        url = f"{self.base_url}/collections/{collection}/points"
        return f"{url}/{operation}" if operation else url

    # ------------------------------------------------------------------
    # 检索与状态
    # ------------------------------------------------------------------

    async def search(
        self, vector: list, limit: int | None = None, *, with_vector: bool = False
    ) -> list:
        """Qdrant 相似度检索，返回 [{id, score, payload, vector?}] 列表。

        返回的是 Qdrant 原始分数（未做阈值过滤，排序由 Qdrant 给出）；
        阈值过滤与排序展示由调用方完成。
        """
        if (
            not isinstance(vector, list)
            or not vector
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
            )
        ):
            raise VectorEngineError("查询向量无效")
        effective_limit = self.top_k if limit is None else max(1, limit)
        try:
            obj = await self._request_json(
                "POST",
                self.points_url("query"),
                json_body={
                    "query": vector,
                    "limit": effective_limit,
                    "with_payload": True,
                    "with_vector": with_vector,
                },
                headers=self.headers(),
            )
            result = obj.get("result") or {}
            raw_hits = result.get("points", []) if isinstance(result, dict) else []
        except HttpError as exc:
            if exc.status != 404:
                raise
            obj = await self._request_json(
                "POST",
                self.points_url("search"),
                json_body={
                    "vector": vector,
                    "limit": effective_limit,
                    "with_payload": True,
                    "with_vector": with_vector,
                },
                headers=self.headers(),
            )
            raw_hits = obj.get("result") or []
        if not isinstance(raw_hits, list):
            raw_hits = []
        hits = []
        for h in raw_hits:
            if not isinstance(h, dict):
                continue
            payload = h.get("payload") or {}
            try:
                score = float(h.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            hits.append(
                {
                    "id": str(h.get("id")),
                    "score": score,
                    "payload": payload if isinstance(payload, dict) else {},
                    **({"vector": h.get("vector")} if with_vector else {}),
                }
            )
        return hits

    async def scroll_payloads(
        self,
        *,
        key: str,
        page_size: int = 512,
        max_points: int = 0,
        with_payload_keys: list | None = None,
    ) -> list:
        """遍历全集合，收集**存在 payload[key] 的点**，返回 [{id, payload}]。

        专为「把服务端预置的感知哈希整体拉到本地建索引」而设：Qdrant 不支持
        「按键非空」服务端过滤（`is_not_empty` 会 400），只能整集合翻页后
        在本地筛。翻页用 `next_page_offset`，直到为空或达到 max_points。

        只取需要的 payload 键（with_payload_keys），2 万级点位下能把响应体
        从数十 MB 压到几 MB。`key` 传入的键只要不是非空字符串就跳过该点——
        服务端把退化图写成哨兵串（'-'）而非空值，正是为了让这类点能被本地
        判据识别出来，所以这里不做真值判断，只要求「非空字符串」。

        出错时抛出 VectorEngineError（由调用方决定降级），不做静默吞错：
        半截索引比没有索引更危险——它会给出「未命中」的确定结论。
        """
        if not key:
            return []
        wanted = [str(k) for k in (with_payload_keys or [key]) if str(k)]
        collected: list = []
        offset = None
        while True:
            body = {
                "limit": max(1, int(page_size)),
                "with_payload": wanted,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            obj = await self._request_json(
                "POST", self.points_url("scroll"), json_body=body, headers=self.headers()
            )
            result = obj.get("result") or {}
            points = result.get("points") if isinstance(result, dict) else None
            if not isinstance(points, list) or not points:
                break
            for point in points:
                if not isinstance(point, dict):
                    continue
                payload = point.get("payload")
                if not isinstance(payload, dict):
                    continue
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                collected.append({"id": str(point.get("id")), "payload": payload})
                # 上限在页内也要生效：只在页间判定的话，一页 512 条会整页照收，
                # --limit 50 的试跑实际拿到 512 条，「先小样本验证」就失去意义
                if max_points > 0 and len(collected) >= max_points:
                    return collected
            offset = result.get("next_page_offset")
            if not offset:
                break
        return collected

    async def count(self) -> int:
        """返回集合点数；集合不存在按 0；连接/鉴权失败抛 VectorEngineError。"""
        url = self.points_url("count")
        try:
            obj = await self._request_json(
                "POST", url, json_body={"exact": True}, headers=self.headers()
            )
            # result 可能显式为 null，此时 .get 的默认值不生效，需再兜一层
            return int((obj.get("result") or {}).get("count") or 0)
        except HttpError as e:
            # 按状态码判定集合缺失，避免响应体偶然含 "404" 字样时误判
            if e.status == 404:
                return 0
            raise

    # ------------------------------------------------------------------
    # 反查（payload 定位 → 原图信息）
    # ------------------------------------------------------------------

    async def thumb_url_for(self, file_id: str) -> str | None:
        """取该原图对应点上的缩略图直链（payload.thumb_url）；没有则为 None。

        「一图一向量一替身」：点 id = UUID5(NAMESPACE_URL, 原图 id)，同一张原图的
        `image_url`（原图）与 `thumb_url`（缩略图）落在**同一个点**上。所以「原图 →
        缩略图」只需按 id 取点，既不需要映射表，也不能靠文件名——图床给缩略图改过
        名（加时间戳前缀、`@` 换 `_` 并移入 thumbnails/ 目录）。

        本方法**永不抛错**：缩略图只是提速手段，取不到就由调用方照常发原图，
        绝不能因为查不到替身而让发送失败。
        """
        key = str(file_id or "").strip()
        if not key:
            return None
        try:
            obj = await self._request_json(
                "POST",
                self.points_url(),
                json_body={
                    "ids": [self.point_id_for(key)],
                    "with_payload": True,
                    "with_vector": False,
                },
                headers=self.headers(),
            )
        except Exception as exc:  # 网络/鉴权/集合缺失一律视为「没有替身」
            self._logger.debug(f"查询缩略图失败（按无替身处理）{key}: {exc}")
            return None
        result = obj.get("result")
        if not isinstance(result, list) or not result:
            return None
        first = result[0]
        payload = first.get("payload") if isinstance(first, dict) else None
        if not isinstance(payload, dict):
            return None
        thumb = str(payload.get("thumb_url") or "").strip()
        return thumb or None

    async def original_info_for_thumb(self, thumb_url: str) -> dict | None:
        """按缩略图直链取回**整个原图点**的信息：{"url": …, "file_name": …}。

        比 original_url_for_thumb 多带一个 `file_name`，这一点很关键：缩略图名
        被图床改过（前置毫秒时间戳 + `@` 换 `_`），而 `@`→`_` 是**不可逆**的——
        光靠清洗缩略图名永远还原不出真正的文件名。同一个点的 payload 里
        `file_name` 存的正是**未经改名的原始文件名**（实测：
        file_name = `【微博@赵今麦工作室official】20250716-02：林其乐剧照.jpg`
        thumb_url = `…/thumbnails/1789658616018_【微博_赵今麦工作室official】….jpg`），
        所以展示名必须取自这里，而不是清洗缩略图名。

        file_name 可能缺失（图床侧 payload 约定不同），此时只返回 url，由调用方
        退回「按缩略图名清洗」的兜底展示。

        入参的 host 不必与库里存的一致：`_payload_by_thumb` 会先试整串精确匹配，
        再退回按路径匹配，故公网域名与内网 IP 两种写法都能反查到同一点。

        与 original_url_for_thumb 同样：命中数非 1 一律放弃，永不抛错。
        """
        target = str(thumb_url or "").strip()
        if not target:
            return None
        payload = await self._payload_by_thumb(target)
        if payload is None:
            return None
        original = str(payload.get("image_url") or "").strip()
        if not original:
            return None
        return {
            "url": original,
            "file_name": str(payload.get("file_name") or "").strip(),
        }

    async def original_info_for_thumb_name(
        self, thumb_name: str, *, scroll_filter=None
    ) -> dict | None:
        """按「缩略图改名后的**文件名**」反查原图；无法唯一确定时返回 None。

        为什么必须单独有这一条：`original_info_for_thumb` 的判据是直链路径里
        含 `thumbnails` 段。一旦这一段在流转中丢掉，整条反查就**静默失效**：

        - 用户引用机器人发出的图片时，QQ 会把图转存到自己的 CDN，插件拿到的
          `url` 是 `multimedia.nt.qq.com.cn/...`，`file` 只剩一个裸文件名；
        - `clean_display_name()` 按设计只取末段（展示用），目录层级同样被丢掉。

        这两种情形手上只剩「图床给缩略图改过名的那个文件名」
        （`1789658616018_【微博_赵今麦工作室official】….jpg`，`@` 已被换成 `_`）。
        此时按 `file_name` 反查原图必然 0 命中（真名里是 `@`、且没有时间戳前缀），
        裸名拼 `/file/{name}` 又必然 404 —— 用户看到的就是「图床里没有找到」。

        匹配分两步：先用 `match.text` 搜 `thumb_url`（库里存的是同一个被改名的
        串）缩小候选，再**在本地按 basename 精确比对**。后者才是判据——`text`
        匹配是分词匹配，拿它当命中依据会误配。

        返回的仍是**同一点上的原图**（`image_url` + `file_name`），所以即便调用方
        传进来的是原图名（碰巧命中缩略图字段），拿回的也是同一张图的原图直链，
        不会张冠李戴。候选非唯一（同名多目录）一律放弃，交调用方走原逻辑。

        本方法**永不抛错**：与其余反查入口一致，查不到就当没查到。
        """
        raw = str(thumb_name or "").strip()
        if not raw:
            return None
        # 只取文件名：调用方可能给的是相对路径
        name = unquote(raw.rsplit("/", 1)[-1]).strip()
        if not name or "." not in name:
            return None
        # 与 random_media 共用同一份清洗口径：只剥起始的「10 位以上数字 + 下划线」，
        # 不做任何猜测性还原（`@`→`_` 不可逆）
        stripped = strip_thumbnail_prefix(name).strip()
        if not stripped:
            return None
        query_filter = {"must": [{"key": "thumb_url", "match": {"text": stripped}}]}
        scroll_filter = scroll_filter or self.scroll_filter
        payloads = await scroll_filter(query_filter, f"缩略图名 {stripped}", 20)
        hits: list[dict] = []
        for payload in payloads:
            thumb = str(payload.get("thumb_url") or "").strip()
            if not thumb:
                continue
            base = unquote(thumb_path_key(thumb).rsplit("/", 1)[-1]).strip()
            # 两侧都要「带前缀/不带前缀」各比一次：调用方手上的名字可能已被上游
            # 剥掉毫秒前缀（`strip_leading_decoration` 就会剥），而库里存的
            # `thumb_url` 一定带前缀——只比原始形态会漏掉这种最常见的情形
            # （实测：带前缀的名字能命中，剥掉前缀后命中数直接从 1 掉到 0）。
            base_stripped = strip_thumbnail_prefix(base).strip()
            if base in (name, stripped) or base_stripped in (name, stripped):
                hits.append(payload)
        if not hits:
            self._logger.debug(f"按缩略图文件名反查未命中: {stripped}")
            return None
        if len(hits) > 1:
            # 命中多条是常态而不是异常：图床里同一个文件常被放进多个相册目录，
            # 入库时每份各建一个点（各带一份不同时间戳的缩略图副本）。实测本部署
            # 6,127 个重名组全部满足「同名 + 同大小 + 同 phash」，即同一张图。
            # 故直接复用 `_duplicate_equivalent` 的既有口径：等价才选一份，
            # 不等价（真正的同名不同图）一律放弃，绝不赌。
            if not self._duplicate_equivalent(hits):
                self._logger.debug(
                    f"按缩略图文件名反查命中 {len(hits)} 条且不等价，放弃: {stripped}"
                )
                return None
            chosen = self._pick_duplicate(hits)
            if chosen is None:
                return None
            self._logger.debug(
                f"按缩略图文件名反查命中 {len(hits)} 条等价点，已按路径字典序选定: {stripped}"
            )
            hits = [chosen]
        original = str(hits[0].get("image_url") or "").strip()
        if not original:
            return None
        return {
            "url": original,
            "file_name": str(hits[0].get("file_name") or "").strip(),
        }

    async def original_by_file_name(self, file_name: str) -> dict | None:
        """按**裸文件名**反查原图：{"url": 带目录的权威直链, "file_name": 真名}。

        为什么必须有这个方法：图床的 `/file/{path}` **只认完整对象键**（含各级
        目录），把裸文件名拼上去**必然 404**。而 `/原图 <文件名>` 这条路径手上
        只有名字，拼出来的就是 `/file/【微博@…】20200207-04：赵今麦海报.jpg`——
        图床里根本不存在这个对象，原图明明在
        `/file/7、综艺节目/爱奇艺《潮流合伙人》/《潮流合伙人》图集/` 下面。
        实测同一文件的 A/B 对照（线上 http://10.0.0.5:7658 与公网
        https://bed.example.com 结果一致）：

            裸文件名   /file/{裸名}            → HTTP 404
            带完整目录 /file/7、综艺节目/…/{裸名} → HTTP 200

        所以正确做法是拿名字去 Qdrant 反查那条**入库时登记的权威直链**
        （payload.image_url 里带完整目录），而不是凭名字去猜目录。

        **按值精确匹配**（`match.value`，不是 `match.text`）：`file_name` 是图床
        登记的真名，含 `@`、中文冒号等字符，用 text 匹配会被切成词而误命中。
        实测该字段可以直接整串精确命中。

        命中数为 0（库里没有这个名字，或压根没进索引）时返回 None，交给调用方
        退回「原样拼 URL + 探测」的老逻辑。

        **命中数 >1（同名文件在不同目录下有多份）时不再一律放弃**——这是 v1.7.1
        的行为变更，理由见 `_duplicate_equivalent`。实测本部署 23,932 点里
        **6,127 个文件名（37.3%）是重名的**，若一律放弃，这些名字发
        `/原图 <文件名>` 会 100% 报「图床里没有找到」，而它们的原图明明就在
        索引里。改为：先判定这些同名点是否**指向同一张图**，是则确定性地选一份
        返回；只有确认「同名但内容不同」才放弃（那种情况下赌错会把别的图当原图
        发出去，比说「找不到」更糟）。

        本方法**永不抛错**，失败一律返回 None。
        """
        target = str(file_name or "").strip()
        if not target:
            return None
        query_filter = {"must": [{"key": "file_name", "match": {"value": target}}]}
        label = f"文件名 {target}"
        hits = await self._count_filter(query_filter, label)
        if hits is None or hits == 0:
            return None
        if hits == 1:
            payloads = await self.scroll_filter(query_filter, label, 1)
            return self._original_info_from_payload(payloads[0] if payloads else None, target)
        # ---- 同名多份：先证明「是同一张图」，再确定性地选一份 ----
        payloads = await self.scroll_filter(query_filter, label, min(hits, DUPLICATE_SCAN_CAP))
        if not self._duplicate_equivalent(payloads):
            self._logger.debug(f"同名 {hits} 份且内容不同，放弃反查（需用户指明目录）: {label}")
            return None
        chosen = self._pick_duplicate(payloads)
        if chosen is None:
            return None
        info = self._original_info_from_payload(chosen, target)
        if info is not None:
            self._logger.info(
                f"同名 {hits} 份且内容一致，已确定性地取其一: {label} -> "
                f"{str(chosen.get('src') or chosen.get('image_url') or '')[:120]}"
            )
        return info

    @staticmethod
    def _original_info_from_payload(payload: dict | None, target: str) -> dict | None:
        """把 Qdrant payload 收敛成 {"url", "file_name"}；缺 image_url 时返回 None。

        缺 image_url 必须返回 None 而不是空直链：调用方拿空 url 会当成「查不到」
        走兜底，但若这里返 `{"url": ""}`，语义就变成了「命中了但直链是空的」，
        更容易在下游被误当作成功。
        """
        if not isinstance(payload, dict):
            return None
        original = str(payload.get("image_url") or "").strip()
        if not original:
            return None
        return {
            "url": original,
            "file_name": str(payload.get("file_name") or "").strip() or target,
        }

    @staticmethod
    def _duplicate_equivalent(payloads: list[dict]) -> bool:
        """判定「同名多份」的这些点是否**指向同一张图**。

        为什么可以有这个判定：图床里同一个文件被放进多个相册目录是常见操作，
        入库时每份各建一个点，于是同名点内容完全相同、只是目录不同。实测本部署
        6,127 个重名组**全部**满足：组内 `size_bytes` 逐组一致；随机抽 13 组下载
        全部副本算 sha256，**逐组字节完全相同**。故「同名 + 同大小」在本部署等价
        于「同一张图」，任选一份发给用户拿到的都是同一张图。

        判据（全部满足才算等价，任一不满足即判「不敢选」）：
        - 至少 2 个点；
        - 每个点的 `size_bytes` 都是**正数**且**组内完全一致**——缺大小就无从
          判定，一律当作不等价，宁可退回「找不到」也不赌；
        - `mime`（凡有值者）组内一致——防同名不同格式；
        - `phash`（凡有值者）组内一致——回填过的点能用时，它是比大小更硬的
          逐位证据，可**否决**「大小相同但内容不同」的巧合。

        注意 phash 只作否决、不作必要条件：本部署回填刚起步（实测仅 25 个点有
        值），要求 phash 存在会让这条修复对绝大多数名字失效。
        """
        if len(payloads) < 2:
            return False
        sizes: set = set()
        mimes: set = set()
        hashes: set = set()
        for payload in payloads:
            size = payload.get("size_bytes")
            if not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0:
                return False
            sizes.add(size)
            mime = str(payload.get("mime") or "").strip()
            if mime:
                mimes.add(mime)
            phash = str(payload.get("phash") or "").strip()
            if phash:
                hashes.add(phash)
        return len(sizes) == 1 and len(mimes) <= 1 and len(hashes) <= 1

    @staticmethod
    def _pick_duplicate(payloads: list[dict]) -> dict | None:
        """在已判定等价的同名候选里**确定性地**选一份。

        排序键取 `src`（图床相对路径），退化用 `image_url`。之所以要确定性而不是
        随便取第一个：Qdrant scroll 的返回顺序不保证稳定，同一个名字两次调用可能
        给出不同目录的直链，让用户看到「同一条命令发出的是不同链接」而怀疑数据在
        变。按路径字典序取最小，至少保证同名请求的结果可复现。
        """
        usable = [p for p in payloads if str(p.get("image_url") or "").strip()]
        if not usable:
            return None
        return min(usable, key=lambda p: str(p.get("src") or p.get("image_url") or ""))

    async def file_name_matches(self, file_name: str) -> int | None:
        """该文件名在库里的命中数；查询失败返回 None（区别于「确实是 0 条」）。

        只服务于 `/原图` **失败路径的文案诊断**，不参与取值决策。原因：反查落空
        后调用方会退回「裸名拼 URL + 探测」，而裸名必然 404，于是无论哪种落空原因
        都会报成「图床里没有找到」——对「同名多份但内容不同」这一种，图其实**确实
        存在**，用户照着提示去核对只会白跑。靠命中数把两种情况分开，才能给出可操作
        的提示（让用户补目录）。

        之所以单独发一次 count、而不是让 `original_by_file_name` 把原因带出来：
        后者要保持「返回 dict 或 None」的简单契约，而这条诊断只在失败时才需要，
        放在失败路径上按需付费，成功路径一次请求都不多发。
        """
        target = str(file_name or "").strip()
        if not target:
            return None
        query_filter = {"must": [{"key": "file_name", "match": {"value": target}}]}
        return await self._count_filter(query_filter, f"文件名诊断 {target}")

    async def _payload_by_thumb(self, thumb_url: str) -> dict | None:
        """按 thumb_url 取回**唯一**命中点的 payload；非唯一/失败返回 None。

        匹配分两轮，先严后宽：

        1. **整串精确匹配**（原行为）——入参与库中值逐字节相同时最快也最准；
        2. **按路径匹配**——只用 `/file/` 之后的**相对路径**（含 `thumbnails/` 段）
           去匹配。

        第二轮是必需的，不是冗余：`thumb_url` 里存的是**入库当时**的 host
        （实测本部署 6000/6000 全是内网 `10.0.0.5:7658`），而插件运行期
        手上的直链可能来自公网域名（同一图床的 OpenResty 反代，`bed.example.com`
        → `10.0.0.6:8080`）。同一个文件的两种写法 host 不同、路径逐字节相同，
        整串精确匹配必然 0 命中——v1.5.9 的 `/原图` 反查正是死在这里。按路径匹配
        把 host 排除在判据之外，两种写法都能命中。

        之所以**可以**只按路径匹配：图床对象键（`/file/` 之后的路径）是全局唯一的，
        实测采样 300 点，完整相对路径与去 `thumbnails/` 后的相对路径**均 300/300
        唯一命中、无多命中**。

        命中数必须**恰好为 1**：0 表示没有这个缩略图（正常），>1 表示数据异常
        （同一路径挂在多个点上），此时宁可返回 None 交调用方走原逻辑，也不赌一个。
        """
        target = str(thumb_url or "").strip()
        if not target:
            return None
        # 无尾斜杠时 rstrip 的结果与 target 相同：用 dict.fromkeys 保序去重，
        # 避免把同一条精确查询连发两遍（落空时白花一次 count 请求）。
        for candidate in dict.fromkeys((target, target.rstrip("/"))):
            payload = await self._payload_by_thumb_exact(candidate)
            if payload is not None:
                return payload
        key = thumb_path_key(target)
        if not key:
            return None
        return await self._payload_by_thumb_path(key)

    async def _payload_by_thumb_exact(self, thumb_url: str) -> dict | None:
        """按 thumb_url 整串精确匹配（逐字节相同才命中）。"""
        query_filter = {"must": [{"key": "thumb_url", "match": {"value": thumb_url}}]}
        return await self._unique_payload(query_filter, f"整串 {thumb_url}")

    async def _payload_by_thumb_path(self, path_key: str) -> dict | None:
        """按 `/file/` 之后的相对路径匹配，绕开 host 差异。

        Qdrant 的 `match.text` 对 text 字段按词切分、区分大小写，路径里的 `/` 与
        中文标点会被当作分隔符；这里传入的是完整相对路径（如
        `thumbnails/1789660677557_【微博_兰蔻LANCOME】20230507-04：郑州线下活动.jpg`），
        实测对真实数据可整串命中且唯一。仍以 count 唯一性为准，不唯一即放弃。
        """
        query_filter = {"must": [{"key": "thumb_url", "match": {"text": path_key}}]}
        return await self._unique_payload(query_filter, f"路径 {path_key}")

    async def _count_filter(self, query_filter: dict, label: str) -> int | None:
        """count(exact) 取命中数；网络/鉴权/集合缺失一律返回 None（调用方按「查不到」处理）。

        单独抽出来是因为它现在有两个调用方（唯一性命中判定、同名多份判定），
        而两者对「查询失败」的语义完全一致——都必须是「查不到」而不是「0 条」。
        """
        try:
            count_obj = await self._request_json(
                "POST",
                self.points_url("count"),
                json_body={"exact": True, "filter": query_filter},
                headers=self.headers(),
            )
            return int((count_obj.get("result") or {}).get("count") or 0)
        except Exception as exc:  # 网络/鉴权/集合缺失一律视为「查不到」
            self._logger.debug(f"反查计数失败（按查不到处理）{label}: {exc}")
            return None

    async def scroll_filter(self, query_filter: dict, label: str, limit: int = 1) -> list:
        """按 filter scroll 取回 payload 列表；失败返回空列表。

        只回 payload（调用方要的是 image_url/file_name/size_bytes 这些字段），
        不取向量——2 万级点位上带向量会把响应体放大一到两个数量级。
        """
        try:
            obj = await self._request_json(
                "POST",
                self.points_url("scroll"),
                json_body={
                    "filter": query_filter,
                    "limit": max(1, int(limit)),
                    "with_payload": True,
                    "with_vector": False,
                },
                headers=self.headers(),
            )
        except Exception as exc:
            self._logger.debug(f"反查取点失败（按查不到处理）{label}: {exc}")
            return []
        points = (obj.get("result") or {}).get("points")
        if not isinstance(points, list):
            return []
        payloads: list = []
        for point in points:
            payload = point.get("payload") if isinstance(point, dict) else None
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    async def _unique_payload(self, query_filter: dict, label: str) -> dict | None:
        """先 count(exact) 确认命中数恰为 1，再 scroll 取回该点 payload。

        count 便宜且能直接区分「没有」与「有多个」，避免拿到一页结果后还要自己
        判断是否唯一。两步共用同一个 filter，语义一致。
        """
        hits = await self._count_filter(query_filter, label)
        if hits != 1:
            # 0=没有这个缩略图（正常）；>1=数据异常，宁可不反查也不赌一个
            if hits is not None:
                self._logger.debug(f"缩略图反查命中 {hits} 条，放弃反查: {label}")
            return None
        payloads = await self.scroll_filter(query_filter, label, 1)
        return payloads[0] if payloads else None

    # ------------------------------------------------------------------
    # 登记原图时同步入向量库（图床无服务器侧钩子时的兜底通道）
    # ------------------------------------------------------------------

    @staticmethod
    def point_id_for(point_key: str) -> str:
        """Qdrant point ID 只接受整数或 UUID；用 UUID5 从内容指纹派生，保证幂等。"""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, point_key))

    async def upsert_file(
        self,
        path: str,
        point_key: str,
        image_url: str,
        file_size: int,
        width: int | None,
        height: int | None,
        src: str = "",
        *,
        mime: str = "",
    ) -> None:
        """写入/覆盖一个向量点。

        point_key 为内容指纹（调用方用 pHash 派生）：同图同 ID、与图床 URL
        规则解耦，任何能给出公开直链的图床模式都能同步且幂等。
        src 仅用于兼容图床侧的 payload 约定，插件自身只读 image_url。
        mime 由调用方在同一次解码里取得时传入，避免 embed_file 与 payload
        各再解一次图。
        """
        vector = await self._embed_file(path, mime)
        file_name = os.path.basename(unquote(urlparse(image_url).path)) or os.path.basename(path)
        payload = {
            "src": src or image_url,
            "image_url": image_url,
            "file_name": file_name,
            "mime": mime or _mime_for(path),
            "size_bytes": int(file_size or 0),
            "width": width,
            "height": height,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "channel": "",
        }
        # Qdrant point ID 只接受整数或 UUID；用 UUID5 从内容指纹派生，保证幂等
        point_id = self.point_id_for(point_key)
        url = f"{self.points_url()}?wait=true"
        body = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
        await self._request_json("PUT", url, json_body=body, headers=self.headers())

    async def delete_point(self, point_key: str) -> None:
        """按内容指纹删除向量点；点或集合不存在时按已删除处理。

        只清理插件侧以同规则写入的点（point_key = f"phash:{phash}"）；
        图床侧钩子写入的点使用图床自己的 key 约定，其清理需在图床侧完成。
        """
        url = f"{self.points_url('delete')}?wait=true"
        body = {"points": [self.point_id_for(point_key)]}
        try:
            await self._request_json("POST", url, json_body=body, headers=self.headers())
        except HttpError as e:
            if e.status == 404:  # 集合不存在等价于点已不存在
                return
            raise


# 供门面/测试复用的扫描上限（沿用 vector_search.DUPLICATE_SCAN_CAP 的旧名）
__all__ = ["DUPLICATE_SCAN_CAP", "QdrantClient", "thumb_path_key"]
