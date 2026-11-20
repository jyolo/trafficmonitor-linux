# TrafficMonitor for Linux（原生 .deb）

版本：@VERSION@

## 这是什么

上游 [zhongyang219/TrafficMonitor](https://github.com/zhongyang219/TrafficMonitor) 是
Windows 平台的 MFC 桌面程序。MFC 是微软专有框架，GCC / MinGW 都不提供，
所以该仓库**没有任何可以在 Linux 上产出二进制文件的构建路径**（工程目录里只有
`.sln` / `.vcxproj`，没有 CMake 或 Makefile；105 个 `.cpp` 中绝大多数 include `<afxwin.h>`）。

本包是按它公开的功能语义重写的**原生 Linux 实现**，直接读内核伪文件，不跑 Wine、
不依赖 Windows 运行时：

| 指标 | 数据来源 |
|---|---|
| 上行 / 下行速度 | `/proc/net/dev`（排除 lo，聚合全部接口，按 1 秒差分） |
| CPU 占用率 | `/proc/stat`（两次采样求非 idle 占比） |
| 内存占用率 | `/proc/meminfo`（`MemTotal - MemAvailable`） |
| CPU / 主板 / 内存 / 硬盘温度 | `/sys/class/hwmon`（见「硬件温度」一节） |
| CPU / 主板 / 内存 / 硬盘功耗 | `/sys/class/powercap`（RAPL）+ `hwmon power*_input`（见「功耗」一节） |

## 安装

    sudo dpkg -i trafficmonitor_@VERSION@_all.deb
    trafficmonitor          # 或从开始菜单启动「网速悬浮窗」

依赖：`python3`、`python3-pyqt5`（Deepin 25 / Debian 系默认仓库已有）。
若 `dpkg` 报依赖缺失：`sudo apt-get install -f`。

## 使用

- 左键拖动移动位置，位置自动记住
- 右键菜单：切换显示项（上传 / 下载 / CPU / 内存 / 四项温度 / 四项功耗）；没有传感器的项
  会置灰并标「（无数据）」，不能勾选，悬浮条也不为它留位置
- 右键菜单「布局」：单行自动，或每行最多 2/3/4/5/6/8 项，超出自动换行，窗口高度跟着变
- 关闭窗口 = 最小化到托盘，真正退出走菜单里的“退出”
- 再次启动不会开出第二个悬浮条（单实例，会自动唤出已有窗口）
- `trafficmonitor --version`
- `trafficmonitor --reset-pos`  位置跑到屏幕外时重置到右上角
- `trafficmonitor --dump-sensors`  只读：列出内核暴露的全部温度/功率传感器，以及八项各自解析到了谁

## 硬件温度

四项温度全部走内核 `hwmon`，**不调外部命令、不做估算**。某一项没有对应传感器时：
右键菜单里置灰、文字后标「（无数据）」且不能勾选；悬浮条上也不占位（不会出现一行 `--`）。
鼠标悬停在悬浮条上可以看到缺失原因和实际用到的传感器名。

| 显示项 | 优先数据源 | 说明 |
|---|---|---|
| CPU | `k10temp`(Tctl/Tdie)、`coretemp`(Package id 0) | AMD / Intel 桌面与移动平台都由内核原生驱动提供 |
| 主板 | `nct6683` / `nct6775` / `it87` / `w83627ehf` 等 Super I/O 的 `SYSTIN`/`System` 标签 | **只认主板类标签**，绝不拿 PECI CPU / VRM 冒充 |
| 内存 | `spd5118`（DDR5 SPD Hub，DDR5 才有）、`jc42` | DDR4 SO-DIMM 物理上没有温度传感器，只能显示 `--` |
| 硬盘 | `nvme` 的 `Composite`、`drivetemp`（SATA） | 多块盘取最热的一块，悬停可看到每块盘的温度 |

SATA 盘的温度要 `drivetemp` 模块（安装包已在 `/etc/modules-load.d/trafficmonitor-sensors.conf`
写入开机加载，并在 postinst 里立即 `modprobe`）。手动加载：

    sudo modprobe drivetemp
    sudo modprobe spd5118      # 仅 DDR5 内存条有意义，其它平台加载也无副作用

内核看不到的情况（迷你主机把温度接在 EC、DDR4 内存条、USB 硬盘盒）就显示 `--`——
这是硬件事实，不是程序缺陷。想指定某个传感器，可在配置里写 `temp_override`：

    "temp_override": {"board": "nct6683:SYSTIN", "cpu": "k10temp:Tctl"}

值写成 `<hwmon name>:<label>`；驱动名可用 `trafficmonitor --dump-sensors` 查。

## 功耗

| 显示项 | 数据来源 | 说明 |
|---|---|---|
| CPU | `/sys/class/powercap/*/energy_uj`（RAPL 累计能量，差分算瓦） | AMD/Intel 都可用；**需要读权限**，见下 |
| 主板 | Super I/O / EC 的 `power*_input` | 消费级主板基本没有功率通道，显示 `--` |
| 内存 | PMBus/VRM 的 `power*_input` | 消费级内存条没有功率传感器，显示 `--` |
| 硬盘 | 硬盘 hwmon 的 `power*_input` | NVMe 一般不上报功率，显示 `--` |

`energy_uj` 默认是 `0400 root:root`。安装包会放一条 udev 规则
（`/etc/udev/rules.d/60-trafficmonitor-powercap.rules`）并在安装时执行
`/usr/lib/trafficmonitor/fix-powercap-perms.sh`，只把该文件改成 `a+r`，
`enabled` 等可写属性仍只有 root 能动。装完可以用下面的命令确认：

    stat -c '%a %n' /sys/class/powercap/intel-rapl:0/energy_uj   # 应为 444
    trafficmonitor --dump-sensors                                 # 看「功耗 CPU」有没有数

没有功率通道的项与温度同理：菜单置灰标「（无数据）」，悬浮条不占位。

卸载会删掉这条 udev 规则，但已经 chmod 过的 sysfs 权限要重启才复位（无害）。

## 任务栏 / 托盘说明（Deepin 25 实测）

- 窗口类型是普通窗口（`_NET_WM_WINDOW_TYPE_NORMAL`），标题栏用
  `_MOTIF_WM_HINTS` 去掉。**不要**改回 `Qt.FramelessWindowHint`：Qt 会给
  无边框窗口附加 `_KDE_NET_WM_WINDOW_TYPE_OVERRIDE`，KWin 就把它当覆盖层，
  任务栏/分页器一律不显示。
- Deepin 25 的停靠栏（dde-shell）不会为任意窗口生成任务栏按钮，只会显示
  “已固定”的应用。固定方法（写入用户 dconf，可随时撤回）：

      gsettings set com.deepin.dde.dock docked-apps \
        "$(gsettings get com.deepin.dde.dock docked-apps | sed "s/]$/, 'trafficmonitor']/")"

  固定项需要停靠栏重载才生效（注销重登最稳）。
  撤回：`gsettings reset com.deepin.dde.dock docked-apps`
