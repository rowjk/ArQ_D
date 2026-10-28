"""
機密防護 (Secret Guard)：避免 .env 中的金鑰 / Token 外洩

ArQ 啟動時 load_dotenv() 會把 .env 全部載入 os.environ，這代表：
    1. 所有子程序 (PowerShell、Python、Claude Code、Antigravity…) 預設都會繼承這些金鑰，
       AI 只要執行 `echo $env:CWA_API_KEY` 就能讀到。
    2. 任何工具輸出若不小心帶到金鑰，會直接送進 Gemini 或 Discord。

因此本模組提供三道防線：
    - child_env()       ：建立「移除機密」的子程序環境變數 (需要的金鑰須明確 keep)。
    - redact() / redact_obj()：把輸出中出現的機密值替換成 ***，送往 Gemini / Discord 前呼叫。
    - is_secret_file()  ：判斷路徑是否為機密檔 (.env、憑證…)，供檔案讀取工具拒絕存取。

新增機密時：把環境變數名稱加進 SECRET_ENV_NAMES (名稱含 KEY / TOKEN / SECRET / PASSWORD 的也會自動視為機密)。
"""

import os
import re
from typing import Any, Dict, Iterable, List

# 明確列管的機密環境變數 (另外名稱符合 _SECRET_NAME_RE 的也會被視為機密)
SECRET_ENV_NAMES = {
    "DISCORD_BOT_TOKEN",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CWA_API_KEY",
    "SJ_API_KEY",       # 永豐 Shioaji
    "SJ_SEC_KEY",
    "SJ_CA_PASSWD",
}
_SECRET_NAME_RE = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)

# 執行期登記的額外機密值 (例如 MCP 設定檔以 ${VAR} 展開的金鑰)；名稱不符樣式也要遮蔽
_EXTRA_SECRET_VALUES: frozenset = frozenset()

# 太短的值不做替換，避免把常見字串誤遮
_MIN_SECRET_LEN = 8

# 機密檔案：.env 系列 (不含 .env.example)、Google 服務帳號憑證、私鑰
_SECRET_FILE_RE = re.compile(
    r"(^\.env(\..+)?$)|(^credentials\.json$)|(\.pem$)|(\.key$)|(^id_(rsa|ed25519|ecdsa)$)",
    re.IGNORECASE,
)
_SECRET_FILE_ALLOW = {".env.example"}


def _is_secret_name(name: str) -> bool:
    return name in SECRET_ENV_NAMES or bool(_SECRET_NAME_RE.search(name))


def register_extra_secrets(values: Iterable[str], replace: bool = True) -> None:
    """
    登記額外要遮蔽的機密值；replace=True 時取代先前登記的全部 (重新載入設定用)。
    整組換成新的 frozenset：其他執行緒正在遮蔽時不會遇到「集合在迭代中被修改」。
    """
    global _EXTRA_SECRET_VALUES
    new = {v.strip() for v in values if v and len(v.strip()) >= _MIN_SECRET_LEN}
    _EXTRA_SECRET_VALUES = frozenset(new if replace else _EXTRA_SECRET_VALUES | new)


def secret_values() -> List[str]:
    """目前環境中所有機密的值 (由長到短，避免短值先替換破壞長值)"""
    values = {
        v for k, v in os.environ.items()
        if _is_secret_name(k) and v and len(v.strip()) >= _MIN_SECRET_LEN
    } | _EXTRA_SECRET_VALUES
    return sorted((v.strip() for v in values), key=len, reverse=True)


def redact(text: str) -> str:
    """把字串中出現的機密值替換成 ***"""
    if not text:
        return text
    for value in secret_values():
        if value in text:
            text = text.replace(value, "***")
    return text


def redact_obj(obj: Any) -> Any:
    """遞迴遮蔽 dict / list / str 中的機密值 (用於工具回傳結果)"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    return obj


def child_env(keep: Iterable[str] = (), base: Dict[str, str] = None) -> Dict[str, str]:
    """
    建立給子程序用的環境變數：移除所有機密，只保留 keep 中明確列出的。
    例如 Claude Code 需要 ANTHROPIC_API_KEY 時：child_env(keep=["ANTHROPIC_API_KEY"])。
    """
    keep = set(keep)
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if _is_secret_name(name) and name not in keep:
            env.pop(name, None)
    return env


def is_secret_file(path: str) -> bool:
    """路徑是否指向機密檔 (.env、credentials.json、私鑰…)"""
    name = os.path.basename(str(path).strip().strip('"').strip("'"))
    if name.lower() in _SECRET_FILE_ALLOW:
        return False
    return bool(_SECRET_FILE_RE.search(name))
