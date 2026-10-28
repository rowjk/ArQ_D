"""
網路搜尋工具 (使用 Google Search Grounding 獲取即時公開資料)
"""

import os

from tools.gemini_settings import thinking_config
from tools.usage_log import record_usage, usage_of

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


def search_web(query: str) -> str:
    """使用 Google 搜尋引擎上網查詢最新網路資訊、即時新聞、天氣、時事、各領域資料或百科知識"""
    if not HAS_GENAI:
        return "尚未安裝 google-genai 模組，無法執行網路搜尋。"

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "未設定 GEMINI_API_KEY，無法執行網路搜尋。"

    try:
        client = genai.Client(api_key=api_key)
        model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        resp = client.models.generate_content(
            model=model_name,
            contents=f"請根據 Google 搜尋結果，針對以下主題提供最新且詳細的繁體中文說明：\n\n{query}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.3,
                thinking_config=thinking_config(),
            ),
        )
        record_usage("search_web", usage_of(resp))
        return resp.text or "查無相關網路搜尋結果。"
    except Exception as e:
        return f"上網搜尋時發生異常: {e}"
