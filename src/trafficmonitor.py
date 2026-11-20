#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
TrafficMonitor for Linux —— 原生 Qt5 悬浮窗实现。

上游 zhongyang219/TrafficMonitor 是 Windows MFC 工程（105 个 .cpp 中 112 个
头文件 include <afxwin.h>），MFC 是微软专有框架，GCC/MinGW 无法编译，
因此本文件是按其功能语义重写的原生实现，不依赖 Wine。

数据全部来自内核伪文件，无外部依赖：
  网速  /proc/net/dev      (排除 lo，聚合所有物理/虚拟接口)
  CPU   /proc/stat         (两次采样求 busy 占比)
  内存  /proc/meminfo      (MemTotal - MemAvailable)
  温度  /sys/class/hwmon   (CPU / 主板 / 内存 / 硬盘，见下方“温度采集”)
  功耗  /sys/class/powercap + hwmon power*_input (CPU 走 RAPL，其余看硬件)

用法:
  trafficmonitor              # 启动悬浮窗
  trafficmonitor --version
  trafficmonitor --reset-pos  # 位置异常时重置到右上角
  trafficmonitor --dump-sensors  # 只读：打印温度/功率传感器与八项解析结果
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

APP_NAME = "TrafficMonitor"
APP_VERSION = "1.86.0+linux10"
IPC_SOCKET = "trafficmonitor-%s" % os.environ.get("USER", "user")

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "trafficmonitor-linux",
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
AUTOSTART_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "autostart",
    "trafficmonitor.desktop",
)

DEFAULTS = {
    "x": None,             # None = 首次启动自动放到右上角
    "y": None,
    "show_up": True,
    "show_down": True,
    "show_cpu": True,
    "show_mem": True,
    # 四项硬件温度：CPU / 主板 / 内存 / 硬盘。默认全开，
    # 没有传感器的项显示 "--"（原因写进窗口 tooltip），不拿别的读数顶替。
    "show_temp_cpu": True,
    "show_temp_board": True,
    "show_temp_mem": True,
    "show_temp_disk": True,
    # 四项功耗：CPU 走 RAPL，其余部件只有硬件真的暴露功率通道时才有数
    "show_power_cpu": True,
    "show_power_board": True,
    "show_power_mem": True,
    "show_power_disk": True,
    # 布局：0 = 自动（全部放一行）；N>0 = 每行最多 N 个指标，超出自动换行
    "max_per_row": 0,
    # 可选覆盖：{"cpu": "k10temp:Tctl", "board": "nct6683:SYSTIN"}
    # 键=驱动名(hwmon 的 name)，值=该驱动下的标签；写的传感器不存在时忽略。
    "temp_override": {},
    "always_on_top": True,
    "opacity": 0.85,       # 背景不透明度 0.2 ~ 1.0
    "font_size": 10,
    "refresh_ms": 1000,    # 采样周期，与原版默认 1 秒一致
}

PAD_X = 10            # 左右内边距
PAD_Y = 3             # 上下内边距
GAP = 12              # 各显示项之间的间距
ROW_GAP = 3           # 多行之间的行距
RADIUS = 6            # 圆角半径

COLOR_UP = QColor(120, 220, 140)      # 上行（绿）
COLOR_DOWN = QColor(120, 190, 255)    # 下行（蓝）
COLOR_CPU = QColor(255, 205, 110)     # CPU（橙）
COLOR_MEM = QColor(230, 150, 235)     # 内存（紫）

# 温度用另一组色，避免与“占用率”混淆
COLOR_T_CPU = QColor(255, 138, 101)   # CPU 温度
COLOR_T_BOARD = QColor(150, 210, 190) # 主板温度
COLOR_T_MEM = QColor(255, 175, 205)   # 内存温度
COLOR_T_DISK = QColor(140, 220, 255)  # 硬盘温度

# 功耗再单独一组（比温度稍暗，单位 W 本身就能区分）
COLOR_W_CPU = QColor(245, 185, 95)    # CPU 功耗
COLOR_W_BOARD = QColor(125, 195, 165) # 主板功耗
COLOR_W_MEM = QColor(235, 150, 195)   # 内存功耗
COLOR_W_DISK = QColor(110, 195, 235)  # 硬盘功耗


# ---------------------------------------------------------------- 数据采集

