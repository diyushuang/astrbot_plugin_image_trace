import pytest
from PIL import Image

from astrbot_plugin_image_trace import features
from astrbot_plugin_image_trace.features import (
    _dct_matrix,
    avif_available,
    compute_features,
    heif_available,
    image_bytes_ok,
    image_file_ok,
    image_mime,
    phash_hex_len,
)


def test_phash_length_matches_hash_size():
    assert phash_hex_len(4) == 4
    assert phash_hex_len(16) == 64
    assert phash_hex_len(32) == 256


def test_compute_features_does_not_mutate_pillow_global_limit(tmp_path, monkeypatch):
    original_limit = Image.MAX_IMAGE_PIXELS
    path = tmp_path / "image.png"
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(path)

    result = compute_features(path, hash_size=8)

    assert len(result.phash) == phash_hex_len(8)
    assert result.width == 4
    assert result.height == 4
    assert original_limit == Image.MAX_IMAGE_PIXELS


def test_compute_features_rejects_oversized_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "_MAX_LOAD_PIXELS", 1)
    path = tmp_path / "image.png"
    Image.new("RGB", (2, 2), color=(0, 255, 0)).save(path)
    try:
        compute_features(path, hash_size=8)
    except ValueError as error:
        assert "超过上限" in str(error)
    else:
        raise AssertionError("Expected pixel limit error")


def test_dct_matrix_cache_returns_same_matrix():
    assert _dct_matrix(8) is _dct_matrix(8)


def test_image_file_ok_accepts_real_image_and_rejects_garbage(tmp_path):
    good = tmp_path / "good.png"
    Image.new("RGB", (3, 3)).save(good)
    assert image_file_ok(good)

    bad = tmp_path / "bad.bin"
    bad.write_bytes(b'{"error":"MetaData is empty"}' * 5)
    assert not image_file_ok(bad)

    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    assert not image_file_ok(empty)

    missing = tmp_path / "missing.jpg"
    assert not image_file_ok(missing)


def test_image_mime_detects_content_instead_of_extension(tmp_path):
    path = tmp_path / "actually-png.jpg"
    Image.new("RGB", (3, 3), color=(0, 128, 255)).save(path, format="PNG")

    assert image_mime(path) == "image/png"
    assert image_file_ok(path)


def test_image_mime_rejects_non_image_and_truncated_image(tmp_path):
    text = tmp_path / "text.png"
    text.write_text("not an image")
    assert image_mime(text) is None

    image = tmp_path / "truncated.png"
    Image.new("RGB", (8, 8), color=(255, 0, 0)).save(image, format="PNG")
    data = image.read_bytes()
    image.write_bytes(data[: len(data) // 2])
    assert image_mime(image) is None


def _fake_heic_bytes(brand: bytes = b"heic") -> bytes:
    """构造只有文件头的 HEIC：容器可识别，但没有可解码的数据。"""
    return b"\x00\x00\x00\x18" + b"ftyp" + brand + b"\x00\x00\x00\x00" + b"mif1" + b"\x00" * 48


def test_heic_container_is_recognized_even_without_decoder(tmp_path):
    """HEIC 容器必须能被认出来，缺解码器时 image_mime 明确返回 None。

    「认得出容器却解不开」与「认不出任何图片容器（错误体）」是两回事：前者
    链接指向同一份内容，重下也不会有不同结果，调用方据此跳过兜底下载并给出
    可操作的提示（见 main._resolve_local_file）。
    """
    from astrbot_plugin_image_trace.image_delivery import sniff_image_format

    assert sniff_image_format(_fake_heic_bytes()) == "HEIF"
    assert sniff_image_format(_fake_heic_bytes(b"mif1")) == "HEIF"

    path = tmp_path / "photo.heic"
    path.write_bytes(_fake_heic_bytes())
    assert image_mime(path) is None
    assert not image_file_ok(path)


@pytest.mark.skipif(not heif_available(), reason="未安装 pillow-heif")
def test_heic_is_decodable_when_pillow_heif_is_installed(tmp_path):
    """装了 pillow-heif 后，HEIC 要能正常识别、完整解码并参与特征计算。"""
    path = tmp_path / "photo.heic"
    Image.new("RGB", (32, 24), color=(10, 20, 30)).save(path, format="HEIF")

    assert image_file_ok(path)
    assert image_mime(path) == "image/heic"
    feats = compute_features(path, hash_size=8)
    assert (feats.width, feats.height) == (32, 24)


def test_image_bytes_ok_matches_image_file_ok(tmp_path):
    """按字节校验与按文件校验判据必须一致（回退路径不再落盘后再校验）。"""
    good = tmp_path / "good.png"
    Image.new("RGB", (24, 16), color=(1, 2, 3)).save(good, format="PNG")
    assert image_bytes_ok(good.read_bytes()) is image_file_ok(str(good)) is True

    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"<html>hostile</html>")
    assert image_bytes_ok(bad.read_bytes()) is image_file_ok(str(bad)) is False

    truncated = tmp_path / "shot.png"
    truncated.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 2])
    assert image_bytes_ok(truncated.read_bytes()) == image_file_ok(str(truncated))

    assert image_bytes_ok(b"") is False


def test_avif_available_reflects_pillow_capability():
    """AVIF 能力必须以 Pillow 的 feature 探测为准，不能靠版本号猜。"""
    from PIL import features as pil_features

    try:
        expected = bool(pil_features.check("avif"))
    except Exception:
        expected = False
    assert avif_available() is expected


def test_scan_extensions_include_supported_formats():
    """重扫的扩展名表要覆盖 Pillow 能解码的格式；AVIF/HEIC 与能力开关一致。"""
    from astrbot_plugin_image_trace import main as main_module

    assert {".jpg", ".jpeg", ".png", ".tif", ".tiff"} <= main_module.IMAGE_EXTS
    assert (".avif" in main_module.IMAGE_EXTS) is main_module.avif_available()
    assert (".heic" in main_module.IMAGE_EXTS) is main_module.heif_available()
    assert (".heif" in main_module.IMAGE_EXTS) is main_module.heif_available()


def test_compute_features_with_mime_opens_image_once(tmp_path, monkeypatch):
    """一次解码同时拿特征与 MIME，不能为了取格式再 open 一次。"""
    path = tmp_path / "one-open.png"
    Image.new("RGB", (32, 24), color=(9, 8, 7)).save(path, format="PNG")

    opened = []
    original_open = features.Image.open

    def counting_open(fp, *args, **kwargs):
        opened.append(fp)
        return original_open(fp, *args, **kwargs)

    monkeypatch.setattr(features.Image, "open", counting_open)

    feats, mime = features.compute_features_with_mime(path, hash_size=8)

    assert len(opened) == 1
    assert mime == "image/png"
    assert (feats.width, feats.height) == (32, 24)
    assert len(feats.phash) == phash_hex_len(8)
