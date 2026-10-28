# 小马联动 Python 版

Windows 笔记本 Wi-Fi 变化监控工具：断开 / 连上 / 切换 Wi-Fi 时按规则触发动作，核心动作是断开 ClashMi 的活动连接。详细说明见仓库根目录的 README.md。

## 快速开始

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

常用参数：`--dry-run`（只匹配不执行）、`--no-tray`（控制台模式）、`--once`（读一次状态退出）、`--no-native`（强制轮询）。

## 规则

在主界面里增删改：触发时机（切换 / 断开 / 连上）× 来源 SSID × 目标 SSID（都可多填、逗号分隔）× 动作（断开 ClashMi 连接 / 关闭 TUN 等）× 冷却时间。规则保存在程序目录的 `config.json`，托盘菜单可重新加载。

## ClashMi

自动读取 `%APPDATA%\clashmi\clashmi\service_core_setting.json` 的 External Controller 与 secret；缺失时读注册表 `HKCU\Software\ClashMi\did` 推导；最后回退 `http://127.0.0.1:9090`。无需手填 secret。

## 打包

```
pip install -r requirements-dev.txt
pip install -r requirements-windows.txt
python build.py
```

产出 `dist/XiaoMaLink.exe`，装有 Inno Setup 6 时同时产出安装器 `dist/XiaoMaLink_Setup_v1.1.0.exe`。

## 测试

```
python -m unittest discover -s tests
```