- 程序同时注册了 StatusNotifierItem 托盘项（`org.kde.StatusNotifierItem`，
  Id=TrafficMonitor），图标内容为上行/下行速度文字，tooltip 里是完整数值。
  若你的托盘区没显示它，多半是被折进了 `^` 展开面板。

## 配置文件

- 配置：`~/.config/trafficmonitor-linux/config.json`
- 开机自启：`~/.config/autostart/trafficmonitor.desktop`

## 卸载

    sudo dpkg -r trafficmonitor        # 保留配置
    sudo dpkg -P trafficmonitor        # 连配置一起删（配置在用户目录，需手动删）

## 与上游的差异

- 无任务栏嵌入（Windows 任务栏 API 在 Linux 不存在），只有桌面悬浮窗
- 无皮肤系统（上游皮肤为 Windows 位图格式）
- 温度/功耗项只读内核暴露的传感器，不像上游那样依赖 LibreHardwareMonitor + WMI；
  迷你主机的 EC、DDR4 内存条、消费级硬件没有功率通道，界面会如实显示 `--`
- 显示项支持单行或多行（每行最多 N 项），字体/不透明度写在配置里

## 构建

    ./build-deb.sh            # 产出 dist/trafficmonitor_@VERSION@_all.deb
    ./build-deb.sh --clean    # 先清空 build/ 与 dist/
