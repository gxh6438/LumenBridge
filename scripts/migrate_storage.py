#!/usr/bin/env python3
"""LumenBridge 存储结构迁移脚本（旧版平铺布局 → v1.1.0 目录化布局）。

用法：
  1. 完全关闭 Minecraft 服务器（迁移期间不可运行插件）
  2. 把本脚本复制到插件数据目录 plugins/lumenbridge/ 下
  3. 在该目录运行：
       python migrate_storage.py          # 执行迁移
       python migrate_storage.py --dry    # 仅预览将要做的改动
  4. 迁移完成后可删除本脚本；旧文件统一备份在 legacy_backup/ 内

迁移内容：
  A. connections.json（单文件混合存储所有适配器卡片）
       → connections/websocket.json + connections/qqofficial.json
       + connections/astrbot.json（按适配器类型分文件）
       官方机器人卡片同时剔除 WebSocket 专属字段
       （ws_type/target/listen_host/listen_port/access_token），
       并把后台静默日志开关升级为默认开启（旧 false → 新 true）。
  B. 运行数据平铺在插件根目录的文件
       rules.json / whitelist.json / whitelist_official.json /
       framework_update.json / command_palette.json
       → 统一移入 data/ 目录。
       白名单文件按 qid+xbox 去重合并（保留迁移前后的全部绑定）；
       其余文件仅在 data/ 下为空/缺失时迁入，已有内容则保留并提示。

特性：纯标准库实现、幂等（重复运行无副作用）、迁移前自动备份。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# 脚本应放在插件数据目录（plugins/lumenbridge/）内运行：以脚本所在目录定位
BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / "legacy_backup"

ADAPTER_FILES = {
    "websocket": "websocket.json",
    "qqofficial": "qqofficial.json",
    "astrbot": "astrbot.json",
}
# WebSocket 专属字段：官方机器人卡片不含（官方域走 AppID/Secret 网关鉴权）
WS_ONLY_FIELDS = ("ws_type", "target", "listen_host", "listen_port", "access_token")
# 运行数据文件：从插件根目录移入 data/
DATA_FILES = (
    "rules.json",
    "whitelist.json",
    "whitelist_official.json",
    "framework_update.json",
    "command_palette.json",
)

DRY = "--dry" in sys.argv


def _fmt(text: str) -> str:
    """Windows 控制台 GBK 兼容输出（不可编码字符降级替换）。"""
    try:
        return text.encode(sys.stdout.encoding or "utf-8", "replace").decode(
            sys.stdout.encoding or "utf-8", "replace"
        )
    except (LookupError, AttributeError):
        return text


def say(message: str) -> None:
    print(_fmt(message))


def read_json(path: Path):
    """读取 JSON 文件；缺失/损坏返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_json(path: Path, payload) -> None:
    """原子写 JSON（tmp + os.replace），避免中断留下半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    os.replace(tmp, path)


def is_empty_data(payload) -> bool:
    """数据文件是否视为“空”（空列表 / 空对象 / None）。"""
    return payload is None or payload == [] or payload == {}


def backup(path: Path) -> bool:
    """把旧文件复制进 legacy_backup/（保留现场，绝不删除数据）。"""
    if not path.is_file():
        return False
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / path.name
    # 同名冲突时追加序号，避免多次迁移互相覆盖备份
    seq = 1
    while target.exists():
        target = BACKUP_DIR / f"{path.stem}_{seq}{path.suffix}"
        seq += 1
    shutil.copy2(path, target)
    return True


def migrate_connections() -> bool:
    """A. connections.json → connections/ 按类型分文件。"""
    src = BASE / "connections.json"
    conn_dir = BASE / "connections"
    if not src.is_file():
        # 已是目录化布局或全新安装：无旧文件可迁
        return False
    payload = read_json(src)
    adapters = payload.get("adapters") if isinstance(payload, dict) else None
    if not isinstance(adapters, list) or not adapters:
        say("  [跳过] connections.json 存在但无适配器卡片，仅备份不迁移")
        if not DRY:
            backup(src)
        return False

    groups: dict[str, list] = {t: [] for t in ADAPTER_FILES}
    cleaned = 0
    promoted = 0
    for item in adapters:
        if not isinstance(item, dict):
            continue
        atype = str(item.get("type") or "")
        if atype not in ADAPTER_FILES:
            say(f"  [警告] 跳过未知类型的适配器卡片：{item.get('name') or atype}")
            continue
        card = dict(item)
        if atype == "qqofficial":
            # 剔除 WebSocket 专属字段（官方机器人卡片不应持有）
            removed = [k for k in WS_ONLY_FIELDS if k in card]
            if removed:
                cleaned += 1
                for key in removed:
                    card.pop(key, None)
            # 旧版默认 false（打印运行日志）升级为新版默认 true（后台静默）
            if card.get("suppress_connection_log") is False:
                card["suppress_connection_log"] = True
                promoted += 1
        groups[atype].append(card)

    if DRY:
        say(f"  [预览] 将按类型写入 connections/："
            f"websocket={len(groups['websocket'])} 张、"
            f"qqofficial={len(groups['qqofficial'])} 张（其中 {cleaned} 张剔除 ws 字段、"
            f"{promoted} 张静默日志开关升级为开启）、"
            f"astrbot={len(groups['astrbot'])} 张")
        return True

    conn_dir.mkdir(parents=True, exist_ok=True)
    for atype, fname in ADAPTER_FILES.items():
        write_json(conn_dir / fname, {"version": 1, "adapters": groups[atype]})
    backup(src)
    src.unlink()
    say(f"  [完成] 适配器卡片已分文件写入 connections/（websocket={len(groups['websocket'])}、"
        f"qqofficial={len(groups['qqofficial'])}、astrbot={len(groups['astrbot'])}）")
    if cleaned:
        say(f"  [完成] {cleaned} 张官方机器人卡片已剔除 WebSocket 专属字段")
    if promoted:
        say(f"  [完成] {promoted} 张官方机器人卡片的后台静默日志开关已升级为开启")
    say("  [备份] 旧 connections.json 已存入 legacy_backup/ 并从原位置移除")
    return True


def merge_whitelist(old: list, new: list) -> tuple[list, int]:
    """按 qid+xbox 去重合并两份白名单绑定，返回 (合并结果, 新增条数)。"""
    seen: set[tuple[str, str]] = set()
    merged: list = []
    for item in old + new:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("qid", "")).strip()
        xbox = str(item.get("xbox", "")).strip().strip('"')
        if not qid or not xbox:
            continue
        key = (qid, xbox.casefold())
        if key in seen:
            continue
        seen.add(key)
        merged.append({"qid": qid, "xbox": xbox})
    return merged, len(merged) - len([i for i in old if isinstance(i, dict)])


def migrate_data_files() -> None:
    """B. 根目录平铺的数据文件 → data/ 目录。"""
    data_dir = BASE / "data"
    for name in DATA_FILES:
        src = BASE / name
        dst = data_dir / name
        if not src.is_file():
            continue
        old_payload = read_json(src)
        if old_payload is None:
            say(f"  [跳过] {name} 无法解析（损坏），仅备份不迁移")
            if not DRY:
                backup(src)
            continue
        new_payload = read_json(dst) if dst.is_file() else None

        if name.startswith("whitelist"):
            # 白名单是用户积累的绑定数据：新旧合并去重，一条都不丢
            old_list = old_payload if isinstance(old_payload, list) else []
            new_list = new_payload if isinstance(new_payload, list) else []
            if new_list:
                merged, added = merge_whitelist(old_list, new_list)
                if DRY:
                    say(f"  [预览] {name} 将与 data/ 下已有记录合并（新增 {max(added, 0)} 条旧绑定）")
                    continue
                write_json(dst, merged)
                backup(src)
                src.unlink()
                say(f"  [完成] {name} 已合并迁入 data/（合并后 {len(merged)} 条绑定）")
            else:
                if DRY:
                    say(f"  [预览] {name} 将移入 data/")
                    continue
                data_dir.mkdir(parents=True, exist_ok=True)
                write_json(dst, old_list)
                backup(src)
                src.unlink()
                say(f"  [完成] {name} 已移入 data/（{len(old_list)} 条绑定）")
            continue

        # 其余数据文件：data/ 下为空/缺失才迁入；已有内容则保留新的
        if is_empty_data(new_payload):
            if DRY:
                say(f"  [预览] {name} 将移入 data/")
                continue
            data_dir.mkdir(parents=True, exist_ok=True)
            write_json(dst, old_payload)
            backup(src)
            src.unlink()
            say(f"  [完成] {name} 已移入 data/")
        else:
            say(f"  [保留] data/{name} 已有内容，旧 {name} 仅备份不覆盖"
                f"（如需以旧文件为准请手动替换）")
            if not DRY:
                backup(src)


def main() -> int:
    # Windows 控制台默认 GBK：强制 UTF-8 输出避免中文乱码/UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    mode = "预览模式（--dry，不修改任何文件）" if DRY else "正式迁移"
    say("=" * 62)
    say(f"LumenBridge 存储结构迁移脚本  [{mode}]")
    say(f"目标目录：{BASE}")
    say("=" * 62)
    if BASE.name != "lumenbridge":
        say("[警告] 目录名不是 lumenbridge——请确认脚本确实放在插件数据目录内")
    if not DRY:
        say("提示：请确保 Minecraft 服务器已完全关闭后再执行迁移")
        say("")

    migrated_conn = migrate_connections()
    if not migrated_conn:
        say("  [跳过] 未发现旧版 connections.json（可能已迁移或为全新安装）")

    say("")
    migrate_data_files()

    say("")
    say("=" * 62)
    if DRY:
        say("预览结束：以上为将要执行的改动。去掉 --dry 参数重新运行以执行迁移")
    else:
        say("迁移完成。迁移后的布局：")
        say("  config.json            主配置")
        say("  connections/           适配器卡片（websocket / qqofficial / astrbot）")
        say("  data/                  运行数据（规则 / 白名单 / 更新回执等）")
        say("  legacy_backup/         迁移前的旧文件备份")
        say("确认插件工作正常后，可删除本脚本与 legacy_backup/ 目录")
    say("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
