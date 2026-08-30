"""os —— 服务器状态查询子插件

群内发送「服务器状态」即可查询：
游戏版本 / 服务器协议 / 天气 / CPU占用 / CPU核数 / 内存占用 /
系统运行时间 / BDS运行时间 / 平均TPS / 在线人数

实现说明：
- 版本、协议、TPS 直接使用 Endstone 原生 Server API（lumen.server）
- CPU / 内存 / 运行时间 使用 Python 标准库（os / time）读取 /proc，
  无第三方依赖；Windows 下自动降级为可用项
- 天气通过 querytarget 不可得，改用 gamerule/命令探测：
  在主线程执行 `weather query` 并解析输出（与 mc.getWeather 等价）
"""

import os as _os
import time as _time

lumen = None
conf = {}

DEFAULT_CONFIG = {
    "trigger": "服务器状态",
    "show_ver": True,
    "show_protocol": True,
    "show_weather": True,
    "show_cpu": True,
    "show_cpu_count": True,
    "show_ram": True,
    "show_sys_runtime": True,
    "show_bds_runtime": True,
    "show_tps": True,
    "show_online": True,
}

# 中文标签（网页配置面板显示用）
_LABELS = {
    "trigger": ("触发关键字", "群内发送该关键字时回复服务器状态"),
    "show_ver": ("显示游戏版本", ""),
    "show_protocol": ("显示协议版本", ""),
    "show_weather": ("显示服务器天气", ""),
    "show_cpu": ("显示CPU占用", ""),
    "show_cpu_count": ("显示CPU核数", ""),
    "show_ram": ("显示内存占用", ""),
    "show_sys_runtime": ("显示系统运行时间", ""),
    "show_bds_runtime": ("显示BDS运行时间", ""),
    "show_tps": ("显示平均TPS", ""),
    "show_online": ("显示在线人数", ""),
}

_bds_start_time = _time.time()  # 子插件加载时刻，近似 BDS 启动时间
_last_cpu_sample = None  # (busy, total) 上次 /proc/stat 采样


# ---------------------------------------------------------------------------
# 系统信息工具函数（替代 node 的 os / os-utils）
# ---------------------------------------------------------------------------

def _deal_mem(mem: float) -> str:
    """字节数转人类可读（复刻原版 dealMem）"""
    if mem > (1 << 30):
        return f"{mem / (1 << 30):.2f}G"
    if mem > (1 << 20):
        return f"{mem / (1 << 20):.2f}M"
    if mem > (1 << 10):
        return f"{mem / (1 << 10):.2f}KB"
    return f"{int(mem)}B"


def _read_meminfo() -> tuple[float, float]:
    """返回 (总内存字节, 可用内存字节)"""
    try:
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = parts[1].strip()

        def kb(key: str) -> float:
            return float(info.get(key, "0 kB").split()[0]) * 1024

        total = kb("MemTotal")
        avail = kb("MemAvailable") or (kb("MemFree") + kb("Buffers") + kb("Cached"))
        return total, avail
    except Exception:
        # 非 Linux 平台降级
        try:
            import psutil  # type: ignore

            vm = psutil.virtual_memory()
            return float(vm.total), float(vm.available)
        except Exception:
            return 0.0, 0.0


