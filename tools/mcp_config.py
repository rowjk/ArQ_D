"""
MCP 設定檔 (mcp_servers.json) 載入與驗證

檔案格式 (與常見 MCP 客戶端相容的 mcpServers)：
    {"mcpServers": {"<server 名稱>": {
        "command": "npx", "args": ["-y", "pkg@1.2.3"],
        "env": {"SOME_TOKEN": "${SOME_TOKEN}"},        # 金鑰類名稱只能整段引用環境變數
        "auto_approve": ["tool"], "include": [], "exclude": [],
        "call_timeout": 30, "startup_timeout": 60, "enabled": true}}}

安全規則：
    - 金鑰只能用 ${ENV_VAR} 引用 (名稱以 KEY / TOKEN / SECRET / PASSWORD / PASSWD / PWD / CREDENTIALS 結尾的 env
      一律必須整段是引用)，不得寫死
    - 不得引用 ArQ 的核心金鑰 (Discord / Gemini / Anthropic / TICKETS_*)
    - 單一 server 的設定錯誤只影響該 server，不影響其他 server 與原生工具
    - 指紋 (fingerprint) 涵蓋會影響「執行什麼」與「是否跳過同意」的欄位；經使用者核可的指紋才會啟動 server
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from tools.config import PROJECT_ROOT, env_str, temp_path
from tools.secret_guard import _is_secret_name

DEFAULT_CALL_TIMEOUT = 30
DEFAULT_STARTUP_TIMEOUT = 60
MAX_TIMEOUT = 3600

_ROOT = PROJECT_ROOT
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EXACT_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ALLOWED_FIELDS = {"command", "args", "env", "auto_approve", "include", "exclude",
                   "call_timeout", "startup_timeout", "enabled"}
_CORE_SECRET_NAMES = {"DISCORD_BOT_TOKEN", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"}
_CORE_SECRET_PREFIXES = ("TICKETS_",)
# secret_guard 只認 API_KEY / TOKEN / SECRET / PASSWORD 結尾；設定檔另外把 PRIVATE_KEY、DB_PASSWD 這類也當金鑰
_EXTRA_SECRET_NAME_RE = re.compile(r"(KEY|PASSWD|PWD|CREDENTIALS?)$", re.IGNORECASE)


def config_path() -> str:
    return env_str("ARQ_MCP_CONFIG") or os.path.join(_ROOT, "mcp_servers.json")


def approved_path() -> str:
    return env_str("ARQ_MCP_APPROVED") or temp_path("mcp_approved.json")


def is_secret_env_name(name: str) -> bool:
    return _is_secret_name(name) or bool(_EXTRA_SECRET_NAME_RE.search(name))


def is_core_secret(name: str) -> bool:
    return name in _CORE_SECRET_NAMES or name.startswith(_CORE_SECRET_PREFIXES)


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: List[str]
    env: Dict[str, str]                 # 已展開，只用來啟動子程序，不得寫入 log / 回覆
    auto_approve: frozenset
    include: Tuple[str, ...]
    exclude: Tuple[str, ...]
    call_timeout: int
    startup_timeout: int
    enabled: bool
    fingerprint: str

    def command_line(self) -> str:
        return " ".join([self.command, *self.args])


@dataclass
class ConfigResult:
    servers: Dict[str, ServerConfig] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    file_error: Optional[str] = None
    secret_values: List[str] = field(default_factory=list)   # 金鑰類 env 展開後的值，供輸出遮蔽


class _ConfigError(Exception):
    pass


def _string_list(entry: Mapping[str, Any], key: str) -> List[str]:
    value = entry.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _ConfigError(f"{key} 必須是字串陣列")
    return value


def _timeout(entry: Mapping[str, Any], key: str, default: int) -> int:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_TIMEOUT:
        raise _ConfigError(f"{key} 必須是 1~{MAX_TIMEOUT} 的整數秒數")
    return value


def _expand_env(env: Any, environ: Mapping[str, str], enabled: bool) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    """回傳 (展開後 env, 原始樣板 env (供指紋), 金鑰類展開值)"""
    if not isinstance(env, dict):
        raise _ConfigError("env 必須是物件")
    expanded: Dict[str, str] = {}
    templates: Dict[str, str] = {}
    secrets: List[str] = []
    for key, raw in env.items():
        if not isinstance(key, str) or not isinstance(raw, str):
            raise _ConfigError("env 的名稱與值都必須是字串")
        if is_core_secret(key):
            raise _ConfigError(f"env 不得使用 ArQ 核心金鑰名稱 {key}")
        refs = _REF_RE.findall(raw)
        if is_secret_env_name(key) and not _EXACT_REF_RE.match(raw):
            raise _ConfigError(f"{key} 屬於金鑰類，值只能整段寫成 ${{ENV_VAR}}，不得寫死或混合文字")
        for ref in refs:
            if is_core_secret(ref):
                raise _ConfigError(f"{key} 不得引用 ArQ 核心金鑰 {ref}")
        templates[key] = raw
        if not enabled:
            continue
        missing = [r for r in refs if not environ.get(r)]
        if missing:
            raise _ConfigError(f"環境變數 {missing[0]} 未設定 (請放在 .env)")
        value = _REF_RE.sub(lambda m: environ[m.group(1)], raw)
        expanded[key] = value
        if is_secret_env_name(key):
            secrets.append(value)
    return expanded, templates, secrets


def _fingerprint(name: str, command: str, args: List[str], templates: Dict[str, str], auto_approve: List[str],
                 include: List[str], exclude: List[str]) -> str:
    raw = json.dumps({"name": name, "command": command, "args": args, "env": templates,
                      "auto_approve": sorted(auto_approve), "include": include, "exclude": exclude},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_server(name: str, entry: Any, environ: Mapping[str, str]) -> Tuple[ServerConfig, List[str]]:
    if not _NAME_RE.match(name):
        raise _ConfigError("server 名稱只能用小寫英數字與單一連字號 (例如 my-server)")
    if not isinstance(entry, dict):
        raise _ConfigError("設定必須是物件")
    unknown = sorted(set(entry) - _ALLOWED_FIELDS)
    if unknown:
        raise _ConfigError(f"不支援的欄位：{', '.join(unknown)}")
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise _ConfigError("缺少 command")
    args = _string_list(entry, "args")
    if any("${" in a for a in args):
        raise _ConfigError("args 不得引用環境變數；金鑰請放在 env")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _ConfigError("enabled 必須是 true / false")
    auto_approve, include, exclude = (_string_list(entry, k) for k in ("auto_approve", "include", "exclude"))
    expanded, templates, secrets = _expand_env(entry.get("env", {}), environ, enabled)
    cfg = ServerConfig(
        name=name, command=command.strip(), args=args, env=expanded,
        auto_approve=frozenset(auto_approve), include=tuple(include), exclude=tuple(exclude),
        call_timeout=_timeout(entry, "call_timeout", DEFAULT_CALL_TIMEOUT),
        startup_timeout=_timeout(entry, "startup_timeout", DEFAULT_STARTUP_TIMEOUT),
        enabled=enabled,
        fingerprint=_fingerprint(name, command.strip(), args, templates, auto_approve, include, exclude),
    )
    return cfg, secrets


def load_config(path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> ConfigResult:
    """讀取並驗證設定檔；檔案不存在 = 沒有 server (不是錯誤)。不拋例外。"""
    path = path or config_path()
    environ = os.environ if environ is None else environ
    result = ConfigResult()
    if not os.path.isfile(path):
        return result
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        result.file_error = f"無法解析 JSON：{type(e).__name__}"
        return result
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        result.file_error = "頂層必須是 {\"mcpServers\": {...}}"
        return result
    for name, entry in servers.items():
        try:
            cfg, secrets = _parse_server(str(name), entry, environ)
        except _ConfigError as e:
            result.errors[str(name)] = str(e)
            continue
        result.servers[cfg.name] = cfg
        result.secret_values.extend(secrets)
    return result


def load_approved(path: Optional[str] = None) -> Dict[str, str]:
    """已核可的 {server 名稱: 指紋}；檔案不存在或損毀視為沒有任何核可 (fail-closed)"""
    try:
        with open(path or approved_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_approved(path: Optional[str], approved: Dict[str, str]) -> None:
    path = path or approved_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(approved, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
