---
name: three-stage-workflow
title: 三阶段工作流与调试原则
description: 三阶段工作流：GUI录制→out/医院→complete.py生成，调试先改skill
metadata:
  type: project
---

## 三阶段工作流

项目有三个产出阶段：

1. **GUI 录制（main_gui.py）** → 输出到 `out/{医院名}/processed_script_{医院名}.py`，包含 marker 占位注释
2. **补全生成（agent.py）** → 读取 processed 脚本，加载 skill → LLM 填充 marker → 输出 `out/{医院名}/completed_{医院名}.py`
3. **Skill 调试循环** → 如果 complete.py 不合理，先更新 `skills/{skill-name}/`（SKILL.md / references），再重新运行 agent.py 生成，不要直接手改 complete.py

**为什么：** skill 是所有医院的共享知识库。直接改 complete.py 只修了一个医院的问题，同类型的 skill 缺陷在其他医院会再次出现。skill 更新后重新运行 agent.py 即可为所有受影响医院一次性生成正确代码。

**产出物命名规范：**
- processed 脚本: `processed_script_{hospital}.py`
- completed 脚本: `completed_{hospital}.py`
- 画布截图: `canvas_frame_{序号}_{时间戳}.jpeg` → `canvas_frames/`
- Meta JSON: `dicom_meta_{时间戳}.json`

**agent.py 用法：**
```bash
D:/Anaconda/envs/codegen-marker/python.exe agent.py out/cxhospital/processed_script_cxhospital.py -o out/cxhospital/completed_cxhospital.py
```
