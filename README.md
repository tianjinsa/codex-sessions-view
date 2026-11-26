# Codex Session Ledger

一个离线运行的 **Python + Qt 6（PyQt6）** 桌面程序，读取 Codex 的 sessions JSONL，按模型输入、缓存输入和输出单价估算费用。

## 启动

本机已有 Python 和 PyQt6 时，直接双击 `启动.bat`，或运行：

```powershell
python main.py
```

首次安装依赖：

```powershell
python -m pip install -r requirements.txt
python main.py
```

默认扫描 `%USERPROFILE%\.codex\sessions`。设置 `CODEX_HOME` 后改用该位置的 `sessions` 子目录。也可以在界面中选择其他目录或单个 JSONL 文件，或通过命令行指定路径。

```powershell
python main.py --sessions "D:\path\to\sessions" --config "D:\path\to\prices.json"
```

## 功能

- 后台读取 JSONL，按事件内容识别 Codex 会话。扫描状态会显示识别、跳过和无用量的文件数量；“扫描详情”可查看异常文件和行号。
- 支持按主会话和子智能体筛选。子智能体会话显示名称、路径和父会话 ID。可按名称、委派消息或父会话 ID 搜索，CSV 也包含会话类型与父会话 ID。
- 按模型分别统计非缓存输入、缓存输入、输出和 API 调用次数。
- 费用总览、近 14 个活跃日趋势（matplotlib 柱状图，柱顶直接标注金额）、会话明细、模型统计和每日统计。
- 每个会话显示**缓存命中率**（`cached_input_tokens ÷ input_tokens`）；最近会话、会话明细、会话详情按模型拆分、CSV 导出中都包含该指标。悬停命中率还能看到**理论上限**：只复用上一次输入、以及复用上一次输入+输出两种口径，并给出与该上限的差距。
- 按**任务轮次**统计：从你发出提问开始，到 GPT 完成任务或中断/异常结束为止算一轮，可单独查看该轮的输入、输出、缓存命中率和费用。
- 任务轮次表显示每条任务的**完成时间与耗时**（`HH:MM:SS`），输入拆分为非缓存输入与缓存输入。
- 悬停任务标题可预览提问、补充消息和最终回复。双击任务并打开“任务内容”，可滚动查看和复制保留换行的完整文本。
- 把鼠标移到任意“输出”单元格上，可看到其中有多少是**思考（reasoning）token** 及其占比；思考 token 已包含在输出里，不会重复计费。
- 界面强制使用浅色主题，不跟随 Windows 深色模式，避免局部深色控件与深色标题栏。
- 双击会话或任务轮次，查看**费用构成环形图**：非缓存输入、缓存输入、输出三项的单价、金额与占比。
- 会话列表优先显示首条提问摘要。可搜索提问、补充消息、会话 ID、工作目录或模型名称。日期和模型筛选按 API 调用生效，会话明细、任务用量和 CSV 使用同一筛选结果。

## 任务轮次的口径

主会话中的一个任务轮次由**用户提问**开启，由 `task_complete` 结束；`turn_aborted` 或日志戛然而止（异常中断）同样算一个任务轮次，并标记为“已中断 / 异常结束 / 未正常收尾”。

以下情况并入当前任务轮次，不会单独计一轮：

- 任务进行中你补充的消息。它们与首个提问共享同一个 `turn_id`，会显示为“（+N 条补充）”。同一次发言的多种日志回显会合并。
- 纯模型内部循环，例如工具调用续跑、自动压缩产生的独立 `turn_id` 且没有用户提问。它们的 token 与费用计入所属任务轮次，并在悬停提示中注明。

带提问但尚未产生用量的任务仍会出现在未限定日期或模型的会话明细里。只有启动记录的会话显示为 0 个任务。

子智能体按日志中的执行边界识别轮次。执行详情可查看委派消息、回复和日志记录的父轮次 ID。默认总览汇总主会话和子智能体各自的用量，可以用会话类型筛选单独核对。

## 计费口径

单价单位是 **每 100 万 tokens，以配置文件中的货币计价**：

```text
非缓存输入 = input_tokens - cached_input_tokens
费用 = (非缓存输入 × input
        + cached_input_tokens × cached_input
        + output_tokens × output) / 1,000,000
```

