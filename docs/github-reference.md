# GitHub 可参考/二次开发项目调研

> 结论：目前没有找到“Windows Wi-Fi 变化 + Clash/ClashMi 规则引擎”完全匹配的直接项目。  
> 但以下项目可以按模块拆开二次开发，尤其是基于现有项目补上 Windows Wi-Fi 事件和 Clash 控制层。

## 最接近目标

### sweellan/mihomo-wifi-auto-switcher  
- 链接：<https://github.com/sweellan/mihomo-wifi-auto-switcher>
- 亮点：  
  - 与“按 Wi-Fi 变化切换 Mihomo/Clash 配置/节点”的核心思路非常接近。  
  - 使用 Mihomo 本地 API 控制，而不是界面自动化。  
  - 结构清晰，包含 config、tests、UI、运行脚本。  
- 限制：  
  - 面向 macOS，依赖 Unix-socket API 和 macOS Wi-Fi 识别。  
  - 若二次开发，需要替换 Wi-Fi 监听层和平台 API 调用层。

### Indigo744/wlanprofilemanager-ps  
- 链接：<https://github.com/Indigo744/wlanprofilemanager-ps>
- 亮点：  
  - Windows 平台，监听 Wi-Fi 连接并切换网络配置。  
  - 带安装任务、通知、日志等基础工程结构。  
- 限制：  
  - 主要用于 IP/DNS profile 切换，不包含 Clash 控制。

### xzer/wlanprofilemanager  
- 链接：<https://github.com/xzer/wlanprofilemanager>
- 亮点：  
  - Node.js 实现，轻量，依赖少。  
  - 使用 `netsh wlan show interfaces` 解析 SSID 和接口名。  
  - 与我们当前项目的技术栈一致，最容易作为参考实现。  
- 限制：  
  - 逻辑简单，没有完整规则引擎。

### LukaszWiktor/slack-status-based-on-wifi-name  
- 链接：<https://github.com/LukaszWiktor/slack-status-based-on-wifi-name>
- 亮点：  
  - 明确按 Wi-Fi 名称触发外部动作。  
  - 结构较小，易于抽取成事件驱动示例。  
- 限制：  
  - 主要绑定 Slack，不太适合直接改造为通用规则引擎。

## Clash/ClashMi 相关

### KaringX/clashmi  
- 链接：<https://github.com/KaringX/clashmi>
- 亮点：  
  - ClashMi 官方客户端本体。  
  - 内置 Mihomo 内核和 zashboard 面板。  
- 限制：  
  - 作为完整 GUI 客户端，不适合直接当作自动化规则引擎二次开发。  
  - 更适合用于确认其本地 API / external-controller 能力。

### AiheCola/node-inspector  
- 链接：<https://github.com/AiheCola/node-inspector>
- 亮点：  
  - 使用 Mihomo External Controller 公共 HTTP API。  
  - Windows 桌面工具，便于参考如何与 Clash/Mihomo 控制接口交互。  
- 限制：  
  - 侧重节点检测，不聚焦 Wi-Fi 变化触发。

## 更大但成熟的网络工具

### BornToBeRoot/NETworkManager  
- 链接：<https://github.com/BornToBeRoot/NETworkManager>
- 亮点：  
  - 成熟的 Windows 网络管理工具，包含 Wi-Fi Analyzer、Ping Monitor 等。  
  - 工程化程度高，UI、配置、日志、测试都比较完整。  
- 限制：  
  - 体量大，作为本项目二次开发基础成本偏高。  
  - 更适合只参考其 Wi-Fi 相关模块。

## 推荐路线

1. **保留我们的项目作为骨架**  
   我们已经有了 Wi-Fi 状态解析、事件模型、规则匹配、动作执行和测试，基础结构比直接拿单一项目改造更清晰。

2. **参考 `sweellan/mihomo-wifi-auto-switcher` 的 Mihomo 控制思路**  
   重点看它如何组织 profile/node 切换、冷启动、熔断、dry-run、测试。

3. **参考 `xzer/wlanprofilemanager` 的 Windows 解析方式**  
   尤其是接口名、SSID、netsh 输出解析和最小依赖实现。

4. **参考 `Indigo744/wlanprofilemanager-ps` 的安装/计划任务设计**  
   后续做开机自启动、通知和日志时可以借鉴。

5. **对 ClashMi/Clash 控制层继续确认**  
   需要确认 ClashMi 是否暴露标准 Mihomo External Controller、端口和 secret。  
   如果确认可用，可以继续完善 `clash.closeConnections` 动作，甚至加入 profile/node 切换动作。
