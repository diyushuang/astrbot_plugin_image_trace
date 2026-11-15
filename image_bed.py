"""泛用图床/对象存储客户端。

mode=local        : 原图副本保存到机器人数据目录（零配置，默认）。
mode=generic_http : 通过可配置的通用 HTTP 接口上传，兼容 Lsky Pro、
                    EasyImages、Chevereto 等常见自建图床——只需配置
                    上传地址、鉴权头、文件字段名与响应直链的 JSON 路径。
mode=cloudflare_r2: Cloudflare R2 对象存储（官方 S3 兼容 API），
                    endpoint https://<ACCOUNT_ID>.r2.cloudflarestorage.com，
                    签名 region 固定 auto；公开直链需在 Cloudflare 侧
                    开启 r2.dev 公开访问或绑定自定义域名。
mode=oracle_oci   : 甲骨文 OCI Object Storage（官方 Amazon S3 兼容 API），
                    endpoint https://<namespace>.compat.objectstorage.
                    <region>.oraclecloud.com，凭据为 Customer Secret Key；
                    公开直链支持公共存储桶原生 URL 或自定义地址（如
                    带前缀的预认证请求 PAR 地址）。
mode=cloudflare_imgbed: CloudFlare-ImgBed 自建图床（官方 REST API）：
                    POST /upload 上传（API Token 或上传鉴权码 authCode），
                    /溯源删除 时按 POST /api/manage/delete/batch 同步
                    删除远端文件（Token 需含 delete 权限）。

上传失败或无法生成长期有效的公开直链时，自动回退为本地副本保存，
保证登记流程不因图床故障而中断（溯源回图需要能长期访问的链接）。

本模块是统一入口与薄门面：按 mode 选择 `storage/` 下的具体后端，
并保持既有的返回文案、日志与回退行为不变。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import aiohttp

from .http_client import GuardedHttpClient
from .storage.backend_utils import timeout_seconds
from .storage.cloudflare_backend import CloudflareImgBedBackend
from .storage.http_backend import GenericHttpBackend
from .storage.local_backend import LocalBackend
from .storage.modes import MODE_CFB, MODE_HTTP, MODE_LOCAL, MODE_OCI, MODE_R2
from .storage.object_backend import ObjectBackend
from .storage.protocol import StoreResult
from .storage.urls import extract_json_path
from .storage.urls import public_url as _public_url

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("image_bed")

# 公开 API：MODE_* / StoreResult / 纯函数沿旧路径可导入（含 _public_url 别名）。
__all__ = [
    "ImageBedClient",
    "MODE_LOCAL",
    "MODE_HTTP",
    "MODE_R2",
    "MODE_OCI",
    "MODE_CFB",
    "StoreResult",
    "extract_json_path",
    "_public_url",
]

# 存储结果类型已迁到 storage/protocol.py，这里沿用导入名以保持向后兼容。


class ImageBedClient:
    def __init__(
        self,
        data_dir: str,
        bed_config: dict,
        http_getter: Callable[[], Awaitable[GuardedHttpClient]],
    ):
        self.mode = str(bed_config.get("mode") or MODE_LOCAL).strip()
        self.config = bed_config or {}
        self.local_dir = os.path.join(data_dir, "images")
        os.makedirs(self.local_dir, exist_ok=True)
        self._http_getter = http_getter
        self._local = LocalBackend(self.local_dir)
        self._http = GenericHttpBackend(self.config, http_getter, self._timeout)
        self._cfb = CloudflareImgBedBackend(self.config, http_getter, self._timeout)
        self._objects = ObjectBackend(
            self.config, http_getter, self.mode, self._timeout_seconds
        )

    # ---------- 对外接口 ----------

    async def store(self, src_path: str) -> StoreResult:
        """登记原图的落库：优先上传图床/对象存储，失败回退本地副本。"""
        uploaders = {
            MODE_HTTP: (self._upload, "图床上传失败", "已上传至图床"),
            MODE_CFB: (self._upload_cf, "CloudFlare-ImgBed 上传失败", "已上传至 CloudFlare-ImgBed"),
            MODE_R2: (self._upload_object, "Cloudflare R2上传失败", "已上传至Cloudflare R2"),
            MODE_OCI: (self._upload_object, "OCI 对象存储上传失败", "已上传至OCI 对象存储"),
        }
        entry = uploaders.get(self.mode)
        if entry is not None:
            upload, failure, ok_message = entry
            try:
                return StoreResult(ok=True, url=await upload(src_path), message=ok_message)
            except Exception as e:
                logger.warning(f"{failure}: {e}", exc_info=True)
                local = await self._copy_local(src_path)
                return StoreResult(
                    ok=True,
                    file_path=local,
                    message=f"{failure}，已保存本地副本（详情见日志）",
                )
        local = await self._copy_local(src_path)
        if self.mode != MODE_LOCAL:
            # schema 的 options 会拦住常见笔误，但手工改配置文件可绕过；
            # 静默按 local 处理会让人误以为配置生效了
            logger.warning(f"未知的图床模式 {self.mode!r}，已按本地副本保存。")
            return StoreResult(
                ok=True,
                file_path=local,
                message=f"图床模式 {self.mode!r} 无法识别，已保存本地副本",
            )
        return StoreResult(ok=True, file_path=local, message="已保存到本地图床")

    async def delete_remote(self, image_url: str) -> bool:
        """删除对象存储中的远端对象；URL 不属于当前配置时返回 False。

        对 cloudflare_r2 / oracle_oci 模式按公开直链反解对象名，与当前
        配置同源且路径前缀匹配后调用 DeleteObject；cloudflare_imgbed 模式
        按站点直链反解文件路径后调用官方删除 API（Token 需 delete 权限）。
        """
        if not image_url:
            return False
        try:
            if self.mode == MODE_CFB:
                file_id = self._cfb.file_id_from_url(image_url)
                token = str(self.config.get("cfi_token") or "").strip()
                if not file_id or not token:
                    return False
                return await self._cf_delete(token, file_id)
            if self.mode in (MODE_R2, MODE_OCI):
                return await self._objects.delete(image_url)
        except Exception as e:
            # 不吞异常原因：删除失败（token 权限不足、签名错误、网络故障）需要可诊断
            logger.warning(f"删除远端对象失败 [{self.mode}] {image_url}: {e}")
            return False
        return False

    def src_path_for(self, image_url: str) -> str:
        """从公开直链反解床内路径（仅用于向量 payload 的 src 字段）。

        能反解则返回床内相对路径（R2/OCI 为对象名、CFB 为 /file/xxx），
        否则原样返回直链。插件自身不依赖该字段，仅为兼容图床侧约定。
        """
        if not image_url:
            return ""
        try:
            if self.mode == MODE_CFB:
                return self._cfb.src_path_for(image_url) or image_url
            if self.mode in (MODE_R2, MODE_OCI):
                key, matched = self._objects.key_from_url(image_url)
                if matched:
                    return key
        except Exception as e:
            logger.debug(f"反解床内路径失败，回退直链: {e}")
        return image_url

    async def close(self) -> None:
        """保留兼容；HTTP 会话由共享客户端统一释放。"""
        return None

    # ---------- 后端薄委托 ----------

    def _timeout_seconds(self) -> int:
        return timeout_seconds(self.config)

    def _timeout(self) -> aiohttp.ClientTimeout:
        """图床 HTTP 请求的统一超时（配置缺失或非法时取 30 秒）。"""
        return aiohttp.ClientTimeout(total=self._timeout_seconds())

    async def _copy_local(self, src_path: str) -> str:
        return await self._local.save(src_path)

    async def _upload(self, src_path: str) -> str:
        return await self._http.upload(src_path)

    async def _upload_cf(self, src_path: str) -> str:
        return await self._cfb.upload(src_path)

    async def _upload_object(self, src_path: str) -> str:
        return await self._objects.upload(src_path)

    async def _cf_delete(self, token: str, file_id: str) -> bool:
        return await self._cfb.delete(token, file_id)

    def _oci_key_from_url(self, image_url: str) -> tuple[str, bool]:
        return self._objects.oci_key_from_url(image_url)