def _sample_cpu() -> tuple[float, float] | None:
    """读取 /proc/stat 返回 (busy, total)"""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
        fields = [float(x) for x in line.split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        total = sum(fields)
        return total - idle, total
    except Exception:
        return None


def _cpu_usage() -> float:
    """两次采样间的 CPU 占用率 0~1（复刻 osutils.cpuUsage 的周期采样思路）"""
    global _last_cpu_sample
    cur = _sample_cpu()
    if cur is None:
        return -1.0
    if _last_cpu_sample is None:
        _last_cpu_sample = cur
        # 首次调用做一次短暂即时采样
        _time.sleep(0.2)
        cur2 = _sample_cpu()
        if cur2 is None:
            return -1.0
        busy = cur2[0] - cur[0]
        total = cur2[1] - cur[1]
        _last_cpu_sample = cur2
        return busy / total if total > 0 else 0.0
    busy = cur[0] - _last_cpu_sample[0]
    total = cur[1] - _last_cpu_sample[1]
    _last_cpu_sample = cur
    return busy / total if total > 0 else 0.0


def _sys_uptime_hours() -> str:
    """系统运行小时数（复刻 osutils.sysUptime）"""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            secs = float(f.read().split()[0])
        return f"{secs / 3600:.2f}"
    except Exception:
        return "?"


def _bds_uptime_hours() -> str:
    """BDS（本进程）运行小时数（复刻 osutils.processUptime）"""
    try:
        # 优先读取本进程真实启动时间
        with open(f"/proc/{_os.getpid()}/stat", "r", encoding="utf-8") as f:
            starttime_ticks = float(f.read().split()[21])
        clk = _os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            sys_uptime = float(f.read().split()[0])
        secs = sys_uptime - starttime_ticks / clk
        return f"{secs / 3600:.2f}"
    except Exception:
        return f"{(_time.time() - _bds_start_time) / 3600:.2f}"


# ---------------------------------------------------------------------------
# 游戏侧信息（Endstone 原生 API，替代 mc.* 与 GMLIB）
# ---------------------------------------------------------------------------

def _weather() -> str:
    """查询天气（复刻原版 weather()，用 weather query 命令实现）
    注意：本函数在 WS 线程通过 runcmdEx 执行，内部已切主线程。"""
    result = lumen.mc.runcmdEx("weather query", timeout=5.0)
    output = (result.get("output") or "").lower()
    if "thunder" in output or "雷" in output:
        return "雷暴天⚡"
    if "rain" in output or "雨" in output:
        return "雨天☔"
    if "clear" in output or "晴" in output:
        return "晴天☀️"
    return "未知"


def _build_status_text() -> str:
    """构建状态文本（在 WS 线程调用；server 属性读取为只读快照，
    与原版一致的行为：读取失败的项跳过并记日志）"""
    server = lumen.server
    lines: list[str] = []

    def safe(getter, fallback="?"):
        try:
            return getter()
        except Exception:
            return fallback

    if conf.get("show_ver"):
        lines.append(f"✨游戏版本：{safe(lambda: server.minecraft_version)}")
    if conf.get("show_protocol"):
        lines.append(f"✨服务器协议：{safe(lambda: server.protocol_version)}")
    if conf.get("show_weather"):
        lines.append(f"✨服务器天气：{safe(_weather, '未知')}")
    if conf.get("show_cpu"):
        usage = _cpu_usage()
        cpu_text = f"{usage * 100:.2f}%" if usage >= 0 else "不支持"
        lines.append(f"✨CPU占用: {cpu_text}")
    if conf.get("show_cpu_count"):
        lines.append(f"✨CPU核数：{_os.cpu_count() or '?'}")
    if conf.get("show_ram"):
        total, avail = _read_meminfo()
        if total > 0:
            used = total - avail
            percent = used * 100 / total
            lines.append(f"✨内存占用: {_deal_mem(used)}/{_deal_mem(total)} {percent:.2f}%")
        else:
            lines.append("✨内存占用: 不支持")
    if conf.get("show_sys_runtime"):
        lines.append(f"✨系统已运行：{_sys_uptime_hours()}小时")
    if conf.get("show_bds_runtime"):
        lines.append(f"✨BDS已运行：{_bds_uptime_hours()}小时")
    if conf.get("show_tps"):
        lines.append(f"✨平均TPS: {safe(lambda: f'{server.average_tps:.2f}')}")
    if conf.get("show_online"):
        lines.append(f"✨在线{len(lumen.mc.online_players)}人")

    return "\n".join(lines) if lines else "（所有显示项均已关闭）"


# ---------------------------------------------------------------------------
# 生命周期与事件
# ---------------------------------------------------------------------------

def on_load(ctx):
    global lumen, conf
    lumen = ctx

    # 1. 初始化配置（自动补全缺失项，复刻 configFile.initFile 行为）
    conf = lumen.storage.read("config.json", DEFAULT_CONFIG)
    changed = False
    for key, val in DEFAULT_CONFIG.items():
        if key not in conf:
            conf[key] = val
            changed = True
    if changed:
        lumen.storage.write("config.json", conf)

    # 2. 注册网页配置面板
    builder = lumen.web.createConfig("os")
    for key, default in DEFAULT_CONFIG.items():
        label, desc = _LABELS.get(key, (key, ""))
        val = conf.get(key, default)
        if isinstance(default, bool):
            builder.switch(key, val, desc, label)
        else:
            builder.text(key, val, desc, label)
    builder.register()

    # 3. 监听网页配置更新
    lumen.on("config.update.os", on_config_update)

    # 4. 监听群消息
    lumen.on("message.group.normal", on_group_message)

    lumen.logger.info("os 服务器状态子插件已加载（发送「服务器状态」查询）")


def on_unload(ctx):
    ctx.logger.info("os 服务器状态子插件已卸载")


def on_config_update(key, value):
    """网页面板保存配置时同步到本地文件"""
    conf[key] = value
    lumen.storage.write("config.json", conf)


def on_group_message(pack, reply):
    if pack.get("group_id") != lumen.env.get("main_group"):
        return
    if pack.get("raw_message", "").strip() != conf.get("trigger", "服务器状态"):
        return
    try:
        reply(_build_status_text())
    except Exception as exc:  # 与原版一致：出错不崩溃
        lumen.logger.error(f"服务器状态查询失败: {exc}")
        reply("服务器状态查询失败，请查看后台日志")
