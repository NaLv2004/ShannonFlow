import json
import os

class PromptBuilder:
    def __init__(self, schemas_path="tools_schema.json"):
        base_dir = os.path.dirname(__file__)
        schema_file = os.path.join(base_dir, schemas_path)
        with open(schema_file, 'r', encoding='utf-8') as f:
            self.tools_schema = json.load(f)

    def build_prompt(self, base_prompt, tool_names, custom_json_fields=None, append_text=""):
        tools_desc = ""
        json_params = {}
        for i, tool in enumerate(tool_names):
            if tool in self.tools_schema:
                schema = self.tools_schema[tool]
                tools_desc += f"{i+1}. `{tool}`: {schema['description']}\n"
                if 'params' in schema and schema['params']:
                    for k, v in schema['params'].items():
                        json_params[k] = f"如果是{tool}, {v}"
            else:
                tools_desc += f"{i+1}. `{tool}`: (未知工具)\n"
        
        json_template = {
            "Thoughts": "思考过程、进度分析等...",
            "Action": " | ".join(tool_names),
            "Action_Params": json_params,
            "summary": "无论当前的Action是什么，都必须提供该字段。其表示对前一个步骤所采取的行动的结果的总结（如果有数据，应包含数据以及对数据的评价）；当前步骤的详细摘要，包括进行的操作，进行操作的目的，操作的对象，或者运行的详细仿真场景，记录到的数据等"
        }
        
        if custom_json_fields:
            for k, v in custom_json_fields.items():
                json_template[k] = v
                
        # Build prompt
        res = base_prompt + "\n\n【你可以执行的操作（Tools）包括】：\n" + tools_desc
        res += "\n【交互格式】\n严格遵守JSON格式，每次只输出一个JSON（必须包含在 ```json 中）：\n"
        res += "```json\n" + json.dumps(json_template, ensure_ascii=False, indent=4) + "\n```\n"
        WRITE_TOOL_NAMES = ['WRITE_FILE','SUBMIT_CODE']
        write_tool = []
        for tool in WRITE_TOOL_NAMES:
            if tool in tool_names:
                write_tool.append(tool)
        if len(write_tool) > 0:
            res += f"""【注意】当 Action 为 {','.join(write_tool)} 时，请在 JSON 外用 markdown 代码块提供文件内容。
            提交新文件时必须严格遵循以下格式，包含每个文件的文件名和文件内容。
            ### File: main.py
            ```python
            print("Hello")
            ```
            """
        
        if append_text:
            res += "\n" + append_text
            
        return res

pb = PromptBuilder()


# directories where prompts are stored
DEFAULT_PATH = os.path.join('prompts','default')
EXECUTE_PATH = os.path.join('prompts','execute')
PLAN_PATH = os.path.join('prompts','plan')


with open (os.path.join(DEFAULT_PATH,'ORCHESTRATOR.md'),'r',errors='ignore',encoding='utf-8') as f:
    DEFAULT_ORCHESTRATOR_PROMPT_BASE  = f.read()
DEFAULT_ORCHESTRATOR_PROMPT = pb.build_prompt(
    DEFAULT_ORCHESTRATOR_PROMPT_BASE, 
    ["READ_FILE", "WRITE_FILE", "SEARCH_LITERATURE", "KILL_TASK", "WAIT", "FINISH", "RECORD_DATA", "FINISH_STEP", "SPAWN_CODER", "SPAWN_RUN", "FIND_TOOL", "MODIFY_CODE"],
    append_text="当且仅当 Action 为 WRITE_FILE 时，在 JSON 外用 markdown 代码块提供文件内容：\n### File: filename.txt\n```\n[内容]\n```"
)

with open (os.path.join(DEFAULT_PATH,'CODER.md'),'r',errors='ignore',encoding='utf-8') as f:
    DEFAULT_CODER_PROMPT_BASE = f.read()
