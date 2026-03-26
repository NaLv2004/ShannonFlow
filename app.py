# app.py
import os
import json
import asyncio
import subprocess
import time
import chainlit as cl
from chainlit.input_widget import Select, TextInput, Slider
import platform

# 从各个重构的模块中导入入口函数
from generate_ideas_cli import run_ideas_workflow
from generate_code import run_generate_code
from perform_experiments import run_experiments
from perform_writeup import perform_writeup_workflow
from review import run_review_workflow
from update_from_reviews import run_update_from_reviews

from cli_async_basic import ChainlitUI

PHASES = ["1_Generate_Ideas", "2_Generate_Code","3_Perform_Experiments","4_Writeup"]

PHASE_INFO = {
    "1_Generate_Ideas":       {"emoji": "💡", "name": "Idea 生成",     "desc": "多个 Student Agent 并行构思 → Teacher Agent 新颖性审查 → 交互式选择与精炼"},
    "2_Generate_Code":        {"emoji": "💻", "name": "代码编写",     "desc": "Orchestrator 指挥 Coder Agent 根据选定 Idea 编写Python仿真代码并自测"},
    "3_Perform_Experiments":  {"emoji": "🔬", "name": "实验执行",     "desc": "自动扫参（SNR、天线数等）获取多组对比数据，支持并发运行"},
    "4_Writeup":              {"emoji": "📄", "name": "论文撰写",     "desc": "自动生成 IEEE 格式 LaTeX 论文，含引言、系统模型、仿真结果等"},
    "5_Review":               {"emoji": "📝", "name": "AI 审稿",      "desc": "PDF初审 + 代码-论文一致性深度审核，输出Major/Minor Comments"},
    "6_Update_From_Reviews":  {"emoji": "🔧", "name": "论文修改",     "desc": "根据审稿人意见自动修改代码、获取新数据、重写 tex 论文"},
}


# ==========================================
# GPU 监控工具
# ==========================================
def get_gpu_status_brief():
    """获取简洁的 GPU 显存状态"""
    try:
        output = subprocess.check_output(
            "nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits",
            shell=True, encoding="utf-8", errors="replace", timeout=5
        ).strip()
        lines = output.split("\n")
        result = ""
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                idx, name, mem_used, mem_total, gpu_util = parts[0], parts[1], parts[2], parts[3], parts[4]
                result += f"GPU{idx} ({name}): {mem_used}/{mem_total} MB ({gpu_util}% util)\n"
        return result.strip() if result else "未检测到 GPU"
    except Exception:
        return "GPU 状态获取失败"


# ==========================================
# 任务仪表盘后台协程
# ==========================================
async def task_dashboard_loop():
    """后台协程：定期更新任务仪表盘消息"""
    # 创建一个固定的仪表盘消息
    dashboard_msg = cl.Message(content="⏳ **任务仪表盘初始化中...**")
    await dashboard_msg.send()
    dashboard_id = dashboard_msg.id

    while True:
        try:
            await asyncio.sleep(15)
            
            # 检查是否还在运行
            if not cl.user_session.get("is_running"):
                await dashboard_msg.remove()
                break

            agent_system = cl.user_session.get("agent_system")

            # 构建仪表盘内容
            lines = ["## 📊 实时任务仪表盘\n"]

            # GPU 状态
            gpu_info = await cl.make_async(get_gpu_status_brief)()
            lines.append(f"### 🖥️ GPU 资源\n```\n{gpu_info}\n```\n")

            # 当前轮次信息
            if agent_system:
                lines.append(f"**当前轮次:** {agent_system.rounds} / {agent_system.max_rounds}\n")

                # 运行中任务
                active = agent_system.task_manager.get_active_tasks()
                if active:
                    lines.append("### ⚡ 运行中任务\n")
                    lines.append("| 任务ID | 类型 | 运行时长 | 最新日志 |\n|--------|------|----------|----------|\n")
                    for tid, task in active.items():
                        elapsed = int(time.time() - task.start_time)
                        last_log = "".join(list(task.log_history)[-3:]).strip().replace("\n", " ")[:80]
                        lines.append(f"| `{tid}` | {task.task_type} | {elapsed}s | {last_log} |\n")
                else:
                    lines.append("### ⚡ 运行中任务\n无\n")

                # 最近动作
                if agent_system.action_history:
                    last_action = agent_system.action_history[-1]
                    lines.append(f"\n**最近动作:** `{last_action.get('action', 'N/A')}`\n")
            else:
                lines.append("*Agent 尚未初始化*\n")

            lines.append(f"\n---\n*上次更新: {time.strftime('%H:%M:%S')}*")
            
            new_content = "".join(lines)
            dashboard_msg.content = new_content
            await dashboard_msg.update()

        except asyncio.CancelledError:
            try:
                await dashboard_msg.remove()
            except:
                pass
            break
        except Exception:
            await asyncio.sleep(10)


