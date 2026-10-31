from astrbot_plugin_image_trace.image_bed import (
    ImageBedClient,
    _public_url,
    extract_json_path,
)
from astrbot_plugin_image_trace.s3_store import _s3_error_detail, uri_encode_path


async def get_client():
    raise AssertionError("HTTP client should not be used in these tests")


def make_client(config, data_dir):
    return ImageBedClient(str(data_dir), config, get_client)


def test_extract_json_path_supports_nested_values_and_arrays():
    data = {"data": {"links": [{"url": "https://img.example.com"}]}}
    assert extract_json_path(data, "data.links.0.url") == "https://img.example.com"
    assert extract_json_path(data, "data.missing.0.url") is None


def test_public_url_encodes_object_key_but_preserves_slash():
    assert _public_url("https://img.example.com/", "a b/с+.jpg") == (
        "https://img.example.com/a%20b/%D1%81%2B.jpg"
    )


def test_oci_key_from_url_requires_same_origin_and_configured_bucket(tmp_path):
    client = make_client(
        {
            "mode": "oracle_oci",
            "oci_namespace": "namespace",
            "oci_region": "ap-osaka-1",
            "oci_bucket": "bucket",
            "oci_public_bucket": True,
        },
        tmp_path,
    )
    valid = (
        "https://objectstorage.ap-osaka-1.oraclecloud.com/n/namespace/b/bucket/o/a%20b.jpg?token=1"
    )
    key, matched = client._oci_key_from_url(valid)
    assert matched is True
    assert key == "a b.jpg"

    invalid = "https://evil.example.com/n/namespace/b/bucket/o/a.jpg"
    assert client._oci_key_from_url(invalid) == ("", False)


def test_src_path_for_cfb_uses_same_origin_and_path_only(tmp_path):
    client = make_client(
        {
            "mode": "cloudflare_imgbed",
            "cfi_base_url": "https://img.example.com",
        },
        tmp_path,
    )
    assert client.src_path_for("https://img.example.com/file/a%20b.jpg?x=1") == "/file/a b.jpg"
    assert client.src_path_for("https://evil.example.com/file/a.jpg") == (
        "https://evil.example.com/file/a.jpg"
    )


async def test_generic_upload_failure_falls_back_to_local_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image-data")
    client = make_client({"mode": "generic_http"}, tmp_path / "plugin")

    async def fail_upload(_path):
        raise RuntimeError("https://secret.example/upload token=secret failed")

    monkeypatch.setattr(client, "_upload", fail_upload)
    stored = await client.store(str(source))

    assert stored.ok is True
    assert stored.url is None
    assert stored.file_path is not None
    assert "详情见日志" in stored.message
    assert "secret" not in stored.message
    assert stored.file_path.startswith(str(tmp_path / "plugin" / "images"))


async def test_cfb_delete_requires_same_origin_and_strips_query(tmp_path, monkeypatch):
    client = make_client(
        {
            "mode": "cloudflare_imgbed",
            "cfi_base_url": "https://img.example.com",
            "cfi_token": "token",
        },
        tmp_path,
    )
    deleted = []

    async def fake_delete(token, file_id):
        deleted.append((token, file_id))
        return True

    monkeypatch.setattr(client, "_cf_delete", fake_delete)
    assert await client.delete_remote("https://img.example.com/file/a%20b.jpg?x=1")
    assert deleted == [("token", "a b.jpg")]
    assert await client.delete_remote("https://evil.example.com/file/a.jpg") is False


def test_s3_path_encoding_and_error_detail_refuse_entities():
    assert uri_encode_path("/bucket/a b+%.jpg") == "/bucket/a%20b%2B%25.jpg"
    malicious = b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Error/>'
    assert "<!ENTITY" in _s3_error_detail(malicious)
