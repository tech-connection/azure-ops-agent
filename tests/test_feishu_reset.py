"""测试 longconn 里的纯函数（重置关键词识别）。"""
import os

os.environ.setdefault("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")

from app.feishu_longconn import _is_reset_command, _strip_mentions


def test_reset_keywords_chinese():
    assert _is_reset_command("清空")
    assert _is_reset_command("重置会话")
    assert _is_reset_command("/reset")
    assert _is_reset_command("/clear")
    assert _is_reset_command("清空上下文！")


def test_reset_with_mention():
    assert _is_reset_command('<at user_id="ou_xxx">@bot</at> 清空')


def test_non_reset():
    assert not _is_reset_command("查 myvm 的 CPU")
    assert not _is_reset_command("我想清空磁盘缓存")  # 含"清空"但不是命令


def test_strip_mentions():
    out = _strip_mentions('<at user_id="ou_xx">@robot</at> 查 myvm cpu')
    assert "<at" not in out
    assert "myvm" in out