def _read_proc(path):
    """读内核伪文件；失败返回空串，调用方据此跳过刷新而不是崩掉。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def read_net_counters():
    """返回 (累计接收字节, 累计发送字节)，跳过回环接口。"""
    rx = tx = 0
    for line in _read_proc("/proc/net/dev").splitlines()[2:]:
        name, sep, rest = line.partition(":")
        if not sep or name.strip() == "lo":
            continue
        fields = rest.split()
        # 布局: rx_bytes ... tx_bytes 位于第 9 列
        if len(fields) < 9:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
    return rx, tx


def read_cpu_times():
    """返回 (idle_jiffies, total_jiffies)。"""
    fields = _read_proc("/proc/stat").split("\n", 1)[0].split()[1:]
    try:
        vals = [int(v) for v in fields]
    except ValueError:
        return 0, 0
    if len(vals) < 4:
        return 0, 0
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return idle, sum(vals)


def read_mem_percent():
    total = available = 0
    for line in _read_proc("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        try:
            value = int(rest.split()[0])  # 单位 kB
        except (IndexError, ValueError):
            continue
        if key == "MemTotal":
            total = value
        elif key == "MemAvailable":
            available = value
    if total <= 0:
        return 0.0
    return (total - available) * 100.0 / total


# ---------------------------------------------------------------- 温度采集
#
# 全部读内核 sysfs，不调外部命令、不做任何估算：
#   CPU    k10temp(Tctl/Tdie) / coretemp(Package id 0) / cpu_thermal
#   主板   Super I/O: nct6683 / nct6775 / nct6798 / it87 / w83627ehf ...
#          （迷你主机多数把温度接在 EC 上，内核看不到，就如实显示 "--"）
#   内存   spd5118（DDR5 SPD Hub）/ jc42（板载温度芯片）
#   硬盘   nvme(Composite) / drivetemp(SATA，需要 modprobe drivetemp)
# 找不到对应传感器就返回 None，绝不拿 CPU/VRM 之类的别的读数顶替。

HWMON_ROOT = "/sys/class/hwmon"

TEMP_ROLES = ("cpu", "board", "mem", "disk")
TEMP_NAMES = {"cpu": "CPU", "board": "主板", "mem": "内存", "disk": "硬盘"}
TEMP_COLORS = {
    "cpu": COLOR_T_CPU,
    "board": COLOR_T_BOARD,
    "mem": COLOR_T_MEM,
    "disk": COLOR_T_DISK,
}

# 顺序即优先级；命中第一条候选就停，同一角色不混用多块芯片
TEMP_DRIVERS = {
    "cpu": ("k10temp", "coretemp", "zenpower", "cpu_thermal", "soc_thermal"),
    "board": (
        "nct6683", "nct6686", "nct6687", "nct6775", "nct6776", "nct6779",
        "nct6791", "nct6792", "nct6793", "nct6794", "nct6795", "nct6796",
        "nct6797", "nct6798", "it87", "f71882fg", "f71889a", "f71869",
        "w83627ehf", "w83627dhg", "w83667hg", "asus", "dell_smm", "hp",
    ),
    "mem": ("spd5118", "jc42", "dimm"),
    "disk": ("nvme", "drivetemp", "scsi", "sas", "megaraid"),
}

# 主板只认这些标签：把 PECI CPU / VRM 当“主板温度”等于报假数
TEMP_LABEL_PREF = {
    "cpu": ("Tctl", "Tdie", "Package id 0", "CPU"),
    "board": ("SYSTIN", "System", "Motherboard", "Mainboard", "Chipset", "Board"),
    "disk": ("Composite",),
}

TEMP_MISSING_NOTE = {
    "cpu": "内核没有暴露 CPU 温度传感器",
    "board": "本机没有可读的主板温度芯片（迷你主机常见的 EC 方案，内核看不到）",
    "mem": "内存条没有温度传感器（DDR4 SO-DIMM 普遍没有；DDR5 由 spd5118 提供）",
    "disk": "没有可读的磁盘温度（NVMe 走 nvme；SATA 需加载 drivetemp 模块）",
}

_SENSOR_CACHE = {"ts": None, "entries": []}
_SENSOR_RESCAN_S = 5.0   # 重新扫描 hwmon 的间隔：后加载的模块也能自动出现


def _match_driver(name, driver):
    return name.lower().startswith(driver.lower())


def _scan_hwmon(force=False):
    """扫描 /sys/class/hwmon，返回 [{'name','path','temps':[...]}]；结果缓存 5 秒。"""
    now = time.monotonic()
    if (not force and _SENSOR_CACHE["ts"] is not None
            and now - _SENSOR_CACHE["ts"] < _SENSOR_RESCAN_S):
        return _SENSOR_CACHE["entries"]

    entries = []
    for path in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
        name = _read_proc(os.path.join(path, "name")).strip()
        if not name:
            continue
        temps = []
        for tpath in sorted(glob.glob(os.path.join(path, "temp*_input"))):
            try:
                milli = int(_read_proc(tpath).strip())
            except ValueError:
                continue
            value = milli / 1000.0
            # 0、负值、>150℃ 都是坏读数（未接线/驱动未刷新），不要显示
            if not 1.0 <= value <= 150.0:
                continue
            base = tpath[:-len("_input")]
            temps.append({
                "label": _read_proc(base + "_label").strip(),
                "value": value,
                "path": tpath,
            })
        powers = []
        seen_channels = set()
        for pattern, suffix in (("power*_input", "_input"), ("power*_average", "_average")):
            for ppath in sorted(glob.glob(os.path.join(path, pattern))):
                channel = os.path.basename(ppath)[:-len(suffix)]
                if channel in seen_channels:
                    continue  # 同一通道的 input / average 只取一个
                try:
                    micro_watt = int(_read_proc(ppath).strip())
                except ValueError:
                    continue
                watts = micro_watt / 1e6
                if not 0.0 < watts <= 2000.0:  # 0 / >2kW 都是坏数据
                    continue
                seen_channels.add(channel)
                powers.append({
                    "label": _read_proc(os.path.join(path, channel + "_label")).strip(),
                    "watts": watts,
                    "path": ppath,
                })

        if temps or powers:
            entries.append({"name": name, "path": path, "temps": temps, "powers": powers})

    _SENSOR_CACHE["entries"] = entries
    _SENSOR_CACHE["ts"] = now
    return entries


def _temp_name(item):
    """标签为空时用 temp1 这种文件名，tooltip 里能指到具体通道。"""
    return item["label"] or os.path.basename(item["path"])[:-len("_input")]


def _select_temp(role, entries, override=None):
    """按角色挑一个传感器，返回 (值或 None, 来源文本, 候选明细)。"""
    if override:
        want_name, _, want_label = str(override).partition(":")
        candidates = []
        for entry in entries:
            if want_name and not _match_driver(entry["name"], want_name):
                continue
            for item in entry["temps"]:
                if want_label and item["label"].lower() != want_label.lower():
                    continue
                candidates.append((entry, item))
        if not candidates:
            return None, "", "配置覆盖 %s 不存在" % override
        entry, item = max(candidates, key=lambda pair: pair[1]["value"])
        return (item["value"],
                "%s:%s" % (entry["name"], _temp_name(item)),
                "")

    candidates = []
    for driver in TEMP_DRIVERS[role]:
        for entry in entries:
            if _match_driver(entry["name"], driver):
                for item in entry["temps"]:
                    candidates.append((entry, item))
    if not candidates:
        return None, "", ""

    detail = " ".join(
        "%s:%s=%.1f℃" % (entry["name"], _temp_name(item), item["value"])
        for entry, item in candidates
    )

    if role == "board":
        # 面板上的“主板温度”只认 SYSTIN/System 这类标签：
        # 拿 PECI CPU 或 VRM 顶替等于报假数，宁可显示 "--"。
        for label in TEMP_LABEL_PREF["board"]:
            for entry, item in candidates:
                if item["label"].lower() == label.lower():
                    return (item["value"],
                            "%s:%s" % (entry["name"], item["label"]),
                            detail)
        return None, "", detail

    # 每颗芯片先选自己的代表温度（有 Composite / Package id 0 就用它），
    # 再多颗里取最热的一颗 —— 双盘时不会因为 NVMe 凉就漏掉更烫的 SATA。
    by_chip = {}
    for entry, item in candidates:
        by_chip.setdefault(entry["path"], (entry, []))[1].append(item)
    picks = []
    for entry, temps in by_chip.values():
        chosen = None
        for label in TEMP_LABEL_PREF.get(role, ()):
            for item in temps:
                if item["label"].lower() == label.lower():
                    chosen = item
                    break
            if chosen is not None:
                break
        if chosen is None:
            chosen = max(temps, key=lambda t: t["value"])
        picks.append((entry, chosen))

    entry, item = max(picks, key=lambda pair: pair[1]["value"])
    return (item["value"],
            "%s:%s" % (entry["name"], _temp_name(item)),
            detail)


def read_temps(cfg=None):
    """返回 {role: {'value','source','detail','note'}}；读不到就是 value=None。"""
    cfg = cfg or {}
    entries = _scan_hwmon()
    overrides = cfg.get("temp_override") or {}
    result = {}
    for role in TEMP_ROLES:
        value, source, detail = _select_temp(role, entries, overrides.get(role))
        result[role] = {
            "value": value,
            "source": source,
            "detail": detail,
            "note": "" if value is not None else (detail or TEMP_MISSING_NOTE[role]),
        }
    return result


def fmt_temp(reading):
    """整数显示；没有读数时给 '--'，不写 0℃（0 看起来像真数据）。"""
    if not reading or reading.get("value") is None:
        return "--"
    return "%d℃" % int(round(reading["value"]))


# ---------------------------------------------------------------- 功耗采集
#
# CPU 功耗走内核 RAPL：/sys/class/powercap/*/energy_uj 是累计微焦耳，
# 两次采样差分除以时间就是瓦。注意该文件默认 0400 root-only，
# 安装包会写一条 udev 规则（energy_uj → 0444），否则这里读不到、显示 "--"。
#
# 主板 / 内存 / 硬盘：只有硬件真的暴露 power*_input（微瓦）时才有数。
# 消费级主板的 Super I/O 一般只有电压/温度/风扇，没有功率；
# 内存条要服务器 PMBus/VRM 才有；NVMe 的 hwmon 普遍不报功率。
# 读不到就显示 "--"，绝不按整机功率摊派或估算。

POWERCAP_ROOT = "/sys/class/powercap"
POWER_ROLES = TEMP_ROLES
POWER_COLORS = {
    "cpu": COLOR_W_CPU,
    "board": COLOR_W_BOARD,
    "mem": COLOR_W_MEM,
    "disk": COLOR_W_DISK,
}

POWER_DRIVERS = {
    "cpu": ("zenpower", "amd_energy", "cpu_power"),
    "board": ("nct6683", "nct6686", "nct6687", "it87", "pmbus", "psu"),
    "mem": ("pmbus", "dimm", "vrm"),
    "disk": ("nvme", "drivetemp", "scsi"),
}

POWER_MISSING_NOTE = {
    "cpu": "RAPL energy_uj 不可读（需要安装包配置的 udev 读权限规则）",
    "board": "主板没有暴露功率通道（Super I/O/EC 不上报功率）",
    "mem": "内存条没有功率传感器（消费级 DDR 没有 PMBus）",
    "disk": "硬盘没有暴露功率通道",
}

_POWER_STATE = {"ts": None, "energy_uj": {}}


def _read_int(path):
    try:
        return int(_read_proc(path).strip())
    except ValueError:
        return None


def _rapl_package_watts(now):
    """返回 (总瓦数或 None, 是否有可读计数器, 明细文本)。

    多路 CPU 时把各 package 的差分相加；计数器回绕按 max_energy_range_uj 补。
    第一次采样没有上一帧，返回 None（下一帧就有数）。
    """
    zones = []
    for path in sorted(glob.glob(os.path.join(POWERCAP_ROOT, "intel-rapl:*"))):
        name = _read_proc(os.path.join(path, "name")).strip()
        if not name.startswith("package"):
            continue
        value = _read_int(os.path.join(path, "energy_uj"))
        if value is None:
            continue
        zones.append((name, path, value))

    if not zones:
        return None, False, ""

    prev_ts = _POWER_STATE["ts"]
    prev_map = _POWER_STATE["energy_uj"]
    total = 0.0
    detail = []
    for name, path, value in zones:
        prev = prev_map.get(path)
        if prev is not None and prev_ts is not None and now > prev_ts:
            delta = value - prev
            if delta < 0:  # 计数器回绕
                max_range = _read_int(os.path.join(path, "max_energy_range_uj"))
                if max_range:
                    delta += max_range
            if delta > 0:
                watts = delta / 1e6 / (now - prev_ts)
                total += watts
                detail.append("%s=%.1fW" % (name, watts))

    _POWER_STATE["ts"] = now
    _POWER_STATE["energy_uj"] = {path: value for _, path, value in zones}
    if total <= 0.0:
        return None, True, ""
    return total, True, "RAPL " + " ".join(detail)


def _select_power(role, entries, now):
    """按角色挑一个功率读数，返回 (瓦数或 None, 来源, 明细/原因)。"""
    rapl_usable = role == "cpu"
    if rapl_usable:
        watts, readable, rapl_detail = _rapl_package_watts(now)
        if watts is not None:
            return watts, "RAPL package", rapl_detail

    candidates = []
    for driver in POWER_DRIVERS[role]:
        for entry in entries:
            if _match_driver(entry["name"], driver):
                for item in entry["powers"]:
                    candidates.append((entry, item))
    if not candidates:
        if rapl_usable and readable:
            return None, "", "RAPL 首次采样中"
        return None, "", POWER_MISSING_NOTE[role]

    detail = " ".join(
        "%s:%s=%.1fW" % (entry["name"], _temp_name(item), item["watts"])
        for entry, item in candidates
    )
    entry, item = max(candidates, key=lambda pair: pair[1]["watts"])
    return item["watts"], "%s:%s" % (entry["name"], _temp_name(item)), detail


def read_powers(cfg=None, now=None):
    """返回 {role: {'value','source','detail','note'}}；value 单位瓦。"""
    cfg = cfg or {}
    now = now or time.monotonic()
    entries = _scan_hwmon()
    result = {}
    for role in POWER_ROLES:
        value, source, detail = _select_power(role, entries, now)
        result[role] = {
            "value": value,
            "source": source,
            "detail": detail,
            "note": "" if value is not None else (detail or POWER_MISSING_NOTE[role]),
        }
    return result


def fmt_power(reading):
    """大功率整数、小功率一位小数；没有读数给 '--'。"""
    if not reading or reading.get("value") is None:
        return "--"
    value = float(reading["value"])
    if value >= 100.0:
        return "%dW" % int(round(value))
    return "%.1fW" % value


def fmt_rate(bytes_per_sec):
    """按 1024 进制带上单位，保留 2 位小数（与原版显示习惯一致）。"""
    value = float(max(0.0, bytes_per_sec))
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024.0 or unit == "GB/s":
            if unit == "B/s":
                return "%d %s" % (int(value), unit)
            return "%.2f %s" % (value, unit)
        value /= 1024.0
    return "%.2f GB/s" % value


def fmt_short(bytes_per_sec):
    """托盘图标只有几十像素，用 1.2M / 340K 这种极短写法。"""
    value = float(max(0.0, bytes_per_sec))
    for unit in ("B", "K", "M", "G"):
        if value < 1024.0 or unit == "G":
            if unit == "B":
                return "%d%s" % (int(value), unit)
            return "%.1f%s" % (value, unit)
        value /= 1024.0
    return "%.1fG" % value


# ---------------------------------------------------------------- 配置读写

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            for key in DEFAULTS:
                if key in saved:
                    cfg[key] = saved[key]
    except (OSError, ValueError):
        pass  # 配置缺失或损坏都走默认值，不让用户卡在启动失败
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)  # 原子写，避免半截配置文件
    except OSError as exc:
        print("警告: 配置保存失败: %s" % exc, file=sys.stderr)


def autostart_enabled():
    return os.path.exists(AUTOSTART_PATH)


def set_autostart(enabled):
    try:
        if enabled:
            os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
            with open(AUTOSTART_PATH, "w", encoding="utf-8") as fh:
                fh.write(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=TrafficMonitor\n"
                    "Comment=网速 CPU 内存悬浮窗\n"
                    "Exec=trafficmonitor\n"
                    "Icon=trafficmonitor\n"
                    "Terminal=false\n"
                    "X-GNOME-Autostart-enable=true\n"
                )
        elif os.path.exists(AUTOSTART_PATH):
            os.remove(AUTOSTART_PATH)
    except OSError as exc:
        print("警告: 开机自启设置失败: %s" % exc, file=sys.stderr)


# ---------------------------------------------------------------- 悬浮窗

class MonitorWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()

        # 用无边框窗口保证渲染紧凑（普通窗口会被 KWin 预留 16px 装饰区，
        # 出现一条去不掉的黑边）。代价是 KWin 会给窗口附
        # _KDE_NET_WM_WINDOW_TYPE_OVERRIDE，任务栏不生成按钮——Deepin 里
        # “出现在任务栏”改用把程序固定到停靠栏实现（见 README）。
        flags = Qt.FramelessWindowHint
        if self.cfg["always_on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setCursor(Qt.OpenHandCursor)

        self._drag_offset = None       # 拖动起点（相对窗口左上角）
        self._segments = []            # [(文本, 颜色)] 每帧重算
        self._rows = []                # 按 max_per_row 切好行的 segments
        self._last_rx, self._last_tx = read_net_counters()
        self._last_idle, self._last_total = read_cpu_times()
        self._last_ts = time.monotonic()
        self._cpu_pct = 0.0
        self._mem_pct = read_mem_percent()
        self._temps = read_temps(self.cfg)
        self._powers = read_powers(self.cfg)
        self._tray = None
        self._tray_menu = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(max(200, int(self.cfg["refresh_ms"])))

        self.resize(200, 28)
        self.restore_position()
        self._setup_tray()
        self.refresh()

    # -- 系统托盘 ------------------------------------------------------
    def _setup_tray(self):
        """托盘图标显示实时速度；没有系统托盘时静默跳过。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("提示: 当前桌面没有系统托盘，仅使用悬浮窗", file=sys.stderr)
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip(APP_NAME)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray_menu = QMenu()
        # 每次弹出前重建，保证勾选状态与当前配置一致
        self._tray_menu.aboutToShow.connect(lambda: self._populate_menu(self._tray_menu))
        self._tray.setContextMenu(self._tray_menu)
        self._tray.show()

    def _update_tray(self, up_rate, down_rate):
        if self._tray is None:
            return
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(1, 1, 62, 62), 10, 10)
        painter.fillPath(path, QColor(18, 18, 20, 235))

        font = QFont()
        font.setPixelSize(19)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(COLOR_UP)
        painter.drawText(2, 27, fmt_short(up_rate))
        painter.setPen(COLOR_DOWN)
        painter.drawText(2, 55, fmt_short(down_rate))
        painter.end()

        self._tray.setIcon(QIcon(pixmap))
        tooltip = (
            "%s\n↑ %s   ↓ %s\nCPU %.0f%%   内存 %.0f%%"
            % (APP_NAME, fmt_rate(up_rate), fmt_rate(down_rate),
               self._cpu_pct, self._mem_pct)
        )
        temp_bits = []
        for role in TEMP_ROLES:
            if self.cfg["show_temp_%s" % role]:
                temp_bits.append("%s %s" % (TEMP_NAMES[role],
                                            fmt_temp(self._temps.get(role))))
        if temp_bits:
            tooltip += "\n" + "  ".join(temp_bits)
        self._tray.setToolTip(tooltip)

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide()
            else:
                self.show()
                self.raise_()

    # -- 位置 ----------------------------------------------------------
    def _apply_borderless(self):
        """用 _MOTIF_WM_HINTS 去掉标题栏。

        不用 Qt.FramelessWindowHint 的原因见 __init__ 注释：那会让 KWin 把窗口
        标成 OVERRIDE 类型，任务栏里就看不到本程序了。
        """
        xprop = shutil.which("xprop")
        if not xprop:
            return
        try:
            subprocess.run(
                [xprop, "-id", str(int(self.winId())),
                 "-f", "_MOTIF_WM_HINTS", "32c",
                 "-set", "_MOTIF_WM_HINTS", "0x2, 0x0, 0x0, 0x0, 0x0"],
                check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print("警告: 去标题栏失败: %s" % exc, file=sys.stderr)
        # 去装饰前 KWin 已经按“有标题栏”记过尺寸，不重压一次会留下 16px 的黑边。
        QTimer.singleShot(120, self._refresh_geometry)

    def _refresh_geometry(self):
        self._resize_to_content()
        self.move(int(self.cfg.get("x", self.x())), int(self.cfg.get("y", self.y())))

    def showEvent(self, event):
        super().showEvent(event)
        # 每次 show（含“总在最前”切换后重建窗口）都要重压一次，否则标题栏会回来。
        # 必须延后到 Qt 自己写完 _MOTIF_WM_HINTS 之后再改，否则会被覆盖
        # （实测 Qt 会把第二字段写成 0x1 = 保留装饰）。
        QTimer.singleShot(0, self._apply_borderless)

    def restore_position(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        if x is None or y is None:
            x = screen.right() - self.width() - 40
            y = screen.top() + 60
        # 屏幕分辨率变了以后旧坐标可能落在屏幕外，兜回左上角区域
        if not (screen.left() - 50 <= x <= screen.right()
                and screen.top() - 50 <= y <= screen.bottom()):
            x = screen.right() - self.width() - 40
            y = screen.top() + 60
        self.move(int(x), int(y))

    def save_position(self):
        self.cfg["x"] = self.x()
        self.cfg["y"] = self.y()
        save_config(self.cfg)

    # -- 采样与绘制 ----------------------------------------------------
    def refresh(self):
        now = time.monotonic()
        elapsed = max(1e-3, now - self._last_ts)

        rx, tx = read_net_counters()
        up_rate = max(0, tx - self._last_tx) / elapsed
        down_rate = max(0, rx - self._last_rx) / elapsed
        self._last_rx, self._last_tx = rx, tx

        idle, total = read_cpu_times()
        d_total = total - self._last_total
        if d_total > 0:
            self._cpu_pct = max(0.0, min(100.0, (1.0 - (idle - self._last_idle) / d_total) * 100.0))
        self._last_idle, self._last_total = idle, total

        self._mem_pct = read_mem_percent()
        self._temps = read_temps(self.cfg)
        self._powers = read_powers(self.cfg)
        self._last_ts = now

        segments = []
        if self.cfg["show_up"]:
            segments.append(("↑ " + fmt_rate(up_rate), COLOR_UP))
        if self.cfg["show_down"]:
            segments.append(("↓ " + fmt_rate(down_rate), COLOR_DOWN))
        if self.cfg["show_cpu"]:
            segments.append(("CPU %.0f%%" % self._cpu_pct, COLOR_CPU))
        if self.cfg["show_mem"]:
            segments.append(("内存 %.0f%%" % self._mem_pct, COLOR_MEM))
        for role in TEMP_ROLES:
            reading = self._temps.get(role) or {}
            # 没有传感器的指标不占位：勾了也不画，等硬件出现自动回来
            if self.cfg["show_temp_%s" % role] and reading.get("value") is not None:
                segments.append(
                    ("%s %s" % (TEMP_NAMES[role], fmt_temp(reading)),
                     TEMP_COLORS[role])
                )
        for role in POWER_ROLES:
            reading = self._powers.get(role) or {}
            if self.cfg["show_power_%s" % role] and reading.get("value") is not None:
                segments.append(
                    ("%s %s" % (TEMP_NAMES[role], fmt_power(reading)),
                     POWER_COLORS[role])
                )
        if not segments:  # 全部取消勾选时留一句提示，避免窗口变成不可见的空条
            segments.append(("TrafficMonitor", QColor(200, 200, 200)))

        self._segments = segments
        self._rows = self._split_rows(segments)
        self._resize_to_content()
        self._update_tray(up_rate, down_rate)
        self.setToolTip(self._tooltip_text())
        self.update()

    def _split_rows(self, segments):
        """按配置把指标切成多行；max_per_row<=0 或装得下就保持单行。"""
        limit = int(self.cfg.get("max_per_row") or 0)
        if limit <= 0 or len(segments) <= limit:
            return [segments]
        return [segments[i:i + limit] for i in range(0, len(segments), limit)]

    def _tooltip_text(self):
        """悬停时给出温度/功耗的真实来源；缺失项写明原因，方便排查。"""
        lines = []
        for role in TEMP_ROLES:
            reading = self._temps.get(role) or {}
            lines.append("%s温度 %s  ← %s" % (
                TEMP_NAMES[role],
                fmt_temp(reading),
                reading.get("source") or reading.get("note") or TEMP_MISSING_NOTE[role],
            ))
            detail = reading.get("detail")
            if detail and reading.get("value") is not None:
                lines.append("    " + detail)
        for role in POWER_ROLES:
            reading = self._powers.get(role) or {}
            lines.append("%s功耗 %s  ← %s" % (
                TEMP_NAMES[role],
                fmt_power(reading),
                reading.get("source") or reading.get("note") or POWER_MISSING_NOTE[role],
            ))
        return "\n".join(lines)

    def _font(self):
        font = QFont()
        font.setPixelSize(max(8, int(self.cfg["font_size"]) + 2))
        return font

    def _resize_to_content(self):
        metrics = QFontMetrics(self._font())
        rows = self._rows or [[]]
        width = 0
        for row in rows:
            row_width = PAD_X * 2 + GAP * max(0, len(row) - 1)
            for text, _ in row:
                row_width += metrics.horizontalAdvance(text)
            width = max(width, row_width)
        line_height = metrics.height()
        height = PAD_Y * 2 + line_height * len(rows) + ROW_GAP * (len(rows) - 1)
        self.setFixedSize(width, height)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        path = QPainterPath()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path.addRoundedRect(rect, RADIUS, RADIUS)
        alpha = int(max(0.2, min(1.0, float(self.cfg["opacity"]))) * 255)
        painter.fillPath(path, QColor(18, 18, 20, alpha))
        painter.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
        painter.drawPath(path)

        font = self._font()
        painter.setFont(font)
        metrics = QFontMetrics(font)
        line_height = metrics.height() + ROW_GAP
        for row_index, row in enumerate(self._rows or []):
            baseline = PAD_Y + metrics.ascent() + row_index * line_height
            x = PAD_X
            for text, color in row:
                painter.setPen(color)
                painter.drawText(x, baseline, text)
                x += metrics.horizontalAdvance(text) + GAP
        painter.end()

    # -- 交互 ----------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = None
            self.setCursor(Qt.OpenHandCursor)
            self.save_position()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        self._populate_menu(menu)
        menu.exec_(event.globalPos())

    def _populate_menu(self, menu):
        """窗口右键菜单与托盘菜单共用，弹出前重建以同步勾选状态。"""
        menu.clear()

        self._add_toggles(menu, (
            ("show_up", "显示上传速度", None, None),
            ("show_down", "显示下载速度", None, None),
            ("show_cpu", "显示 CPU 占用", None, None),
            ("show_mem", "显示内存占用", None, None),
        ))
        menu.addSeparator()
        self._add_toggles(menu, tuple(
            ("show_temp_%s" % role, "显示 %s 温度" % TEMP_NAMES[role], "temp", role)
            for role in TEMP_ROLES
        ))
        menu.addSeparator()
        self._add_toggles(menu, tuple(
            ("show_power_%s" % role, "显示 %s 功耗" % TEMP_NAMES[role], "power", role)
            for role in POWER_ROLES
        ))
        menu.addSeparator()

        # -- 布局：每行最多 N 个指标（0 = 自动单行） --
        layout_menu = menu.addMenu("布局")
        group = QActionGroup(layout_menu)
        group.setExclusive(True)
        for limit, label in (
            (0, "自动（单行）"),
            (2, "每行 2 项"),
            (3, "每行 3 项"),
            (4, "每行 4 项"),
            (5, "每行 5 项"),
            (6, "每行 6 项"),
            (8, "每行 8 项"),
        ):
            action = QAction(label, layout_menu)
            action.setCheckable(True)
            action.setChecked(int(self.cfg.get("max_per_row") or 0) == limit)
            action.triggered.connect(
                lambda _checked=False, value=limit: self._set_max_per_row(value)
            )
            group.addAction(action)
            layout_menu.addAction(action)

        menu.addSeparator()

        top = QAction("总在最前", menu)
        top.setCheckable(True)
        top.setChecked(bool(self.cfg["always_on_top"]))
        top.toggled.connect(self._toggle_on_top)
        menu.addAction(top)

        autostart = QAction("开机自动启动", menu)
        autostart.setCheckable(True)
        autostart.setChecked(autostart_enabled())
        autostart.toggled.connect(set_autostart)
        menu.addAction(autostart)

        menu.addSeparator()
        about = QAction("%s %s（原生 Linux 版）" % (APP_NAME, APP_VERSION), menu)
        about.setEnabled(False)
        menu.addAction(about)
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

    def _add_toggles(self, menu, items):
        """kind=None 的项恒可用；temp/power 项在当前读数缺失时置灰、不许勾选。"""
        for key, label, kind, role in items:
            reading = {}
            if kind == "temp":
                reading = self._temps.get(role) or {}
            elif kind == "power":
                reading = self._powers.get(role) or {}
            available = kind is None or reading.get("value") is not None
            if not available:
                label += "（无数据）"
            action = QAction(label, menu)
            action.setCheckable(True)
            # 没有数据时不显示成“已勾选”，避免用户以为它在监控
            action.setChecked(bool(self.cfg[key]) and available)
            action.setEnabled(available)
            if available:
                action.toggled.connect(lambda checked, k=key: self._toggle_item(k, checked))
            else:
                action.setToolTip(reading.get("note") or "当前没有可读的传感器")
            menu.addAction(action)

    def _set_max_per_row(self, value):
        self.cfg["max_per_row"] = int(value)
        save_config(self.cfg)
        self.refresh()

    def _quit(self):
        self.save_position()
        if self._tray is not None:
            self._tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _toggle_item(self, key, checked):
        self.cfg[key] = bool(checked)
        save_config(self.cfg)
        self.refresh()

    def _toggle_on_top(self, checked):
        self.cfg["always_on_top"] = bool(checked)
        save_config(self.cfg)
        # 改窗口标志会重建原生窗口，必须重新 show 并复位位置。
        # 这里同样不用 FramelessWindowHint，靠 _MOTIF_WM_HINTS 去标题栏，
        # 否则窗口会从任务栏里消失。
        pos = self.pos()
        flags = Qt.FramelessWindowHint
        if checked:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        self.move(pos)

    def closeEvent(self, event):
        self.save_position()
        if self._tray is not None and self._tray.isVisible():
            # 有托盘时，关闭窗口 = 最小化到托盘（Windows 版就是这个行为），
            # 真正退出走托盘/右键菜单里的“退出”。
            self.hide()
            event.ignore()
            return
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def _raise_existing():
    """已有实例在跑时唤出它的窗口，避免出现两个悬浮条。"""
    sock = QLocalSocket()
    sock.connectToServer(IPC_SOCKET)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def _start_ipc_server(window):
    QLocalServer.removeServer(IPC_SOCKET)  # 清掉上次异常退出残留的 socket 文件
    server = QLocalServer()
    if not server.listen(IPC_SOCKET):
        print("警告: 单实例锁创建失败: %s" % server.errorString(), file=sys.stderr)
        return server

    def on_new_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(conn.readAll)
        window.show()
        window.raise_()
        conn.disconnectFromServer()

    server.newConnection.connect(on_new_connection)
    return server


def main():
    if "--dump-sensors" in sys.argv:
        cfg = load_config()
        entries = _scan_hwmon(force=True)
        print("== 内核暴露的温度传感器 ==")
        for entry in entries:
            for item in entry["temps"]:
                print("  %-16s %-10s %5.1f℃  %s"
                      % (entry["name"], item["label"] or "-",
                         item["value"], item["path"]))
        print("")
        print("== 内核暴露的功率通道 ==")
        power_rows = 0
        for entry in entries:
            for item in entry["powers"]:
                power_rows += 1
                print("  %-16s %-10s %6.1fW  %s"
                      % (entry["name"], _temp_name(item),
                         item["watts"], item["path"]))
        if power_rows == 0:
            print("  （无 power*_input 通道）")
        print("")
        print("== 四个监控项解析结果 ==")
        temps = read_temps(cfg)
        for role in TEMP_ROLES:
            reading = temps[role]
            print("  温度 %-4s %-5s <- %s"
                  % (TEMP_NAMES[role], fmt_temp(reading),
                     reading["source"] or reading["note"]))
        # RAPL 是累计计数器，必须取两次间隔才能算出瓦
        first = read_powers(cfg, time.monotonic())
        time.sleep(0.6)
        powers = read_powers(cfg, time.monotonic())
        for role in POWER_ROLES:
            reading = powers[role]
            print("  功耗 %-4s %-5s <- %s"
                  % (TEMP_NAMES[role], fmt_power(reading),
                     reading["source"] or reading["note"]))
        return 0
    if "--version" in sys.argv:
        print("%s %s" % (APP_NAME, APP_VERSION))
        return 0
    if "--reset-pos" in sys.argv:
        cfg = load_config()
        cfg["x"] = cfg["y"] = None
        save_config(cfg)
        print("位置已重置为右上角")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    # 让窗口/托盘与 /usr/share/applications/trafficmonitor.desktop 对应，
    # 任务栏和托盘才会用我们装的图标，而不是通用占位图。
    app.setDesktopFileName("trafficmonitor")

    if _raise_existing():
        print("%s 已在运行，已唤出窗口" % APP_NAME)
        return 0

    window = MonitorWindow()
    window.show()
    server = _start_ipc_server(window)
    rc = app.exec_()
    server.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
