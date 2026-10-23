# WiFi-Looking

一个面向 Windows 笔记本的 Wi-Fi 变化监控和规则触发工具：当笔记本断开某个 Wi-Fi、连上某个 Wi-Fi、或从一个 Wi-Fi 切换到另一个时，按规则自动执行动作。核心场景是离开指定 Wi-Fi 时自动断开 ClashMi 的活动连接。

当前仓库包含两套实现：

1. **Node.js 原型版**：早期架构验证，已冻结，仅作参考。
2. **Python 版（正式）**：PySide6 主界面 + 系统托盘、Windows 原生 WLAN 通知（失败自动回退轮询）、规则引擎、ClashMi 自动对接、EXE/安装器打包。

## Python 版

目录：

```
python/
  main.py               # 启动入口（GUI + 托盘；--no-tray 控制台模式）
  main_window.py        # 主界面：规则增删改、启用/禁用、ClashMi 状态
  tray_app.py           # 系统托盘与右键菜单
  config.py             # 配置读取、校验、保存
  wifi_monitor.py       # netsh 轮询监控与防抖、事件生成
  native_wifi_monitor.py# Windows 原生 WLAN 通知监控
  rule_engine.py        # 规则匹配与冷却控制
  clashmi.py            # 自动发现 ClashMi 的 External Controller 与 secret
  action_executor.py    # 动作执行（ClashMi / 系统代理 / 进程 / 命令等）
  build.py              # 一键打包：PyInstaller exe + Inno Setup 安装器
  installer.iss         # Inno Setup 脚本
  config.json           # 默认配置
  tests/                # 自动化测试（unittest）
```

### 安装与运行

```
cd python
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

- `python main.py --dry-run`：只匹配规则不执行动作
- `python main.py --no-tray`：控制台模式
- `python main.py --once`：读取一次当前 Wi-Fi 状态后退出
- `python main.py --no-native`：强制轮询模式

配置和日志固定放在程序所在目录（不再跟随启动时的工作目录）。

### 规则模型

规则由主界面维护，保存在 `config.json`：

- 触发时机：切换 Wi-Fi 时 / 断开 Wi-Fi 时 / 连上 Wi-Fi 时
- 来源 SSID、目标 SSID 均可填多个（逗号分隔），目标可用 `*` 表示任意
- 动作：断开 ClashMi 连接（默认）、关闭 ClashMi TUN（可选），以及旧版的系统代理、进程、命令、HTTP 等动作
- 每条规则可单独启用/禁用，并带冷却时间

默认规则：

- 从 `Xms` / `Xms 5G` 断开时，断开 ClashMi 连接
- 从 `Xms` / `Xms 5G` 切换到其他 Wi-Fi 时，断开 ClashMi 连接

### ClashMi 对接

程序自动发现 ClashMi 的 External Controller 地址和 secret，不需要手填：

1. 先读 `%APPDATA%\clashmi\clashmi\service_core_setting.json` 的 `external-controller` 和 `secret`；
2. 读不到时从注册表 `HKCU\Software\ClashMi\did` 按 ClashMi 的规则推导 secret；
3. 再不行回退 `http://127.0.0.1:9090`。

主界面启动时会自动检测一次 ClashMi 连接状态，托盘和界面里也有「测试 ClashMi」入口。

### 打包

```
pip install -r requirements-dev.txt
python build.py
```

生成 `python/dist/WiFi-Looking.exe`；若装有 Inno Setup 6，会继续生成安装器 `WiFi-Looking-Setup.exe`（安装到用户目录、无需管理员，可选桌面快捷方式与开机自启）。

### 测试

```
cd python
python -m unittest discover -s tests
```

界面测试需要 PySide6（已在 requirements 里），其余测试无额外依赖。

## 旧版 Node.js 原型

```
copy config\config.example.json config\config.json
npm run doctor
npm start
```

仅作架构参考，后续开发以 `python/` 为准。
