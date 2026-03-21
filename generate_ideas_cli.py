# generate_ideas_cli.py
import json
import os
import random
import time
import asyncio
import chainlit as cl

from llm import LLMAgent
from cli_async_basic import WorkspaceManager
from utils import format_search_results_and_update_map, process_papers_to_read, read_knowledge_base, process_files_to_read

# 从 prompts.py 导入所有 Idea 阶段的完整提示词
from prompts import (
    IDEA_GENERATOR_SYSTEM_PROMPT,
    IDEA_GENERATOR_FIRST_PROMPT,
    IDEA_GENERATOR_ITERATION_PROMPT,
    NOVELTY_CHECK_SYSTEM_PROMPT,
    NOVELTY_CHECK_EVAL_PROMPT,
    IDEA_REFINER_SYSTEM_PROMPT,
    IDEA_REFINER_START_PROMPT,
    IDEA_REFINER_ITERATION_PROMPT,
)


# ==========================================
# 1. Student Agent: 生成 Idea
# ==========================================
async def run_student_agent(student_id, theme, max_iters, model, log_dir, search_params, local_context, workspace):
    agent = LLMAgent(model=model, log_file=os.path.join(log_dir, f"log_student_{student_id}.log"))
    agent.set_context_len(4)
    kb_txt_path = os.path.join(log_dir, f"kb_student_{student_id}.txt")
    doi_url_map = {}
    current_prompt = IDEA_GENERATOR_FIRST_PROMPT.format(theme=theme, local_context=local_context)
    current_ideas = []

    async with cl.Step(name=f"👨‍🎓 Student {student_id} 正在构思", type="run") as parent_step:
        for i in range(max_iters):
            async with cl.Step(name=f"Iteration {i+1}/{max_iters}") as step:
                step.output = "正在思考并生成JSON..."
                try:
                    response, _ = await agent.get_response_stream_async(current_prompt, IDEA_GENERATOR_SYSTEM_PROMPT)
                except:
                    continue
                parsed_json = LLMAgent.robust_extract_json(response)

                if not parsed_json:
                    step.output = "⚠️ JSON解析失败，重试中..."
                    current_prompt = "你的输出不符合JSON格式要求，请修正并重新输出。"
                    continue

                step.output = f"**Thoughts:** {parsed_json.get('Thoughts', '')}\n```json\n{json.dumps(parsed_json, indent=2, ensure_ascii=False)}\n```"

                queries = parsed_json.get("SearchQueries", [])
                papers_to_read = parsed_json.get("PapersToRead", [])
                files_to_read = parsed_json.get("FilesToRead", [])
                ideas = parsed_json.get("Ideas", [])
                if ideas:
                    current_ideas = ideas

                if "i'm done" in parsed_json.get("Thoughts", "").lower():
                    break

                if i < max_iters - 1:
                    if papers_to_read:
                        async with cl.Step(name="📄 正在下载并精读文献全文..."):
                            await cl.make_async(process_papers_to_read)(papers_to_read, doi_url_map, kb_txt_path)
                            
                    if files_to_read:
                        async with cl.Step(name="📄 正在读取本地指定文件..."):
                            await cl.make_async(process_files_to_read)(files_to_read, kb_txt_path, workspace_dir=workspace)

                    search_feedback = await cl.make_async(format_search_results_and_update_map)(queries, doi_url_map, **search_params)
                    kb_content = await cl.make_async(read_knowledge_base)(kb_txt_path)

                    current_prompt = IDEA_GENERATOR_ITERATION_PROMPT.format(
                        search_results=search_feedback, knowledge_base=kb_content,
                        previous_ideas=json.dumps(current_ideas, indent=2, ensure_ascii=False)
                    )
        parent_step.output = f"✅ 完成构思，共生成 {len(current_ideas)} 个 Idea。"
    return current_ideas