DEFAULT_CODER_PROMPT = pb.build_prompt(
    DEFAULT_CODER_PROMPT_BASE,
    ["READ_FILE", "WRITE_FILE", "RUN_CODE", "MODIFY_CODE", "SUBMIT_CODE"],
    append_text="当 Action 为 WRITE_FILE 时，可在外部附带代码块：\n### File: main.py\n```python\nprint(\"Hello\")\n```"
)

with open (os.path.join(DEFAULT_PATH,'PLANNER.md'),'r',errors ='ignore',encoding='utf-8') as f:
    PLANNER_PROMPT_BASE = f.read()
PLANNER_PROMPT = PLANNER_PROMPT_BASE + "\n严格输出 JSON 格式：\n```json\n{\n    \"Plan\": [\n        \"第一步：分析xxx文件并编写yyy的测试代码\",\n        \"第二步：运行该测试代码，确保没有报错\"\n    ]\n}\n```\n"

with open (os.path.join(DEFAULT_PATH,'STUDENT_PLANNER.md'),'r',errors='ignore',encoding='utf-8') as f:
    STUDENT_PLANNER_PROMPT_BASE= f.read()
STUDENT_PLANNER_PROMPT = pb.build_prompt(
    STUDENT_PLANNER_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "SUBMIT_PLAN"]
)

with open (os.path.join(DEFAULT_PATH, 'CRITIC.md'),'r',errors='ignore',encoding='utf-8') as f:
    TEACHER_CRITIC_PROMPT_BASE = f.read()
TEACHER_CRITIC_PROMPT = pb.build_prompt(
    TEACHER_CRITIC_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "EVALUATE_PLAN"]
)

##################################### Idea Generation #####################################
IDEA_GENERATOR_SYSTEM_PROMPT = """你是一个充满雄心壮志且富有创造力的通信领域AI科学家。
你的目标是提出具有高度创新性、跨学科（或跨细分领域）的科研Idea。
请遵循以下原则：
1. 鼓励大胆假设，并充分考虑实际通信场景的复杂性（如信道衰落、硬件损伤、动态拓扑等）。
2. 提出的假设必须非常具体，避免假大空。
3. 鼓励进行广泛的文献调研。当你需要搜索文献时，请使用逻辑词或模糊查询词。
4. 你可以同时执行多项操作：生成新的Idea、优化（Refine）之前的Idea、发起新的文献搜索、选择阅读某篇文献的全文。
6. 生成新的idea时，需要在返回的结果中包含之前的所有idea。
7. 研究对象不应该过于复杂，不应该堆砌过多技术名词，而是要聚焦于某一个具体问题，给出原理性的创新。
8。每轮对话中，你都需要refine之前的idea（更加具体、可行、具有合理性），或者努力根据你已有的知识，或者搜索到的论文，提出新的insight和idea(或使用新的insights修改当前idea)。

如果当前搜索结果中的摘要不足以判断，你可以要求阅读全文。将你想要阅读的论文的 DOI 填入 "PapersToRead" 列表中。系统会自动下载、阅读并把核心总结返回给你。
如果需要阅读当前工作目录下的本地文件（如果之前已经下载或者存在于本地，也可以直接读取已下载好的.pdf文件或者文本文件），请将相对或者绝对文件路径填入 "FilesToRead" 列表中（例如 ["paper1.pdf", "code.py"]），系统会解析并将内容/读取后的总结补充进知识库。
此外，你需要通过阅读其他论文和摘要产生新的insights，而不是仅仅依据这些内容来鉴定创新性。

你的回复必须包含如下JSON格式（可以包含在 ```json 和 ``` 之间）：
```json
{
    "Thoughts": "这里写下你的思考过程、对当前idea的分析以及你接下来的计划。",
    "SearchQueries": ["query1", "query2"], 
    "PapersToRead": ["https://doi.org/10.xxxx/xxxx", "https://doi.org/10.yyyy/yyyy"],
    "FilesToRead": ["file1.txt", "file2.pdf"],
    "Ideas": [
        {
            "Name": "简短的Idea英文代号",
            "Title": "Idea的完整标题",
            "Background": "研究背景与动机",
            "Hypothesis": "具体的大胆假设",
            "Methodology": "具体的研究方法与实际复杂场景考量"
        }
    ]
}
```
SearchQueries 不可为空，Ideas字段不可以为空。如果不需要读全文，PapersToRead 和 FilesToRead 可为空列表。
"""