# ==========================================
# Chainlit 回调
# ==========================================
@cl.action_callback("select_workspace")
async def on_action(action):
    folder = ChainlitUI.select_directory()
    if folder:
        cl.user_session.set("workspace_dir", folder)
        await cl.Message(content=f"✅ 工作空间已设置为: `{folder}`\n请在 Settings 配置后发送消息开始！").send()
    else:
        await cl.Message(content="⚠️ 未选择任何文件夹。").send()


@cl.on_chat_start
async def start():
    cl.user_session.set("user_interrupt_requests", [])
    cl.user_session.set("interrupt_event", asyncio.Event())
    cl.user_session.set("workspace_dir", os.path.abspath("./my_research_workspace"))
    cl.user_session.set("is_running", False)
    cl.user_session.set("agent_system", None)
    cl.user_session.set("dashboard_task", None)

    await cl.ChatSettings([
        Select(id="start_phase", label="选择起始阶段", values=PHASES, initial_index=0),
        TextInput(id="request_file", label="指定需求文件名", initial="request.txt"),
        TextInput(id="orchestrator_model", label="科研组织者大模型 (Orchestrator)", initial="gemini-3.1-pro-preview"),
        TextInput(id="coder_model", label="编程大模型 (Coder)", initial="gemini-3.1-pro-preview"),
        Select(id="env_type", label="环境类型", values=["None", "Conda", "Venv"], initial_index=1),
        TextInput(id="env_name_or_path", label="环境名/路径", initial="AutoGenOld"),
        Select(id="plan_mode", label="计划拆解模式 (Plan Mode)", values=["True", "False"], initial_index=0),
        Slider(id="max_concurrent_tasks", label="最大并发任务数", initial=3, min=1, max=10, step=1),
        Slider(id="max_rounds", label="管家最大执行轮次", initial=500, min=10, max=1000, step=10),
        Slider(id="max_idea_generator_iterations",label="Idea 生成器最大迭代次数",initial=10,min=3,max=20),
        Slider(id="max_idea_review_iterations",label="Idea 审查器最大迭代次数",initial=10,min=3,max=20)
    ]).send()

    # 欢迎页面
    banner = """
# 🌌 Aether:全自动通信 AI 科研管家

> 从灵感到论文，端到端自动化科研工作流

---
"""
    
    phases_table = "### 🗺️ 工作流全景\n\n"
    phases_table += "| 阶段 | 功能 | 说明 |\n|------|------|------|\n"
    for phase_key, info in PHASE_INFO.items():
        phases_table += f"| {info['emoji']} | **{info['name']}** | {info['desc']} |\n"

    instructions = """
---
### 🚀 快速开始
1. **设置工作空间** — 点击下方按钮选择本地文件夹（或使用默认路径）
2. **配置参数** — 点击左侧 ⚙️ Settings 面板调整模型、环境、起始阶段等
3. **发送指令** — 输入您的**科研主题**或**任务描述**，系统将自动启动工作流

> 💡 **运行中追加指令**: 工作流运行时发送新消息可实时注入指令给 Orchestrator
"""

    workspace_path = cl.user_session.get("workspace_dir")
    status_line = f"\n📂 **当前工作空间:** `{workspace_path}`\n"

    actions = [cl.Action(name="select_workspace", payload={"value": "select"}, label="📁 选择工作空间文件夹")]
    await cl.Message(content=banner + phases_table + instructions + status_line, actions=actions).send()


