# 架构设计

## 目标

WiFi-Looking 的目标是：

1. 准确感知笔记本的 Wi-Fi 状态变化
2. 把状态变化抽象成稳定的事件模型
3. 让触发规则可以完全由配置驱动
4. 把“触发”和“执行”解耦，方便以后扩展更多动作

## 当前架构

~~~
netsh wlan show interfaces
        |
        v
   wifi.js 读取并解析状态
        |
        v
   events.js 计算差异事件
        |
        v
   rules.js 匹配规则和冷却时间
        |
        v
   actions.js 执行动作
~~~

## 模块职责

### config.js

负责读取 JSON 配置，并做基本合法性校验。后续可以继续扩展为：

- JSON Schema 完整校验
- 配置热加载
- 配置迁移

### wifi.js

当前通过 Windows 自带的 netsh 命令读取 Wi-Fi 状态。它会处理：

- 中文/英文系统输出
- UTF-8 / GB18030 / UTF-16 编码
- connected / disconnected 状态归一化

### events.js

把“前后两次状态”变成事件。事件模型刻意保持很小：

- connected
- disconnected
- changed

这样做的好处是，规则配置不用关心底层监控方式。以后即使换成 Windows 原生 WLAN 通知，规则层也不用改。

### rules.js

负责规则匹配和冷却控制。规则支持：

- 事件类型过滤
- 事件前 SSID 过滤
- 事件后 SSID 过滤
- 冷却时间

### actions.js

动作执行器。当前支持：

- log
- http
- clash.closeConnections
- command

后续可以继续加入：

- Windows 通知
- 杀进程
- 启动程序
- 脚本执行
- 延迟执行
- 条件动作

## 关键设计取舍

### 为什么先做轮询？

轮询实现简单、依赖少，能先把规则引擎和动作执行链路跑通。后续再替换为 Windows 原生 WLAN 通知，只需要替换 wifi.js，不影响其他模块。

### 为什么规则和动作分离？

这样可以把“什么时候触发”和“触发后做什么”分开管理。比如同一个 Wi-Fi 断开事件，可以既发通知，又关闭 Clash 连接，也可以以后换成其他动作。

### 为什么默认不直接杀 ClashMi？

“关闭连接”和“关闭软件”是两件事。当前更推荐通过 Clash 外部控制 API 只断开连接，不直接结束进程；如果确实需要结束进程，再用 command 动作。

## 后续演进

1. 引入 Windows 原生 WLAN 通知
2. 增加托盘 UI
3. 增加配置编辑器
4. 增加运行日志查看和规则测试按钮
5. 增加安装包和开机自启动
