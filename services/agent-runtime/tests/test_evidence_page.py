"""Large evidence remains fully readable without oversized model tool responses."""

from bbx_runtime.evidence import evidence_page


def test_evidence_pages_preserve_unicode_and_expose_offsets():
    text = "中文 evidence\n" * 4000
    first = evidence_page(text.encode())
    assert first.endswith(text[:16000])
    assert "offset=16000" in first
    assert len(first) < 16100
    second = evidence_page(text.encode(), 16000, 32000)
    assert second.endswith(text[16000:48000])
    assert "已到末尾" in second
    assert "offset 必须非负" in evidence_page(b"text", -1)
    assert "超出范围" in evidence_page(b"text", 5)
    assert "view_image" in evidence_page(b"\x89PNG\r\n\x1a\n")
    assert "二进制" in evidence_page(b"hello\x00world")