IDEA_GENERATOR_FIRST_PROMPT = """
我们正在探索以下粗略的研究主题：
【{theme}】

【本地工作区现有内容参考】
{local_context}

请根据该主题，提出你初步的想法和idea，并且给出一系列广泛的文献检索Query以帮助你构思。请输出符合系统提示词要求的JSON。
"""

IDEA_GENERATOR_ITERATION_PROMPT = """
这是你在上一轮中提交的文献搜索Query的结果：
{search_results}

这是你（或其它评审）之前要求精读的论文的全文总结笔记（知识库）：
{knowledge_base}

这是你之前已经生成的Ideas（供你参考，你可以选择Refine它们，或者提出全新的Idea）。注意，每次json中返回的Ideas列表中，至少要包含之前提出的所有Ideas，并且使之前生成的所有idea更加完备，详细,可实现；最好要追加新的idea，并将之前生成的多个idea进行有机整合，形成新的、更系统的、具有更多创新点（但又不是简单堆叠的）idea。
{previous_ideas}

请根据最新的文献结果和精读笔记，继续你的研究设想。如果发现你的Idea已经被前人做过，请大修你的假设。如果需要阅读新搜索出的文献全文，请填入 PapersToRead。
"""

NOVELTY_CHECK_SYSTEM_PROMPT = """你是一位顶尖通信学术会议（如 GLOBECOM, ICC）或知名学术期刊的资深审稿人 (Area Chair)。
你的任务是严格审查一个新提交的科研Idea是否具有真正的学术创新价值。
如果你怀疑某篇已发表的论文已经做过了这个 Idea，你可以将该论文的 DOI 放入 "PapersToRead" 列表中。
请在每轮回复中输出如下JSON：
```json
{
    "Thoughts": "你的审查思路、对Idea的评价或对文献搜索结果的分析。",
    "SearchQueries": ["查找该idea相关文献的Query"],
    "PapersToRead": ["https://doi.org/10.xxxx/xxxx"],
    "Decision": "Pending",
    "Score": null
}
```
若检索轮数超过8轮而且你在若干轮检索/阅读后有了明确结论，请将 "Decision" 设置为 "Finished"，并给出具体的 "Score" (1到10分)。
"""

NOVELTY_CHECK_EVAL_PROMPT = """你需要评估以下科研Idea：
标题: {title}
背景: {background}
假设: {hypothesis}
方法与实际场景考量: {methodology}

当前搜索结果反馈：
{search_results}

当前精读文献的全文笔记（知识库）：
{knowledge_base}

请继续你的审查。如果还需要搜索/阅读，请在Decision中填 "Pending"。如果审查完毕，请填 "Finished"。
"""

IDEA_REFINER_SYSTEM_PROMPT = """你是一个专业的通信领域科研助手。你的任务是根据用户的反馈和指令，修改并完善一个已有的科研Idea。
你的回复必须包含如下JSON格式（可以包含在 ```json 和 ``` 之间）：
```json
{
    "Thoughts": "你对用户反馈的理解，以及你修改Idea的思路。",
    "SearchQueries": ["query1", "query2"], 
    "PapersToRead": ["https://doi.org/10.xxxx/xxxx"],
    "Ideas": [
        {
            "Name": "Idea代号(保持不变)",
            "Title": "修改后的标题",
            "Background": "修改后的背景",
            "Hypothesis": "修改后的假设",
            "Methodology": "修改后的方法"
        }
    ]
}
```
如果不需要搜文献或用户禁止搜索，SearchQueries 应为空列表；如果不需要读全文，PapersToRead 应为空列表。
"""