# ==========================================
# 2. Teacher Agent: 新颖性检查 (Novelty Checker)
# ==========================================
async def run_teacher_agent(teacher_id, idea, max_iters, model, log_dir, search_params):
    """Novelty Check Agent 的运行逻辑（Chainlit 异步版）"""
    agent = LLMAgent(model=model, log_file=os.path.join(log_dir, f"log_teacher_{teacher_id}.log"))

    kb_txt_path = os.path.join(log_dir, f"kb_teacher_{teacher_id}.txt")
    doi_url_map = {}

    search_feedback = "目前尚未进行任何搜索。"
    final_score = None
    review_comments = ""

    async with cl.Step(name=f"👨‍🏫 Teacher {teacher_id} 正在审查: {idea.get('Title', 'N/A')}...", type="run") as parent_step:
        for i in range(max_iters):
            async with cl.Step(name=f"Review Iter {i+1}/{max_iters}") as step:
                kb_content = await cl.make_async(read_knowledge_base)(kb_txt_path)

                current_prompt = NOVELTY_CHECK_EVAL_PROMPT.format(
                    title=idea.get('Title', ''),
                    background=idea.get('Background', ''),
                    hypothesis=idea.get('Hypothesis', ''),
                    methodology=idea.get('Methodology', ''),
                    search_results=search_feedback,
                    knowledge_base=kb_content,
                )
                try:
                    response, _ = await agent.get_response_stream_async(current_prompt, NOVELTY_CHECK_SYSTEM_PROMPT)
                except:
                    continue
                parsed_json = LLMAgent.robust_extract_json(response)

                if not parsed_json:
                    step.output = "⚠️ JSON解析失败"
                    search_feedback = "请严格按照要求的JSON格式输出你的评估和搜索Query。"
                    continue

                decision = parsed_json.get("Decision", "Pending")
                final_score = parsed_json.get("Score")
                review_comments = parsed_json.get("Thoughts", "")
                step.output = f"**Decision:** {decision} | **Score:** {final_score}\n**Thoughts:** {review_comments}..."

                if decision == "Finished":
                    break

                # 处理全文精读请求
                papers_to_read = parsed_json.get("PapersToRead", [])
                if papers_to_read:
                    await cl.make_async(process_papers_to_read)(papers_to_read, doi_url_map, kb_txt_path)

                # 执行新的检索更新 Feedback
                queries = parsed_json.get("SearchQueries", [])
                search_feedback = await cl.make_async(format_search_results_and_update_map)(
                    queries, doi_url_map, **search_params
                )

        parent_step.output = f"✅ 审查完毕 | Score: {final_score}"

    return {
        "Idea": idea,
        "Reviewer_ID": teacher_id,
        "Score": final_score,
        "Review_Comments": review_comments
    }


# ==========================================
# 3. Refiner Agent: 根据用户指令修改 Idea
# ==========================================
async def refine_idea(idea, user_instructions, allow_search, max_iters, model, log_dir, search_params):
    """根据用户指令Refine特定的Idea (Chainlit 异步版)"""
    refine_id = int(time.time())
    agent = LLMAgent(model=model, log_file=os.path.join(log_dir, f"log_refiner_{refine_id}.log"))
    agent.set_context_len(4)

    kb_txt_path = os.path.join(log_dir, f"kb_refiner_{refine_id}.txt")
    doi_url_map = {}

    current_idea = idea
    current_prompt = IDEA_REFINER_START_PROMPT.format(
        current_idea=json.dumps(current_idea, indent=2, ensure_ascii=False),
        user_feedback=user_instructions
    )

    async with cl.Step(name=f"🔧 Refiner 正在修改 Idea: {idea.get('Name', 'N/A')}", type="run") as parent_step:
        for i in range(max_iters):
            async with cl.Step(name=f"Refine Iter {i+1}/{max_iters}") as step:
                response, _ = await agent.get_response_stream_async(current_prompt, IDEA_REFINER_SYSTEM_PROMPT)
                parsed_json = LLMAgent.robust_extract_json(response)

                if not parsed_json:
                    step.output = "⚠️ JSON解析失败，重试中..."
                    current_prompt = "你的输出不符合JSON格式要求，请修正并重新输出。"
                    continue

                thoughts = parsed_json.get("Thoughts", "")
                queries = parsed_json.get("SearchQueries", [])
                papers_to_read = parsed_json.get("PapersToRead", [])
                refined_ideas_list = parsed_json.get("Ideas", [])

                if refined_ideas_list and len(refined_ideas_list) > 0:
                    current_idea = refined_ideas_list[0]

                step.output = f"**Thoughts:** {thoughts}...\n```json\n{json.dumps(current_idea, indent=2, ensure_ascii=False)}\n```"

                # 处理搜索和阅读逻辑
                search_feedback = "用户未开启搜索权限或本轮未进行搜索。"
                kb_content = "暂无新笔记。"

                if allow_search:
                    if papers_to_read:
                        await cl.make_async(process_papers_to_read)(papers_to_read, doi_url_map, kb_txt_path)
                    if queries:
                        search_feedback = await cl.make_async(format_search_results_and_update_map)(
                            queries, doi_url_map, **search_params
                        )
                    kb_content = await cl.make_async(read_knowledge_base)(kb_txt_path)

                current_prompt = IDEA_REFINER_ITERATION_PROMPT.format(
                    search_results=search_feedback,
                    knowledge_base=kb_content,
                    previous_idea=json.dumps(current_idea, indent=2, ensure_ascii=False)
                )

        parent_step.output = f"✅ Refine 完成"
    return current_idea


