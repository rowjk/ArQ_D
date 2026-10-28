"""
雙 RD 技能庫同步工具

使用者規則：Claude Code 與 Antigravity 的 Agent Skills 必須隨時一致。新增 / 更新技能後執行本腳本即可同步到全部技能庫。

    uv run python scripts/sync_skills.py           # 只檢查，列出差異 (有差異時 exit code 1)
    uv run python scripts/sync_skills.py --apply   # 同步：缺少的補上，內容不同的以「最近修改」的版本覆蓋

同步規則：
    - 取所有技能庫的聯集：任一庫有、其他庫沒有的技能會被複製過去 (不會刪除任何技能)。
    - 同名技能內容不同時，以最近修改 (資料夾內檔案最新 mtime) 的版本為準覆蓋其他庫。
    - 只想移除某個技能時，請手動從「所有」技能庫刪除，否則下次同步會被補回來。
"""

import argparse
import hashlib
import os
import shutil
import sys
from typing import Dict, List, Tuple

HOME = os.path.expanduser("~")
DEFAULT_SKILL_DIRS = [
    os.path.join(HOME, ".claude", "skills"),                    # Claude Code
    os.path.join(HOME, ".gemini", "antigravity", "skills"),      # Antigravity
    os.path.join(HOME, ".gemini", "antigravity-cli", "skills"),  # Antigravity CLI (agy)
]


def _skill_info(path: str) -> Tuple[str, float]:
    """技能資料夾的 (內容雜湊, 最新修改時間)"""
    digest = hashlib.sha256()
    # 只看「檔案」的修改時間：資料夾的 mtime 在建立 / 複製時就會變新，不能代表內容新舊
    latest = 0.0
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            digest.update(os.path.relpath(full, path).replace("\\", "/").encode("utf-8"))
            with open(full, "rb") as f:
                digest.update(f.read())
            latest = max(latest, os.path.getmtime(full))
    return digest.hexdigest(), latest


def _list_skills(skill_dir: str) -> Dict[str, Tuple[str, float]]:
    if not os.path.isdir(skill_dir):
        return {}
    return {
        name: _skill_info(os.path.join(skill_dir, name))
        for name in sorted(os.listdir(skill_dir))
        if os.path.isdir(os.path.join(skill_dir, name))
    }


def plan_sync(skill_dirs: List[str]) -> List[Tuple[str, str, str, str]]:
    """
    計算需要的同步動作，回傳 [(動作, 技能名稱, 來源庫, 目標庫)]；動作為 "add" (補上缺少的) 或 "update" (覆蓋舊版)。
    """
    inventory = {d: _list_skills(d) for d in skill_dirs}
    names = sorted({name for skills in inventory.values() for name in skills})
    actions = []
    for name in names:
        holders = [(d, inventory[d][name]) for d in skill_dirs if name in inventory[d]]
        source, (source_hash, _) = max(holders, key=lambda h: h[1][1])  # 最近修改的版本為準
        for d in skill_dirs:
            if name not in inventory[d]:
                actions.append(("add", name, source, d))
            elif inventory[d][name][0] != source_hash:
                actions.append(("update", name, source, d))
    return actions


def apply_sync(actions: List[Tuple[str, str, str, str]]) -> None:
    for action, name, source, target in actions:
        os.makedirs(target, exist_ok=True)
        dest = os.path.join(target, name)
        if action == "update":
            shutil.rmtree(dest)
        shutil.copytree(os.path.join(source, name), dest)


def main(argv: List[str] = None) -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="同步 Claude Code 與 Antigravity 的 Agent Skills")
    parser.add_argument("--apply", action="store_true", help="實際同步 (預設只檢查)")
    parser.add_argument("--dirs", nargs="+", default=DEFAULT_SKILL_DIRS, help="技能庫路徑 (預設為三個全域技能庫)")
    args = parser.parse_args(argv)

    actions = plan_sync(args.dirs)
    for d in args.dirs:
        print(f"• {d}：{len(_list_skills(d))} 個技能")
    if not actions:
        print("✅ 所有技能庫已同步。")
        return 0

    label = {"add": "補上", "update": "更新"}
    for action, name, source, target in actions:
        print(f"  {label[action]} {name}：{source} → {target}")
    if not args.apply:
        print(f"⚠️ 共 {len(actions)} 項差異。執行 `--apply` 進行同步。")
        return 1
    apply_sync(actions)
    print(f"✅ 已完成 {len(actions)} 項同步。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