IDEA_REFINER_START_PROMPT = """这是当前版本的Idea：
{current_idea}

用户对该Idea的修改反馈/指令如下：
【{user_feedback}】

请根据用户的指令，对Idea进行修改和完善。
"""

IDEA_REFINER_ITERATION_PROMPT = """这是你在上一轮中提交的文献搜索Query的结果：
{search_results}

这是你要求精读的论文的全文总结笔记（知识库）：
{knowledge_base}

这是上一轮修改后的Idea版本：
{previous_idea}

请根据最新的文献结果和之前的思路，继续完善Idea。确保回应了用户的初始反馈。
"""

PDFReader_PROMPT = """你是一个高级学术助理。你的任务是仔细阅读提供的PDF文献，并总结出其核心创新点(Key Takeaway)、使用的方法、以及它解决了什么具体问题。
你需要详细解释：
0) 论文标题（放在第一行）
1）这篇文章想解决什么问题
2）这篇文章采用了什么样的系统模型（给出具体文字描述和准确的公式描述）
3）详细解释这篇文章的所有创新点，对关键创新点给出具体公式
4）这篇文章的关键结论，取得的增益等
5）概括这篇文章中总结的本领域之前的研究进展
"""


##################################### Performing Experiments #####################################
with open (os.path.join(EXECUTE_PATH, 'PERFORM_EXPERIMENT.md'),'r',errors='ignore',encoding='utf-8') as f:
    EXPERIMENT_PROMPT_BASE = f.read()
EXPERIMENT_PROMPT = pb.build_prompt(
    EXPERIMENT_PROMPT_BASE,
    ["READ_FILE", "SPAWN_RUN", "KILL_TASK", "WAIT", "RECORD_DATA", "PASS_STEP", "SPAWN_CODER", "MODIFY_CODE","FINISH"]
)


with open (os.path.join(PLAN_PATH, 'PERFORM_EXPERIMENT_STUDENT.md'),'r',errors = 'ignore',encoding='utf-8') as f:
    EXPERIMENT_PLAN_PROMPT_BASE = f.read()
# EXPERIMENT_PLAN_PROMPT_BASE = "制定详细的实验计划产生足够多数据，让论文能发表在IEEE TCOM顶级期刊。"
EXPERIMENT_PLAN_PROMPT = pb.build_prompt(
    EXPERIMENT_PLAN_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "SUBMIT_PLAN"]
)


MONITOR_SYSTEM_PROMPT = """你是一个负责监控长期运行任务的 Orchestrator Agent。
你需要观察终端输出和硬件状态，判断程序是否陷入死循环、报错卡死或占用异常。
如果你认为程序正常运行，请返回：{"Action": "CONTINUE"}
如果你认为程序出现严重异常必须立刻杀死，或者当前程序无法在2个小时执行完，请返回：{"Action": "KILL", "Feedback": "你的理由"}
请严格以 JSON 格式返回。"""



##################################### Review #####################################
with open (os.path.join(EXECUTE_PATH, 'REVIEW_PDF.md'),'r', errors = 'ignore',encoding='utf-8') as f:
    PDF_COMMENTATOR_PROMPT = f.read()

with open (os.path.join(EXECUTE_PATH, 'REVIEW_PDF.md'),'r', errors = 'ignore',encoding='utf-8') as f:
    REVIEW_PROMPT_BASE = f.read()
REVIEW_PROMPT = pb.build_prompt(
    REVIEW_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "FINISH_REVIEW"]
)



##################################### Update from Review #####################################
with open (os.path.join(EXECUTE_PATH,'UPDATE_FROM_REVIEW.md'),'r',errors='ignore',encoding='utf-8') as f:
     UPDATE_PROMPT_BASE = f.read()
UPDATE_PROMPT = pb.build_prompt(
    UPDATE_PROMPT_BASE,
    ["SEARCH_LITERATURE", "READ_FILE", "SPAWN_CODER", "SPAWN_RUN", "WRITE_FILE", "RECORD_DATA", "KILL_TASK", "WAIT", "PASS_STEP", "FIND_TOOL", "MODIFY_CODE","FINISH"],
    append_text="当 Action 为 WRITE_FILE 时，在 JSON 外用 markdown 附带文件内容。严格遵循以下格式：\n### File: filename.txt\n```\n[内容]\n```"
)


