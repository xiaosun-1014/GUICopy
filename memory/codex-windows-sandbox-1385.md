---
name: codex-windows-sandbox-1385
description: cc 调 codex MCP 报 CreateProcessWithLogonW 1385 的根因与修复（windows.sandbox=elevated 不可用，改 unelevated）
metadata:
  type: project
---

# cc 调 codex 报 `CreateProcessWithLogonW failed: 1385` 的根因与修复

## 现象
cc 里通过 MCP 调 codex 时，codex MCP server 本体能启动（工具可见可调），但 codex 会话内**执行任何 shell 命令/读源码**都失败，报：
`windows sandbox failed: CreateProcessWithLogonW failed: 1385`

## 根因
- `1385 = ERROR_LOGON_FAILURE`（登录失败），只在调用 `CreateProcessWithLogonW`（以指定凭证创建进程）时出现。
- codex 在 Windows 上用「受限令牌（restricted token）」跑每条命令（`codex sandbox` 帮助第一行：*"run ... under Windows restricted token sandbox"*）。
- `C:\Users\jincheng.sun\.codex\config.toml` 里 `[windows] sandbox = "elevated"` → 提权需要**重新登录** → 走 `CreateProcessWithLogonW` → 本机登录失败 1385。
- 这台机器有企业管控软件（EsafeNet Cobra DocGuard / UniAccessAgent / LVUAAgent，见 `.codex\sandbox.*.log` 的 `world-writable scan FAILED` 审计），拦截了提权登录。
- **与 MCP 配置、Claude 沙箱、代码无关**，是 codex 自己 Windows 沙箱起子进程的登录问题。

## 修复
`config.toml`：
```toml
[windows]
sandbox = "unelevated"   # 受限令牌、不重新登录 → 绕开 1385
```
合法取值只有 `elevated` / `unelevated`（报错明确给出）。改完必须**重启 cc / 终端**让 MCP server 重新读配置。

## 验证
改前 `codex sandbox echo hello` → 1385 挂；改后 1385 消失，命令可执行。
备份保留在 `config.toml.bak-elevated`。

## Why:
用户用 cc 调 codex 协同工作，卡在此错误会误判为 MCP 或代码问题，实际是环境级沙箱配置，值得固化避免重查。

## How to apply:
再遇 1385，先查 `~/.codex/config.toml` 的 `[windows] sandbox`，确认是不是 `elevated`，改成 `unelevated` 并重启会话即可。
