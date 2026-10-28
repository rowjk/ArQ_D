"""
文字處理工具：過長文字截斷、分段等純文字排版函式
"""

from typing import List


def split_message(text: str, limit: int = 1900) -> List[str]:
    """
    依換行將過長文字切成多個不超過 limit 字元的區塊。
    若切割點落在 ``` 程式碼圍欄內，會自動在上一塊補收尾 ```、在下一塊補開頭 ```<lang>，
    避免原本依固定字數硬切導致程式碼區塊跨訊息顯示錯亂。
    """
    lines = text.split("\n")
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    in_fence = False
    fence_lang = ""

    def flush():
        nonlocal current, current_len
        if not current:
            return
        chunk = "\n".join(current)
        if in_fence:
            chunk += "\n```"
        chunks.append(chunk)
        current = []
        current_len = 0

    for line in lines:
        stripped = line.strip()
        is_fence_marker = stripped.startswith("```")
        line_len = len(line) + 1

        # 單行本身就超過上限 (極端狀況)：獨立強制切斷，不混入其他行
        if line_len > limit:
            flush()
            step = max(limit - 10, 1)
            for i in range(0, len(line), step):
                chunks.append(line[i:i + step])
            continue

        if current_len + line_len > limit:
            flush()
            if in_fence:
                current.append(f"```{fence_lang}")
                current_len = len(fence_lang) + 4

        current.append(line)
        current_len += line_len

        if is_fence_marker:
            if not in_fence:
                in_fence = True
                fence_lang = stripped[3:].strip()
            else:
                in_fence = False
                fence_lang = ""

    flush()
    return chunks


def clip_text(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


_clip_text = clip_text