`reasoning_output_tokens` 已包含在 `output_tokens` 内，**不会重复计费**；界面的输出列悬停提示会单独显示这部分思考 token。`cache_write_input_tokens` 目前不参与费用计算。

会话缓存命中率的口径是 `cached_input_tokens ÷ input_tokens`，即“命中的输入 token 占全部输入 token 的比例”，**不把输出 token 计入分母**；输入为 0 时显示 `—`。

### 缓存命中率的理论上限

上下文是连续累积的：第 N 次请求的输入，等于第 N-1 次的完整上下文（input + output）再加上本轮新增内容。因此命中率有两个层次的参照：

- **硬上限**：`cached_input_tokens` 不可能超过本次 `input_tokens`。这是唯一严格成立的边界。
- **可复用上限（保守）**：上一轮的 `input_tokens` 必定已在上下文中，前缀不变就该全部命中。
- **可复用上限（乐观）**：上一轮的 `input + output` 也进入了下轮上下文，但 output 能否命中取决于服务端前缀缓存的边界。

因为上下文持续增长，`cached_input_tokens` 完全可以超过“上一轮 input + output”，这不是异常，而是新增内容与前缀缓存边界共同作用的结果，提示中不再把它当作警告。

**首次请求**没有“上一次”可参考，因此不预估上限，直接以它的实际命中作为上限。这样所有请求使用同一批分子分母，满足 `实际命中 + 缺口 = 上限`，两个百分数可以直接比较和核对。

**额外缓存失效**只统计**本次命中低于上一次命中**的下降量。命中与上次持平或更高时不算失效。

上下文被压缩后，压缩前的历史不再可复用，压缩后的第一条请求会跳过上限判定，只作为新的比较基线，避免把压缩开销误判为缓存变差。

### 为什么不能直接累加 `total_token_usage`

`event_msg.payload.info.total_token_usage` 是到当前请求为止的**累计快照**，不是本次请求的新增量。逐行求和会把前面的请求重复收费。本程序的优先级是：

1. `token_usage_record.payload.usage`：本次 API 调用用量，按 `response_id` 去重；不累加其中的 `turn_token_usage`、`thread_token_usage` 快照。
2. 旧版 `event_msg.payload.info.last_token_usage`：本次请求用量。
3. 只有 `total_token_usage` 时，按会话的上一条累计快照做差分。输入或输出累计值回退时重新建立基线。
4. 同一文件同时出现新版记录和旧版事件时，一对一匹配同次调用，保留独立的旧版用量。

例如，单价为输入 2、缓存输入 0.1、输出 10（每百万 tokens），请求用量为：

```json
{
  "input_tokens": 22767,
  "cached_input_tokens": 14720,
  "output_tokens": 707,
  "reasoning_output_tokens": 663
}
```

费用为：

```text
(8,047 × 2 + 14,720 × 0.1 + 707 × 10) / 1,000,000 = 0.024636
```

### 配置文件

默认配置在程序目录的 `prices.json`，格式如下：

```json
{
  "currency": "USD",
  "currency_symbol": "$",
  "models": {
    "模型名称": {
      "input": 2,
      "cached_input": 0.1,
      "output": 10,
      "note": "可选备注"
    },
    "gpt-5-*": {
      "input": 1.25,
      "cached_input": 0.125,
      "output": 10
    }
  }
}
```

模型名优先精确匹配；也支持仅在末尾使用 `*` 的前缀规则，多个规则匹配时取最长前缀。GUI 会保留已有备注。请根据自己的计费标准核对并修改单价，程序不会联网更新价格。

**金额是按用户单价计算的估算值，不等于 Codex 订阅账单、优惠、税费或实际扣款。** 日期筛选按 API 调用发生时间逐条筛选，使用本机时区；因此跨日会话在不同日期只显示当日的用量。

## 测试

```powershell
python -m unittest -v
```

无桌面环境中可使用：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest -v
```

测试覆盖真实请求样例、新旧格式去重、累计差分、上下文压缩、异常 JSON 行、未知模型、价格配置、模型/日期筛选和 Qt 页面。

## 文件

- `main.py`：Qt 6 界面与启动入口。
- `codex_ledger.py`：纯 Python 解析、去重、计价和配置读写。
- `prices.json`：模型价格配置。
- `test_ledger.py`：回归测试。
- `启动.bat`：Windows 无控制台启动。
