"""图像感知特征提取：pHash / dHash / aHash。

仅依赖 Pillow 与 numpy：DCT 通过预计算的正交矩阵做矩阵乘法实现，无需 scipy。
特征以十六进制字符串表示，方便入库与比对。

- pHash 的位数随 hash_size 变化，其十六进制长度必须统一从
  phash_hex_len() 取，main / library 不得各自推导；
- dHash / aHash 固定 64bit，目前入库留存、检索仅用 pHash，
  预留作为后续二级确认字段。

注意：compute_features 为 CPU 密集操作，调用方应通过 asyncio.to_thread 执行。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

# 防解压炸弹：放宽 Pillow 默认的像素数告警阈值。放在模块级是因为 Pillow
# 按图片打开时的全局值判断，无法按单张图覆盖；这会影响宿主进程内其它
# Pillow 使用方（仅放宽告警阈值，真正的 DecompressionBombError 仍在两倍
# 值处抛出）。超限拒绝逻辑见 _load_rgb 的像素数预检。
Image.MAX_IMAGE_PIXELS = 50_000_000
_MAX_LOAD_PIXELS = 100_000_000  # 告警阈值的两倍：达到 DecompressionBombError 前先拒绝

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # 旧版本兜底
    _RESAMPLE = Image.LANCZOS

# DCT 正交矩阵缓存，key 为矩阵边长。compute_features 在 to_thread 中并发
# 首次遇到同一尺寸时会重复计算一次（CPython 下良性竞争，结果一致），
# 因此不加锁。
_DCT_CACHE: dict[int, np.ndarray] = {}


def phash_hex_len(hash_size: int) -> int:
    """pHash 十六进制串长度：hash_size² bit 经 packbits 补齐到字节边界后的字符数。

    期望长度在 main（建库）与 library（过滤）两侧使用，必须统一从这里取；
    此前两处各自用 hash_size²//4 推导，与实际长度仅在 hash_size 为 4 的
    倍数时一致，其它取值会导致全库数据被过滤、索引静默清空。
    """
    bits = hash_size * hash_size
    return 2 * ((bits + 7) // 8)


@dataclass
class ImageFeatures:
    phash: str  # pHash 十六进制（hash_size=16 时为 256bit / 64 字符）
    dhash: str  # dHash 十六进制（固定 64bit / 16 字符，预留二级确认）
    ahash: str  # aHash 十六进制（固定 64bit / 16 字符，预留二级确认）
    width: int
    height: int


def _dct_matrix(n: int) -> np.ndarray:
    """归一化正交 DCT-II 变换矩阵。"""
    mat = _DCT_CACHE.get(n)
    if mat is None:
        k = np.arange(n, dtype=np.float64)
        mat = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
        mat[0, :] *= np.sqrt(1.0 / n)
        mat[1:, :] *= np.sqrt(2.0 / n)
        _DCT_CACHE[n] = mat
    return mat


def _load_rgb(path: str) -> Image.Image:
    with Image.open(path) as src:  # with 退出时释放原文件句柄
        if src.width * src.height > _MAX_LOAD_PIXELS:
            raise ValueError(
                f"图片像素数 {src.width}x{src.height} 超过上限 {_MAX_LOAD_PIXELS}"
            )
        im = src
        try:
            im = ImageOps.exif_transpose(im)  # 总是返回新对象（副本/转正副本）
        except Exception:
            pass
        if getattr(im, "is_animated", False):  # 动图取第一帧
            im.seek(0)
        if im.mode not in ("RGB", "L"):  # 灰度图不必经 RGB 往返
            im = im.convert("RGB")
        elif im is src:
            im = src.copy()  # 不向外返回依赖文件句柄的原对象
        im.load()  # 像素读入内存，返回后不再依赖任何文件句柄
        return im


def _bits_to_hex(bits: np.ndarray) -> str:
    return np.packbits(bits.astype(np.uint8)).tobytes().hex()


def _phash(gray: Image.Image, hash_size: int) -> str:
    size = hash_size * 4
    pixels = np.asarray(gray.resize((size, size), _RESAMPLE), dtype=np.float64)
    mat = _dct_matrix(size)
    dct = mat @ pixels @ mat.T
    low = dct[:hash_size, :hash_size]
    return _bits_to_hex((low > np.median(low)).flatten())


def _dhash(gray: Image.Image) -> str:
    pixels = np.asarray(gray.resize((9, 8), _RESAMPLE), dtype=np.int16)
    return _bits_to_hex((pixels[:, 1:] > pixels[:, :-1]).flatten())


def _ahash(gray: Image.Image) -> str:
    pixels = np.asarray(gray.resize((8, 8), _RESAMPLE), dtype=np.float64)
    return _bits_to_hex((pixels > pixels.mean()).flatten())


def compute_features(path: str, hash_size: int = 16) -> ImageFeatures:
    """计算图片的感知特征。

    对压缩、缩放、轻微水印有较强鲁棒性；EXIF 方向会自动转正，动图取首帧。
    """
    hash_size = int(hash_size)
    if not 4 <= hash_size <= 32:
        raise ValueError("hash_size 仅支持 4~32")
    im = _load_rgb(path)
    gray = im if im.mode == "L" else im.convert("L")
    return ImageFeatures(
        phash=_phash(gray, hash_size),
        dhash=_dhash(gray),
        ahash=_ahash(gray),
        width=im.width,
        height=im.height,
    )