@cl.on_settings_update
async def setup_agent(settings):
    if "plan_mode" in settings:
        settings["plan_mode"] = True if settings["plan_mode"] == "True" else False
    cl.user_session.set("settings", settings)
    await cl.Message(content="✅ 系统参数已更新！").send()


@cl.on_message
async def main_message(message: cl.Message):
    if cl.user_session.get("is_running"):
        cl.user_session.get("user_interrupt_requests").append(message.content)
        event = cl.user_session.get("interrupt_event")
        if event: event.set()
        await cl.Message(content="📥 **已接收最新指令！** 正在通知当前运行的智能体...").send()
        return

    cl.user_session.set("is_running", True)
    workspace = cl.user_session.get("workspace_dir")
    settings = cl.user_session.get("settings", {})
    start_phase = settings.get("start_phase", PHASES[0])
    start_idx = PHASES.index(start_phase)

    # 1. 自动创建所需目录
    subdirs = ["log", "reference", "paper", "data", "idea"]
    for d in subdirs:
        os.makedirs(os.path.join(workspace, d), exist_ok=True)
    await cl.Message(content=f"📁 工作区目录已就绪: `{workspace}`").send()

    # 2. 读取用户需求
    user_request = message.content
    req_file = os.path.join(workspace, settings.get("request_file", "request.txt"))
    if os.path.exists(req_file):
        with open(req_file, 'r', encoding='utf-8') as f:
            user_request += f"\n\n[来自 {req_file} 的需求]:\n" + f.read()

    # 3. 启动任务仪表盘
    dashboard_task = asyncio.create_task(task_dashboard_loop())
    cl.user_session.set("dashboard_task", dashboard_task)

    # 4. 状态机：依次执行各阶段
    try:
        for phase in PHASES[start_idx:]:
            info = PHASE_INFO.get(phase, {})
            emoji = info.get("emoji", "🔹")
            name = info.get("name", phase)
            desc = info.get("desc", "")
            
            await cl.Message(content=f"## {emoji} 进入阶段: {name}\n> {desc}").send()
            
            if phase == "1_Generate_Ideas":
                await run_ideas_workflow(workspace, user_request, settings)
            elif phase == "2_Generate_Code":
                await run_generate_code(workspace, user_request, settings)
            elif phase == "3_Perform_Experiments":
                await run_experiments(workspace, user_request, settings)
            elif phase == "4_Writeup":
                await perform_writeup_workflow(
                    workspace_dir=workspace,
                    settings=settings,
                    interrupt_event=cl.user_session.get("interrupt_event"),
                    user_interrupt_requests=cl.user_session.get("user_interrupt_requests"),
                    request_text=user_request
                )
                await cl.Message(content="✅ 论文生成完毕！").send()
            elif phase == "5_Review":
                await run_review_workflow(workspace, settings)
            elif phase == "6_Update_From_Reviews":
                await run_update_from_reviews(workspace, user_request, settings)

            res = await cl.AskActionMessage(
                content=f"✅ 阶段 **{name}** 执行完毕。是否继续进入下一阶段？",
                actions=[
                    cl.Action(name="continue", payload={"value": "yes"}, label="▶️ 继续下一阶段"),
                    cl.Action(name="stop", payload={"value": "no"}, label="⏸️ 暂停工作流")
                ], timeout=3600
            ).send()
            if res and res.get("value") == "no":
                break

    except Exception as e:
        await cl.Message(content=f"❌ 工作流异常中断: {e}").send()
    finally:
        # 取消仪表盘
        dashboard = cl.user_session.get("dashboard_task")
        if dashboard and not dashboard.done():
            dashboard.cancel()
        
        cl.user_session.set("is_running", False)
        cl.user_session.set("agent_system", None)
        await cl.Message(content="🏁 **工作流已安全退出。**").send()