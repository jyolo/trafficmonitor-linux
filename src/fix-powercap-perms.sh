#!/bin/sh
# 只放行 /sys/class/powercap/*/energy_uj 的读权限（a+r）。
# 用途：让非 root 的 TrafficMonitor 能读 RAPL 累计能量，差分算出 CPU 功耗。
# 不触碰 enabled / max_energy_range_uj 等其它属性：写权限仍然只有 root 有。
# 由 udev 规则在 powercap 设备出现/变化时调用，也可 postinst 里手工跑一次。
for f in /sys/class/powercap/*/energy_uj; do
    [ -e "$f" ] || continue
    chmod a+r "$f" 2>/dev/null || true
done
exit 0
