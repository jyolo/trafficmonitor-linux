#!/bin/bash
# 构建 TrafficMonitor 原生 Linux 版 .deb 包
# 用法: ./build-deb.sh [--clean]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="1.86.0+linux10"
PKG="trafficmonitor"
ARCH="all"
BUILD="${ROOT}/build"
DIST="${ROOT}/dist"
TREE="${BUILD}/${PKG}_${VERSION}_${ARCH}"
DEB="${DIST}/${PKG}_${VERSION}_${ARCH}.deb"

if [ "${1:-}" = "--clean" ]; then
    rm -rf "${BUILD}" "${DIST}"
    echo "已清理 build/ dist/"
fi

rm -rf "${TREE}"
mkdir -p "${TREE}/DEBIAN" \
         "${TREE}/opt/${PKG}" \
         "${TREE}/usr/bin" \
         "${TREE}/usr/share/applications" \
         "${TREE}/usr/share/icons/hicolor/128x128/apps" \
         "${TREE}/usr/share/icons/hicolor/48x48/apps" \
         "${TREE}/usr/share/doc/${PKG}" \
         "${TREE}/usr/lib/${PKG}" \
         "${TREE}/etc/udev/rules.d" \
         "${DIST}"

# ---- 应用文件 ----
install -m 0755 "${ROOT}/src/trafficmonitor.py" "${TREE}/opt/${PKG}/trafficmonitor.py"
install -m 0644 "${ROOT}/src/trafficmonitor.png" "${TREE}/opt/${PKG}/trafficmonitor.png"
install -m 0755 "${ROOT}/src/fix-powercap-perms.sh" "${TREE}/usr/lib/${PKG}/fix-powercap-perms.sh"
install -m 0644 "${ROOT}/src/60-trafficmonitor-powercap.rules" \
    "${TREE}/etc/udev/rules.d/60-trafficmonitor-powercap.rules"
install -m 0644 "${ROOT}/src/trafficmonitor.png" "${TREE}/usr/share/icons/hicolor/128x128/apps/${PKG}.png"
/usr/bin/python3 -c "
import sys
from PyQt5.QtGui import QGuiApplication, QPixmap
from PyQt5.QtCore import Qt
app = QGuiApplication(sys.argv)   # QPixmap 需要先有 GUI 应用实例
pm = QPixmap('${ROOT}/src/trafficmonitor.png')
pm.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation).save('${TREE}/usr/share/icons/hicolor/48x48/apps/${PKG}.png', 'PNG')
"

# ---- 启动器：固定解释器，避免 conda 的 python3 抢在前面 ----
cat > "${TREE}/usr/bin/${PKG}" <<'LAUNCHER'
#!/bin/sh
# TrafficMonitor 启动器
# 固定使用系统 python3：conda 环境里的 python 缺 PyQt5，
# 走 PATH 解析会得到 ModuleNotFoundError 这种假故障。
PY=/usr/bin/python3
if [ ! -x "$PY" ]; then
    PY=$(command -v python3) || { echo "未找到 python3" >&2; exit 1; }
fi
exec "$PY" /opt/trafficmonitor/trafficmonitor.py "$@"
LAUNCHER
chmod 0755 "${TREE}/usr/bin/${PKG}"

# ---- 桌面入口 ----
cat > "${TREE}/usr/share/applications/${PKG}.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=TrafficMonitor
Name[zh_CN]=网速悬浮窗
GenericName=Network Monitor
GenericName[zh_CN]=网络监视
Comment=显示网速、CPU 与内存占用的桌面悬浮窗
Comment[en]=Floating window showing network speed, CPU and memory usage
Exec=trafficmonitor
Icon=trafficmonitor
Terminal=false
Categories=Utility;Monitor;
Keywords=network;speed;cpu;memory;traffic;
StartupWMClass=TrafficMonitor
StartupNotify=false
DESKTOP
chmod 0644 "${TREE}/usr/share/applications/${PKG}.desktop"

# ---- 文档 ----
sed -e "s/@VERSION@/${VERSION}/g" "${ROOT}/README.md" > "${TREE}/usr/share/doc/${PKG}/README.md"

# ---- 控制信息 ----
INSTALLED_SIZE=$(du -sk --exclude=DEBIAN "${TREE}" | cut -f1)
cat > "${TREE}/DEBIAN/control" <<CONTROL
Package: ${PKG}
Version: ${VERSION}
Architecture: ${ARCH}
Maintainer: local <local@localhost>
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.8), python3-pyqt5
Section: utils
Priority: optional
Homepage: https://github.com/zhongyang219/TrafficMonitor
Description: 桌面网速/CPU/内存/温度/功耗悬浮窗（原生 Linux 实现，非 Wine）
 参考 zhongyang219/TrafficMonitor 的功能重写的原生 Qt5 悬浮窗：
 实时上行/下行速度、CPU 占用率、内存占用率，CPU/主板/内存/硬盘的温度
 与功耗，支持单行/多行布局（每行最多 N 项）、拖动、置顶、项目显隐切换
 与开机自启，配置保存在 ~/.config/trafficmonitor-linux。
 上游为 Windows MFC 专用工程，无法在 Linux 直接编译，故未使用其二进制。
CONTROL

cat > "${TREE}/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true

    # 温度采集用到的两个内核模块：
    #   drivetemp —— SATA 盘的 SMART 温度（大多数发行版不会默认加载）
    #   spd5118   —— DDR5 内存条的 SPD Hub 温度（DDR4 上加载也无副作用）
    # 写 modules-load.d 保证重启后还在；失败不影响安装（没有 SATA 盘也没有害处）。
    printf 'drivetemp\nspd5118\n' > /etc/modules-load.d/trafficmonitor-sensors.conf 2>/dev/null || true
    modprobe drivetemp 2>/dev/null || true
    modprobe spd5118 2>/dev/null || true

    # RAPL 的 energy_uj 默认 0400 root-only；放行读权限后普通用户才能算 CPU 功耗。
    # 用 udev 规则保证重启后仍然生效，这里立即执行一次。
    /usr/lib/trafficmonitor/fix-powercap-perms.sh 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=powercap --action=change 2>/dev/null || true
fi
exit 0
POSTINST
chmod 0755 "${TREE}/DEBIAN/postinst"

cat > "${TREE}/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
    # 只删本包自己写的 modules-load.d 条目与 udev 规则，别的文件不动
    rm -f /etc/modules-load.d/trafficmonitor-sensors.conf
    rm -f /etc/udev/rules.d/60-trafficmonitor-powercap.rules
    udevadm control --reload-rules 2>/dev/null || true
fi
exit 0
POSTRM
chmod 0755 "${TREE}/DEBIAN/postrm"

# ---- 打包 ----
dpkg-deb --root-owner-group --build "${TREE}" "${DEB}" > /dev/null
echo "构建完成: ${DEB}"
dpkg-deb -I "${DEB}" | sed -n '1,14p'
echo "---- 文件清单 ----"
dpkg-deb -c "${DEB}" | awk '{print $6}'
