---
name: vl-config
description: VL 模型配置与调用模板。提供统一的 vl_config.json 配置（url / model）和可执行调用脚本 call_vl.py，VL_API_KEY 从环境变量读取。被 marker-* skill（序列选择、Meta 提取等）作为通用 VL 调用后端使用。当其他 skill 需要调用视觉/语言模型时触发本 skill。
---

# VL 模型配置与调用

## 文件结构

```
vl-config/
├── SKILL.md
├── vl_config.json           ← 配置：URL、模型、各任务的 system prompt
└── scripts/
    └── call_vl.py           ← 通用调用脚本（OpenAI 兼容 API）
```

## 配置方式

### 1. 环境变量（必需）

```bash
set VL_API_KEY=sk-your-api-key
set VL_API_BASE_URL=https://your-proxy.com/v1   # 可选，覆盖 vl_config.json
set VL_MODEL=gpt-4o                               # 可选，覆盖默认模型
```

### 2. 配置文件 `vl_config.json`

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `base_url` | string | API 端点根 URL（默认 `https://api.openai.com/v1`） |
| `default_model` | string | 默认模型名 |
| `tasks.<name>.model` | string | 该任务使用的模型（覆盖 default） |
| `tasks.<name>.system_prompt` | string | 该任务的 system prompt |
| `http_timeout` | int | 请求超时秒数（默认 120） |

### 3. 预置任务类型

| task 名 | 用途 | 输入 |
|---|---|---|
| `sequence_list` | 序列选择（报告+列表 → VL 决策） | 序列列表 + 报告 meta |
| `series_extract` | 截图识别帧数 | 序列列表截图 |
| `meta_extract` | Meta 面板提取 DICOM tag | Meta 面板截图 |
| `generic_chat` | 通用文本对话 | 自定义 prompt |

在 `vl_config.json` 中修改 `tasks.<name>.system_prompt` 即可调优每个任务的 prompt。

## 调用方式

### 命令行调用

```bash
# 用 conda 环境
D:/Anaconda/envs/codegen-marker/python.exe skills/vl-config/scripts/call_vl.py ^
    --task sequence_list --input vl_input_sequence_list_*.json
```

### 从 Python 代码调用

```python
import subprocess, json

result = subprocess.run(
    ["D:/Anaconda/envs/codegen-marker/python.exe",
     "skills/vl-config/scripts/call_vl.py",
     "--task", "series_extract",
     "--image", "screenshot.png",
     "--input", "vl_input.json"],
    capture_output=True, text=True, check=True,
)
output = json.loads(result.stdout)
```

### 与 `completed_cx.py` 集成

将原文件交互模式的 `_vl_decide_best_sequence` / `_vl_extract_sequences_from_screenshot`
中的打印+轮询代码替换为 `subprocess.run(call_vl.py)`：

```python
def _call_vl(task: str, input_path: str, image_path: str | None = None) -> dict:
    cmd = [
        "D:/Anaconda/envs/codegen-marker/python.exe",
        "skills/vl-config/scripts/call_vl.py",
        "--task", task,
        "--input", input_path,
    ]
    if image_path:
        cmd += ["--image", image_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)
```

## 环境变量优先级

```
VL_API_KEY > 无 → 报错退出
VL_API_BASE_URL > vl_config.json base_url
VL_MODEL > tasks.<name>.model > vl_config.json default_model
VL_CONFIG_PATH > scripts/../vl_config.json > cwd/vl_config.json
```

## 添加新任务

编辑 `vl_config.json` 的 `tasks` 段追加一个条目：

```json
"new_task": {
  "model": "gpt-4o",
  "system_prompt": "你的新任务 prompt..."
}
```

然后在其他 skill 中通过 `--task new_task` 调用即可。