# ==========================================
# 4. 主控工作流 (完整交互逻辑)
# ==========================================
async def run_ideas_workflow(workspace_dir, user_request, settings):
    log_dir = os.path.join(workspace_dir, "log")
    idea_dir = os.path.join(workspace_dir, "idea")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(idea_dir, exist_ok=True)
    model = settings.get("orchestrator_model", "gemini-3-flash-preview")
    student_run_iterations = settings.get("max_idea_generator_iterations",5)
    teacher_review_iterations = settings.get("max_idea_review_iterations",5)
    search_params = {"open_access": True, "has_pdf_url": True, "from_year": 2020}

    # 读取本地环境
    local_context = WorkspaceManager.get_workspace_state_recursive(workspace_dir, 50)

    await cl.Message(content=f"💡 **开始执行 Idea 生成阶段**\n正在扫描本地目录作为先验知识...\n主题: {user_request}").send()

    # ======================================
    # 阶段一：并行启动 Student Agents 生成 Ideas
    # ======================================
    student_tasks = [
        run_student_agent(i + 1, user_request, student_run_iterations, model, log_dir, search_params, local_context, workspace_dir)
        for i in range(2)
    ]
    student_results = await asyncio.gather(*student_tasks)

    all_ideas = []
    for ideas in student_results:
        all_ideas.extend(ideas)

    if not all_ideas:
        await cl.Message(content="❌ 未生成任何Idea。").send()
        return

    # 保存所有初步生成的 idea
    all_ideas_path = os.path.join(idea_dir, "all_ideas_raw.json")
    with open(all_ideas_path, "w", encoding="utf-8") as f:
        json.dump(all_ideas, f, indent=4, ensure_ascii=False)

    # 打乱顺序
    random.shuffle(all_ideas)

    # ======================================
    # 阶段二：并行启动 Teacher Agents 评审新颖性
    # ======================================
    await cl.Message(content=f"👨‍🏫 **进入阶段二：Novelty Check (并发严苛审稿)**\n共 {len(all_ideas)} 个 Idea 等待审查...").send()
    
    teacher_tasks = [
        run_teacher_agent(idx + 1, idea, teacher_review_iterations, model, log_dir, search_params)
        for idx, idea in enumerate(all_ideas)
    ]
    evaluated_results = await asyncio.gather(*teacher_tasks)

    # 建立评审映射表
    review_map = {}
    for res in evaluated_results:
        review_map[id(res['Idea'])] = {
            "score": res["Score"],
            "comments": res["Review_Comments"]
        }

    # 准备显示列表
    display_list = []
    for idx, idea in enumerate(all_ideas):
        review_info = review_map.get(id(idea))
        if review_info:
            score = review_info['score']
            comments = review_info['comments']
        else:
            score = "未评审"
            comments = "该 Idea 尚未被 Teacher Agent 评审。"

        display_list.append({
            "id": idx + 1,
            "idea": idea,
            "score": score,
            "comments": comments
        })

    # 保存评审结果
    review_log_path = os.path.join(log_dir, "idea_review_results.json")
    with open(review_log_path, "w", encoding="utf-8") as f:
        json.dump([{
            "id": d["id"],
            "title": d["idea"].get("Title", ""),
            "score": d["score"],
            "comments": d["comments"]
        } for d in display_list], f, indent=4, ensure_ascii=False)

    # ======================================
    # 阶段三：交互式选择循环
    # ======================================
    while True:
        # 展示所有 Ideas 的完整信息
        md_content = f"### 📊 生成的 Ideas 汇总 (共 {len(display_list)} 个)\n\n"
        for item in display_list:
            idea = item['idea']
            md_content += f"---\n#### 📌 [{item['id']}] {idea.get('Title', 'No Title')} (Score: {item['score']})\n"
            md_content += f"**Name:** {idea.get('Name', 'N/A')}\n\n"
            md_content += f"**Background:** {idea.get('Background', '')}...\n\n"
            md_content += f"**Hypothesis:** {idea.get('Hypothesis', '')}...\n\n"
            md_content += f"**Methodology:** {idea.get('Methodology', '')}...\n\n"
            if item['score'] != "未评审":
                md_content += f"**📝 Review:** {item['comments']}...\n\n"

        await cl.Message(content=md_content).send()

        # 用户选择
        res = await cl.AskUserMessage(
            content="👉 **请输入你想进一步处理的 Idea 编号 (数字)**，或输入 `q` 退出:",
            timeout=3600
        ).send()

        if not res:
            await cl.Message(content="⏳ 超时未响应，默认采用第一个Idea。").send()
            selected_item = display_list[0]
        else:
            user_input = res['output'].strip()
            if user_input.lower() == 'q':
                await cl.Message(content="👋 用户退出Idea选择。").send()
                return

            try:
                selected_idx = int(user_input) - 1
                if not (0 <= selected_idx < len(display_list)):
                    await cl.Message(content="⚠️ 编号无效，请重新选择。").send()
                    continue
                selected_item = display_list[selected_idx]
            except ValueError:
                await cl.Message(content="⚠️ 请输入有效数字。").send()
                continue

        # ======================================
        # 子循环：选中 Idea 的详细操作
        # ======================================
        current_idea = selected_item['idea']

        while True:
            # 展示选中 Idea 的完整信息
            detail_md = f"### 🔍 当前选中: Option [{selected_item['id']}]\n\n"
            detail_md += f"```json\n{json.dumps(current_idea, indent=4, ensure_ascii=False)}\n```\n\n"
            detail_md += f"**📝 评审意见:** {selected_item['comments']}\n\n"
            detail_md += f"**⭐ 评分:** {selected_item['score']}\n"
            await cl.Message(content=detail_md).send()

            action_res = await cl.AskActionMessage(
                content="请选择操作：",
                actions=[
                    cl.Action(name="confirm_idea", payload={"value": "y"}, label="✅ 满意此版本 (保存并继续)"),
                    cl.Action(name="refine_idea", payload={"value": "n"}, label="🔧 进入修改/Refine模式"),
                    cl.Action(name="back_to_list", payload={"value": "b"}, label="🔙 返回Idea列表"),
                    cl.Action(name="quit_flow", payload={"value": "q"}, label="🚪 退出程序"),
                ],
                timeout=3600
            ).send()

            if not action_res:
                action_val = "y"  # 超时默认确认
            else:
                payload = action_res.get("payload", {})
                action_val = payload.get("value", "y")
                # action_val = action_res.get("value", "y")

            if action_val == "q":
                await cl.Message(content="👋 用户退出。").send()
                return

            elif action_val == "b":
                break  # 跳出子循环，回到列表展示

            elif action_val == "y":
                # 保存最终选定的 Idea
                final_path = os.path.join(idea_dir, "selected_idea.json")
                with open(final_path, "w", encoding="utf-8") as f:
                    json.dump(current_idea, f, indent=4, ensure_ascii=False)
                await cl.Message(content=f"🎉 **最终选定的 Idea 已保存至:** `{final_path}`").send()
                return

            elif action_val == "n":
                # 进入 Refine 流程
                instructions_res = await cl.AskUserMessage(
                    content="📝 **请输入你的具体修改指令** (例如: '增加对低轨卫星场景的考虑'):",
                    timeout=600
                ).send()

                if not instructions_res or not instructions_res['output'].strip():
                    await cl.Message(content="⚠️ 指令为空，取消修改。").send()
                    continue

                instructions = instructions_res['output'].strip()

                # 询问是否允许搜索文献
                search_res = await cl.AskActionMessage(
                    content="允许 Refiner Agent 搜索新文献吗?",
                    actions=[
                        cl.Action(name="allow_search", payload={"value": "yes"}, label="✅ 允许搜索"),
                        cl.Action(name="no_search", payload={"value": "no"}, label="❌ 仅使用内部知识"),
                    ],
                    timeout=120
                ).send()
                
                
                allow_search_payload = search_res.get("payload", {})
                allow_search = (allow_search_payload.get("value", "yes") == "yes")
                #allow_search = (search_res and search_res.get("value") == "yes")

                await cl.Message(
                    content=f"🔧 **正在启动 Refiner Agent** 对 Idea 进行优化 (Search={allow_search})..."
                ).send()

                try:
                    refined_idea_result = await refine_idea(
                        idea=current_idea,
                        user_instructions=instructions,
                        allow_search=allow_search,
                        max_iters=3,
                        model=model,
                        log_dir=log_dir,
                        search_params=search_params
                    )
                    # 更新内存中的 Idea
                    current_idea = refined_idea_result
                    selected_item['idea'] = current_idea

                    # 自动备份
                    refined_filename = f"refined_idea_{selected_item['id']}_{int(time.time())}.json"
                    refined_path = os.path.join(idea_dir, refined_filename)
                    with open(refined_path, "w", encoding="utf-8") as f:
                        json.dump(current_idea, f, indent=4, ensure_ascii=False)

                    await cl.Message(content=f"✅ **修改完成！** 新版本已保存至 `{refined_path}`\n请查看上方的新版本内容。").send()

                except Exception as e:
                    await cl.Message(content=f"❌ Refine 过程出错: {e}").send()