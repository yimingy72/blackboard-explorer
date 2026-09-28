"""Small, explicit pages prevent evidence reads from flooding model context."""


def evidence_page(data: bytes, offset: int = 0, limit: int = 16000) -> str:
    if offset < 0 or not 1 <= limit <= 32000:
        return "offset 必须非负，limit 必须在 1–32000 字符之间。"
    if data.startswith((b"\x89PNG", b"\xff\xd8\xff")) or data[8:12] == b"WEBP":
        return "这是图片证据，请使用 view_image 查看；不能把二进制内容当文本阅读。"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "证据不是 UTF-8 文本。请通过工作区工具转换为可读格式后再保存。"
    if "\x00" in text:
        return "证据包含二进制内容，请先转换为可读文本。"
    if offset > len(text):
        return f"偏移超出范围；全文共 {len(text)} 字符。"
    end = min(offset + limit, len(text))
    continuation = f"；如需后文，以 offset={end} 继续读取" if end < len(text) else "；已到末尾"
    return f"[证据文本 {offset}–{end} / 共 {len(text)} 字符{continuation}]\n{text[offset:end]}"