with open (os.path.join(PLAN_PATH,'UPDATE_FROM_REVIEW_STUDENT.md'),'r',errors='ignore',encoding='utf-8') as f:
     UPADATE_PLAN_PROMPT_BASE = f.read()
UPDATE_FROM_REVIEW_STUDENT_PLAN_PROMPT = pb.build_prompt(
    UPADATE_PLAN_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "SUBMIT_PLAN"]
)


##################################### Code Generation #####################################
with open (os.path.join(EXECUTE_PATH, 'GENERATE_CODE.md'),'r',errors='ignore',encoding='utf-8') as f:
    GENERATE_CODE_PROMPT_WITH_PLAN_BASE = f.read()
# GENERATE_CODE_PROMPT_WITH_PLAN_BASE = """你是一个科研项目管家 (Orchestrator)。你需要充分读取当前workspace的内容，充分了解当前已有的代码、论文和运行数据，以及审稿人意见。
# 你需要根据审稿人的意见，在充分了解已有工作之后，按照已有的计划指导Coder修改代码，重新运行仿真，并完成回复撰写。"""
GENERATE_CODE_PROMPT_WITH_PLAN = pb.build_prompt(
    GENERATE_CODE_PROMPT_WITH_PLAN_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "SPAWN_CODER", "SPAWN_RUN", "KILL_TASK", "WAIT", "RECORD_DATA", "WRITE_FILE", "PASS_STEP", "FIND_TOOL", "MODIFY_CODE","FINISH"],
    append_text="当 Action 为 WRITE_FILE 时，可在外部附带代块，严格遵循以下格式：\n### File: filename.txt\n```\n[内容]\n```（注：尽量不要自己写代码，而是通过SPAWN_CODER来让Coder生成代码。你可以自己对代码做小的修改，或者让Coder对代码做大的修改）。"
)

with open (os.path.join(PLAN_PATH, 'CODER_STUDENT.md'),'r',errors = 'ignore',encoding='utf-8') as f:
    CODER_STUDENT_PLAN_PROMPT_BASE = f.read()
CODER_STUDENT_PLAN_PROMPT = pb.build_prompt(
    CODER_STUDENT_PLAN_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "SUBMIT_PLAN"]
)

CODER_TEACHER_PLAN_PROMPT_BASE = "你是一位苛刻的资深教授（Teacher Planner）。你的任务是审查学生提交的研究计划。"
CODER_TEACHER_PLAN_PROMPT = pb.build_prompt(
    CODER_TEACHER_PLAN_PROMPT_BASE,
    ["READ_FILE", "SEARCH_LITERATURE", "EVALUATE_PLAN"]
)




################################### Paper writeup #######################################
with open(os.path.join(PLAN_PATH, 'PERFORM_WRITEUP_STUDENT.md'),'r',errors='ignore',encoding='utf-8') as f:
    WRITER_PLAN_PROMPT = f.read()
WRITEUP_PLAN_PROMPT = pb.build_prompt(
    WRITER_PLAN_PROMPT,
    ["READ_FILE", "SEARCH_LITERATURE", "SUBMIT_PLAN"]
)

with open(os.path.join(EXECUTE_PATH, 'PERFORM_WRITEUP.md'),'r',errors='ignore',encoding='utf-8') as f:
    PAPER_WRITER_SYSTEM_PROMPT_BASE = f.read()

PAPER_WRITER_SYSTEM_PROMPT = pb.build_prompt(
    PAPER_WRITER_SYSTEM_PROMPT_BASE,
    ["READ_CODE", "WRITE_FILE", "SEARCH_LITERATURE"],
    append_text="当为 WRITE_FILE时, 必须在外部附带代码块：\n### File: papers\\abstract.tex\n```latex\n\\begin{abstract}\n...\n```"
)
