"""命令级错误文案脱敏。

``sanitize_error_text`` 已有单元测试，但此前没有用例证明**命令处理器**真的
会把经过脱敏的文案交给用户。这里驱动真实的 ``/签到文案导入``（管理员附件
导入）失败路径：让附件读取抛出带内网地址与查询串的异常，断言用户可见文本
不含 URL / IPv4，而日志里仍保留原始异常。
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_checkin_import_error_text_hides_url_and_ip(make_plugin, make_event):
    from astrbot.api.message_components import File as CompFile

    plugin = make_plugin()
    segment = CompFile(name="微博文案.csv")
    leaky = "读取失败: http://10.20.30.40:8080/secret.csv?token=abc123"

    async def boom(allow_return_url=False):
        raise RuntimeError(leaky)

    segment.get_file = boom
    event = make_event("/签到文案导入", chain=[segment], origin="sanitize-command")

    results = [item async for item in plugin.checkin_import(event)]

    assert len(results) == 1
    text = results[0].text
    assert text.startswith("读取文件失败：")
    assert "10.20.30.40" not in text
    assert "secret.csv" not in text
    assert "token=abc123" not in text
    assert "<地址已隐藏>" in text
