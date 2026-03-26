import os
import re
import time
import threading
import queue
import subprocess
import json
import asyncio
from collections import deque
import tkinter as tk
from tkinter import filedialog
import io

import chainlit as cl
from chainlit.input_widget import Select, TextInput, Slider

from llm import LLMAgent
from utils import setup_logger

# ==========================================
# 依赖存根 (原样保留)
# ==========================================
try:
    from utils import format_search_results_and_update_map, process_papers_to_read, read_knowledge_base
except ImportError:
    def format_search_results_and_update_map(queries, doi_map): return f"Searched for: {queries}"
    def process_papers_to_read(dois, doi_map, kb_path): pass
    def read_knowledge_base(kb_path): return "Knowledge Base Content"

from prompts import DEFAULT_ORCHESTRATOR_PROMPT, DEFAULT_CODER_PROMPT, PLANNER_PROMPT, STUDENT_PLANNER_PROMPT, TEACHER_CRITIC_PROMPT

logger = setup_logger("agent_workspace.log")

# ==========================================
# 模块1: 系统与环境监控 (System & Workspace)
# ==========================================
class SystemMonitor:
    @staticmethod
    def get_hardware_status():
        status_info = "【当前硬件资源状态】\n"
        try:
            smi_output = subprocess.check_output("nvidia-smi", shell=True, encoding="utf-8", errors="replace", timeout=5)
            status_info += f"--- nvidia-smi 专用显存与GPU利用率 ---\n{smi_output}\n"
        except Exception: pass
        try:
            mem_output = subprocess.check_output("wmic OS get FreePhysicalMemory,TotalVisibleMemorySize /Value", shell=True, encoding="utf-8", errors="ignore", timeout=5)
            status_info += f"--- 系统物理内存 ---\n{mem_output.strip()}\n"
        except Exception: pass
        return status_info

    @staticmethod
    def get_installed_packages(conda_env):
        cmd = f'conda run -n {conda_env} pip list'
        try:
            result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, text=True)
            return result.stdout if result.returncode == 0 else "Failed to get pip list."
        except: return "Failed to get pip list."

class WorkspaceManager:
    @staticmethod
    def git_init(workspace_dir, remote_repo=None):
        if not os.path.exists(os.path.join(workspace_dir, ".git")):
            subprocess.run(["git", "init"], cwd=workspace_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"[Git] 初始化 Git 仓库...")
        with open(os.path.join(workspace_dir, ".gitignore"), "w") as f:
            f.write("__pycache__/\n*.pyc\npdfs/\n*.log\n")
        subprocess.run(["git", "add", "."], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if remote_repo:
            try:
                subprocess.run(["git", "remote", "add", "origin", remote_repo], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "branch", "-M", "main"], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass

    @staticmethod
    def git_commit_and_push_with_msg(workspace_dir, commit_msg, remote_repo=None):
        subprocess.run(["git", "add", "."], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=workspace_dir, capture_output=True, text=True).stdout
        if status.strip():
            subprocess.run(["git", "commit", "-m", f"{commit_msg}"], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if remote_repo:
            try: subprocess.run(["git", "push", "origin", "main", "-f"], cwd=workspace_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass

    @staticmethod
    def save_state(workspace_dir, rounds, tool_calls_history, summaries, plan_mode, plan=None, plan_index=None):
        if not plan_mode:
            state = {"rounds": rounds, "tool_calls_history": tool_calls_history, "summaries": summaries}
        else:
            state = {"rounds": rounds, "plan": plan,"plan_index":plan_index,"tool_calls_history": tool_calls_history, "summaries": summaries}
        path = os.path.join(workspace_dir, "experiment_state.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=4)
        except Exception: pass

    @staticmethod
    def load_state(workspace_dir):
        path = os.path.join(workspace_dir, "experiment_state.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f: return json.load(f)
            except Exception: pass
        return None

    @staticmethod
    def get_workspace_state_recursive(dir_path, max_files_per_dir=10, prefix=""):
        if not os.path.exists(dir_path): return "Workspace empty."
        state = ""
        try: items = sorted(os.listdir(dir_path))
        except PermissionError: return prefix + "[Permission Denied]\n"

        items = [f for f in items if not f.startswith('.') and f not in ['__pycache__', 'pdfs']]
        files = [f for f in items if (os.path.isfile(os.path.join(dir_path, f)) and not f.endswith("experiment_state.json") and not f.endswith("log"))]
        dirs = [d for d in items if os.path.isdir(os.path.join(dir_path, d))]

        for i, f in enumerate(files):
            if i < max_files_per_dir: state += f"{prefix}- {f}\n"
            elif i == max_files_per_dir:
                state += f"{prefix}- ... and {len(files) - max_files_per_dir} more files.\n"
                break
                
        for d in dirs:
            state += f"{prefix}+ [DIR] {d}/\n"
            state += WorkspaceManager.get_workspace_state_recursive(os.path.join(dir_path, d), max_files_per_dir, prefix + "  ")
        return state if state else f"{prefix}(Empty Directory)\n"

    @staticmethod
    def extract_files_from_response(text):
        pattern = r"###\s*File:\s*([^\n]+)\s*```[^\n]*\n(.*?)```"
        matches = re.findall(pattern, text, re.DOTALL)
        return {filename.strip(): content.strip() for filename, content in matches}

    @staticmethod
    def save_files_to_workspace(files, cwd, base_readme=""):
        saved_list = []
        for filename, content in files.items():
            filepath = os.path.join(cwd, filename)
            final_content = f"{base_readme}\n\n## [Current Step]\n{content}".strip() if filename.lower() == "readme.md" else content
            try:
                with open(filepath, "w", encoding="utf-8") as f: f.write(final_content)
                saved_list.append(filename)
            except Exception: pass
        return saved_list


# ==========================================
# 模块2: 并发任务管理器 (Task Management)
# ==========================================
# class AsyncTask:
#     def __init__(self, task_id, task_type, args, workspace_dir):
#         self.task_id = task_id
#         self.task_type = task_type
#         self.args = args
#         self.workspace_dir = workspace_dir
#         self.status = "RUNNING"
#         self.output_queue = queue.Queue()
#         self.log_history = deque(maxlen=2000)
#         self.full_log = []
#         self.result_summary = ""
#         self.start_time = time.time()
#         self.process = None
#         self.thread = None
#         self._stop_event = threading.Event()

#     def log(self, msg):
#         line = msg.strip() + "\n"
#         self.output_queue.put(line)
#         self.log_history.append(line)
#         self.full_log.append(line)
#         try:
#             cl.run_sync(cl.Message(content=f"[{self.task_id}] {msg.strip()}").send())
#         except Exception as e:
#             # logger.error(f"[TaskManager] Failed to send message: {e}")
#             pass

#     def kill(self):
#         self._stop_event.set()
#         if self.process:
#             try: subprocess.run(f"taskkill /F /T /PID {self.process.pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
#             except: pass
#         self.status = "KILLED"
#         self.log("\n[System] Task was KILLED by Orchestrator.")

# class TaskManager:
#     def __init__(self, max_concurrent, workspace_dir, coder_model_name, env_type, env_name_or_path):
#         self.max_concurrent = max_concurrent
#         self.workspace_dir = workspace_dir
#         self.coder_model_name = coder_model_name
#         self.env_type = env_type
#         self.env_name_or_path = env_name_or_path
#         self.tasks = {}
#         self.task_counter = 0
#         self.system = None
#         self.finished_archive = {}

#     def get_active_tasks(self):
#         return {tid: t for tid, t in self.tasks.items() if t.status == "RUNNING"}

#     def get_finished_tasks_and_clear(self, action_history = None ):
#         for tid in list(self.tasks.keys()):
#             if self.tasks[tid].status in ["FINISHED", "KILLED", "ERROR"]:
#                 self.finished_archive[tid] = self.tasks.pop(tid)
#         return self.finished_archive

#     def _run_worker(self, task, run_script, tid):
#         import platform
#         is_windows = platform.system() == "Windows"
#         ext = "bat" if is_windows else "sh"
#         script_path = os.path.join(self.workspace_dir, f"run_{tid}.{ext}")
        
#         script_content = run_script
#         if self.env_type == "Conda" and self.env_name_or_path:
#             if is_windows:
#                 script_content = f"call conda activate {self.env_name_or_path}\n" + run_script
#             else:
#                 script_content = f"source activate {self.env_name_or_path}\n" + run_script
#         elif self.env_type == "Venv" and self.env_name_or_path:
#             if is_windows:
#                 script_content = f"call {self.env_name_or_path}\\Scripts\\activate.bat\n" + run_script
#             else:
#                 script_content = f"source {self.env_name_or_path}/bin/activate\n" + run_script

#         with open(script_path, "w", encoding="utf-8") as f: 
#             f.write(script_content)
        
#         if is_windows:
#             cmd = f'cmd.exe /c "{script_path} & exit"'
#         else:
#             cmd = f'bash "{script_path}"'
            
#         env = os.environ.copy()
#         env["PYTHONUNBUFFERED"] = "1"
        
#         task.process = subprocess.Popen(
#             cmd, shell=True, cwd=self.workspace_dir, stdout=subprocess.PIPE, 
#             stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', text=True, env=env
#         )
        
#         q = queue.Queue()
#         def reader_thread(proc, q_out):
#             for line in iter(proc.stdout.readline, ''): q_out.put(line)
#             proc.stdout.close()

#         rt = threading.Thread(target=reader_thread, args=(task.process, q), daemon=True)
#         rt.start()

#         while True:
#             if task._stop_event.is_set(): break
#             while not q.empty():
#                 try: task.log(q.get_nowait())
#                 except queue.Empty: break
#             if task.process.poll() is not None:
#                 while not q.empty():
#                     try: task.log(q.get_nowait())
#                     except queue.Empty: break
#                 break
#             if time.time() - task.start_time > 36000:
#                 task.log("进程运行超过硬性超时限制，被系统强制杀死。")
#                 task.kill()
#                 break
#             time.sleep(0.5)
        
#         if not task._stop_event.is_set():
#             task.status = "FINISHED" if task.process.returncode == 0 else "ERROR"
#             task.result_summary = f"Process exited with code {task.process.returncode}."

class AsyncTask:
    def __init__(self, task_id, task_type, args, workspace_dir):
        self.task_id = task_id
        self.task_type = task_type
        self.args = args
        self.workspace_dir = workspace_dir
        self.status = "RUNNING"
        self.output_queue = queue.Queue()
        self.log_history = deque(maxlen=2000)
        self.full_log = []
        self.result_summary = ""
        self.start_time = time.time()
        self.process = None
        self.thread = None
        self._stop_event = threading.Event()
        
        # 新增：用于跟踪和更新Chainlit界面的最后一条消息
        self.last_cl_msg = None
        self.is_last_update = False

    def log(self, msg, is_update=False):
        # 过滤掉 ANSI 转义字符 (终端颜色代码等)，保持UI整洁
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_msg = ansi_escape.sub('', msg).strip()
        
        if not clean_msg:
            return

        line = clean_msg + "\n"

        if is_update:
            # 如果当前是进度更新，且上一条也是更新，则覆盖上一条
            if self.is_last_update and self.last_cl_msg:
                if self.log_history: self.log_history.pop()
                if self.full_log: self.full_log.pop()
                
                self.log_history.append(line)
                self.full_log.append(line)
                
                try:
                    self.last_cl_msg.content = f"[{self.task_id}] {clean_msg}"
                    cl.run_sync(self.last_cl_msg.update()) # 核心：更新而不是发送
                except Exception:
                    pass
            else:
                # 第一次遇到进度更新行，新建一条消息
                self.log_history.append(line)
                self.full_log.append(line)
                try:
                    self.last_cl_msg = cl.Message(content=f"[{self.task_id}] {clean_msg}")
                    cl.run_sync(self.last_cl_msg.send())
                    self.is_last_update = True
                except Exception:
                    pass
        else:
            # 正常的新行输出
            self.output_queue.put(line)
            self.log_history.append(line)
            self.full_log.append(line)
            try:
                self.last_cl_msg = cl.Message(content=f"[{self.task_id}] {clean_msg}")
                cl.run_sync(self.last_cl_msg.send())
                self.is_last_update = False
            except Exception:
                pass

    def kill(self):
        self._stop_event.set()
        if self.process:
            try: subprocess.run(f"taskkill /F /T /PID {self.process.pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except: pass
        self.status = "KILLED"
        self.log("\n[System] Task was KILLED by Orchestrator.", is_update=False)
        
class TaskManager:
    def __init__(self, max_concurrent, workspace_dir, coder_model_name, env_type, env_name_or_path):
        self.max_concurrent = max_concurrent
        self.workspace_dir = workspace_dir
        self.coder_model_name = coder_model_name
        self.env_type = env_type
        self.env_name_or_path = env_name_or_path
        self.tasks = {}
        self.task_counter = 0
        self.system = None
        self.finished_archive = {}

    def get_active_tasks(self):
        return {tid: t for tid, t in self.tasks.items() if t.status == "RUNNING"}

    def get_finished_tasks_and_clear(self, action_history = None ):
        for tid in list(self.tasks.keys()):
            if self.tasks[tid].status in ["FINISHED", "KILLED", "ERROR"]:
                self.finished_archive[tid] = self.tasks.pop(tid)
        return self.finished_archive
        
        
    def _run_worker(self, task, run_script, tid):
        import platform
        is_windows = platform.system() == "Windows"
        ext = "bat" if is_windows else "sh"
        script_path = os.path.join(self.workspace_dir, f"run_{tid}.{ext}")
        
        script_content = run_script
        if self.env_type == "Conda" and self.env_name_or_path:
            if is_windows:
                script_content = f"call conda activate {self.env_name_or_path}\n" + run_script
            else:
                script_content = f"source activate {self.env_name_or_path}\n" + run_script
        elif self.env_type == "Venv" and self.env_name_or_path:
            if is_windows:
                script_content = f"call {self.env_name_or_path}\\Scripts\\activate.bat\n" + run_script
            else:
                script_content = f"source {self.env_name_or_path}/bin/activate\n" + run_script

        with open(script_path, "w", encoding="utf-8") as f: 
            f.write(script_content)
        
        if is_windows:
            # 加入 chcp 65001 强制Windows cmd以UTF-8输出，解决  乱码问题
            cmd = f'cmd.exe /c "chcp 65001 > nul & {script_path} & exit"'
        else:
            cmd = f'bash "{script_path}"'
            
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8" # 进一步确保Python子进程输出UTF-8
        
        # task.process = subprocess.Popen(
        #     cmd, shell=True, cwd=self.workspace_dir, stdout=subprocess.PIPE, 
        #     stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', 
        #     text=True, newline='', env=env # 【关键】添加 newline='' 防止 \r 被自动翻译为 \n
        # )
        
        task.process = subprocess.Popen(
            cmd, shell=True, cwd=self.workspace_dir, stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, env=env 
        )
        
        # 【修改点 2】：手动包装 stdout 数据流，在这里设置 newline='' 拦截换行符转换
        stream = io.TextIOWrapper(
            task.process.stdout,
            encoding='utf-8',
            errors='replace',
            newline=''  # 核心：保留 \r 不被转换为 \n
        )
        
        q = queue.Queue()
        
        # 【关键】修改读取线程：按字符读取，手动识别 \r 和 \n
        # def reader_thread(proc, q_out):
        #     buffer = []
        #     while True:
        #         char = proc.stdout.read(1)
        #         if not char:
        #             if buffer:
        #                 q_out.put(("".join(buffer), False))
        #             break
        #         if char == '\n':
        #             q_out.put(("".join(buffer), False)) # 遇到换行，普通日志
        #             buffer = []
        #         elif char == '\r':
        #             q_out.put(("".join(buffer), True))  # 遇到回车，进度条更新
        #             buffer = []
        #         else:
        #             buffer.append(char)
        #     proc.stdout.close()

        # rt = threading.Thread(target=reader_thread, args=(task.process, q), daemon=True)
        def reader_thread(proc, q_out, text_stream):
            buffer = []
            while True:
                try:
                    char = text_stream.read(1)
                except ValueError: # 防止 stream 在关闭时读取报错
                    break
                    
                if not char:
                    if buffer:
                        q_out.put(("".join(buffer), False))
                    break
                    
                if char == '\n':
                    q_out.put(("".join(buffer), False))
                    buffer = []
                elif char == '\r':
                    q_out.put(("".join(buffer), True))
                    buffer = []
                else:
                    buffer.append(char)
            text_stream.close()

        # 启动线程，传入手动包装的 stream
        rt = threading.Thread(target=reader_thread, args=(task.process, q, stream), daemon=True)
        rt.start()

        while True:
            if task._stop_event.is_set(): break
            while not q.empty():
                try: 
                    item = q.get_nowait()
                    if isinstance(item, tuple):
                        task.log(item[0], is_update=item[1])
                    else:
                        task.log(item)
                except queue.Empty: break
            
            if task.process.poll() is not None:
                while not q.empty():
                    try: 
                        item = q.get_nowait()
                        if isinstance(item, tuple):
                            task.log(item[0], is_update=item[1])
                        else:
                            task.log(item)
                    except queue.Empty: break
                break
            
            if time.time() - task.start_time > 36000:
                task.log("进程运行超过硬性超时限制，被系统强制杀死。")
                task.kill()
                break
            time.sleep(0.5)
        
        if not task._stop_event.is_set():
            task.status = "FINISHED" if task.process.returncode == 0 else "ERROR"
            task.result_summary = f"Process exited with code {task.process.returncode}."

    def spawn_run(self, run_script):
        if len(self.get_active_tasks()) >= self.max_concurrent:
            return None, "Max concurrency reached. Please WAIT or KILL_TASK."
        self.task_counter += 1
        tid = f"Task-Run-{self.task_counter}"
        task = AsyncTask(tid, "RUN", {"script": run_script}, self.workspace_dir)
        self.tasks[tid] = task
        task.thread = threading.Thread(target=self._run_worker, args=(task, run_script, tid), daemon=True)
        task.thread.start()
        return tid, "Spawned successfully."
    
    def _coder_worker(self, task, instruction, tid):
        import platform
        log_dir = os.path.join(self.workspace_dir, "log")
        os.makedirs(log_dir, exist_ok=True)
        coder_agent = LLMAgent(model=self.coder_model_name, log_file=os.path.join(log_dir, f"coder_{tid}.log"))
        coder_history = []
        task.log(f"[Coder] Started task: {instruction[:50]}...")
        pip_list = SystemMonitor.get_installed_packages(self.env_name_or_path) if self.env_type == "Conda" else "Package list unavailable"
        
        for i in range(10):
            if task._stop_event.is_set(): break
            coder_agent.clear_history()
            ws_state = WorkspaceManager.get_workspace_state_recursive(self.workspace_dir, 20)
            
            prompt = f"【Orchestrator 指令】\n{instruction}\n\n【当前工作空间文件结构】\n{ws_state}\n\n【Pip 依赖包】\n{pip_list[:1000]}\n\n"
            if coder_history:
                prompt += "【已执行的 Tool 历史】\n"
                for h in coder_history:
                    prompt += f"Action: {h['action']}, Params: {json.dumps(h['params'], ensure_ascii=False)}\nResult:\n{h['result']}\n\n"
            prompt += "请决定下一步 Action (READ_CODE, RUN_CODE, SUBMIT_CODE, FINISH, MODIFY_CODE)。"

            try:
                formatted_prompt = DEFAULT_CODER_PROMPT
                resp, _ = coder_agent.get_response_stream(prompt, formatted_prompt)
            except Exception as e:
                task.log(f"[Coder Error] API fail: {e}")
                time.sleep(5)
                continue
            
            action_json = LLMAgent.robust_extract_json(resp)
            if not action_json:
                files = WorkspaceManager.extract_files_from_response(resp)
                if files:
                    saved_files = WorkspaceManager.save_files_to_workspace(files, self.workspace_dir)
                    task.log(f"[Coder] 隐式提交文件: {', '.join(saved_files)}")
                    task.status = "FINISHED"
                    task.result_summary = f"Files written: {', '.join(saved_files)}"
                    return
                coder_history.append({"action": "PARSE_ERROR", "params": "", "result": "JSON解析失败。"})
                continue
            
            action = action_json.get("Action")
            params = action_json.get("Action_Params", {})
            
            system = getattr(self, "system", None)
            
            if action == "SUBMIT_CODE":
                files = WorkspaceManager.extract_files_from_response(resp)
                if files:
                    saved_files = WorkspaceManager.save_files_to_workspace(files, self.workspace_dir)
                    task.log(f"[Coder] 最终提交代码且保存文件: {', '.join(saved_files)}")
                else:
                    task.log(f"[Coder] 最终提交代码。")
                task.status = "FINISHED"
                task.result_summary = f"Coder Successfully Finished Task."
                WorkspaceManager.git_commit_and_push_with_msg(self.workspace_dir, f"Coder finished task.")
                return
                
            if system and action in ["READ_FILE", "WRITE_FILE", "RUN_CODE", "MODIFY_CODE","FINISH"]:
                if action in ["WRITE_FILE", "RUN_CODE"]:
                    files = WorkspaceManager.extract_files_from_response(resp)
                    if files: 
                        saved = WorkspaceManager.save_files_to_workspace(files, self.workspace_dir)
                        if action != "WRITE_FILE":
                            task.log(f"[Coder Action] 自动保存附带代码块文件: {', '.join(saved)}")
                        
                # Use StandardTools asynchronously via asyncio.run
                try:
                    res = asyncio.run(system.tool_registry.execute(action, system, params, resp))
                except Exception as e:
                    res = f"Tool Execution Error: {e}"
                    
                coder_history.append({"action": action, "params": params, "result": res})
                
                # Feedback to Orchestrator via task.log
                if action == "MODIFY_CODE":
                    task.log(f"[Coder Action] 执行 MODIFY_CODE -> 文件 {params.get('filename')}。结果: {res[:500]}...")
                elif action == "RUN_CODE":
                    summary_out = res.replace('\n', ' ')
                    task.log(f"[Coder Action] 执行 RUN_CODE。结果: {summary_out[:500]}...")
                elif action == "READ_FILE":
                    task.log(f"[Coder Action] 读取文件 {params.get('filename')}")
                elif action == "WRITE_FILE":
                    task.log(f"[Coder Action] 写文件 {params.get('filename')}")
                elif action == "FINISH":
                    task.log(f"[Coder Action] Coder认为自己完成了工作")
                    task.status = "FINISHED"
                    task.result_summary = f"Coder Successfully Finished Task."
                    return
            else:
                coder_history.append({"action": action, "params": params, "result": f"Unsupported or Unknown Action '{action}' for Coder."})
        
        if not task._stop_event.is_set():
            task.status = "ERROR"
            task.result_summary = "Coder 达到调试上限，未能提交代码。"

    def spawn_coder(self, instruction):
        if len(self.get_active_tasks()) >= self.max_concurrent:
            return None, "Max concurrency reached. Please WAIT or KILL_TASK."
        self.task_counter += 1
        tid = f"Task-Coder-{self.task_counter}"
        task = AsyncTask(tid, "CODER", {"instruction": instruction}, self.workspace_dir)
        self.tasks[tid] = task
        task.thread = threading.Thread(target=self._coder_worker, args=(task, instruction, tid), daemon=True)
        task.thread.start()
        return tid, "Coder spawned successfully."

    def kill_task(self, task_id):
        if task_id in self.tasks:
            self.tasks[task_id].kill()
            return f"Task {task_id} kill signal sent."
        return f"Task {task_id} not found."


# ==========================================
# 模块3: 动态工具注册与系统级上下文构建 (Extensibility)
# ==========================================
class Tool:
    def __init__(self, name, description, handler_coroutine):
        self.name = name
        self.description = description
        self.handler_coroutine = handler_coroutine

class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    async def execute(self, action_name, agent_system, params, full_resp):
        if action_name in self.tools:
            return await self.tools[action_name].handler_coroutine(agent_system, params, full_resp)
        return f"Unknown Action: {action_name}"

class BaseContextBuilder:
    def build_context(self, system: "AgentSystem", request_text: str, active_tasks_info: str, finished_tasks_info: str, workspace_tree: str, hardware_status: str) -> str:
        """可被用户覆写，实现即插即用的 Context Prompt"""
        context = f"【用户在你的第{self.rounds}轮行动前提出的请求/意见】\n{request_text}\n\n"
        context += f"【工作目录结构】\n{workspace_tree}\n\n{hardware_status}\n"
        if system.plan_mode:
            if system.plan_index < len(system.plan):
                active_steps = system.plan[system.plan_index : system.plan_index + system.concurrent_plan_steps]
                context += f"完整的科研计划是：\n{system.plan}\n"
                context += f"【当前执行计划 (Plan Mode)】\n整体进度: {system.plan_index}/{len(system.plan)}\n当前你必须聚焦完成的步骤:\n"
                for i, step in enumerate(active_steps):
                    context += f"{i+1}. {step}\n"
                context += "完成上述所有当前步骤后，必须调用 FINISH_STEP 工具推进计划。\n\n"
            else:
                context += "【当前执行计划 (Plan Mode)】\n所有计划步骤均已完成，请检查并调用 FINISH 工具结束任务。\n\n"
        context += f"【当前运行中的任务监控 (最大并发:{system.task_manager.max_concurrent})】\n{active_tasks_info}\n\n"
        if finished_tasks_info: context += f"【刚刚结束的任务】\n{finished_tasks_info}\n\n"
            
        context += "【近期执行过的历史动作】\n"
        for h in system.action_history[-15:]:
            context += f"Action: {h.get('action')}, Params: {json.dumps(h.get('params',{}), ensure_ascii=False)}\nResult: {str(h.get('result', ''))[-10000:]}\n\n"
        context += f"【最近执行历史的概述】\n{system.summaries}\n\n请根据上述监控状态和请求，返回你的 JSON 决策。如果你需要等待时间收集日志输出，请选择 WAIT。"
        return context
        
        
class PlannerContextBuilder:
    @staticmethod
    def build_student_context(request_text: str, user_feedback: str, teacher_feedback: str, workspace_tree: str, hardware_status: str, action_history: list, student_plan_prev: list, is_last_step = False) -> str:
        context = f"【用户原始需求】\n{request_text}\n\n"
        context += f"【当前工作目录结构】\n{workspace_tree}\n\n{hardware_status}\n\n"
        
        if user_feedback:
            context += f"【⚠️ 用户的退回修改意见】\n{user_feedback}\n\n"
        if teacher_feedback:
            context += f"【⚠️ Teacher Agent 的审核反馈意见】\n{teacher_feedback}\n请根据上述意见重新调查并修改你的计划！\n\n"
            
        context += "【你近期的探索历史】\n"
        for h in action_history[-10:]:
            context += f"Action: {h.get('action')}, Params: {json.dumps(h.get('params',{}), ensure_ascii=False)}\nResult: {str(h.get('result', ''))}\n\n"
        context += "【你给出的最新计划（如果为空，则说明你尚未给出任何计划）】\n"
        plan_str = "\n".join([f"{i+1}. {step}" for i, step in enumerate(student_plan_prev)])
        context += plan_str
        if is_last_step:
            context += f"这是最后一轮评估，你必须调用SUBMIT_PLAN给出最终的计划。"
        context += "请基于以上信息，返回你的 JSON 决策。如果调查完毕，请调用 SUBMIT_PLAN 提交计划。"
        return context

    @staticmethod
    def build_teacher_context(request_text: str, student_plan: list, workspace_tree: str, action_history: list, is_last_step = False) -> str:
        context = f"【用户原始需求】\n{request_text}\n\n"
        context += f"【当前工作目录结构】\n{workspace_tree}\n\n"
        
        plan_str = "\n".join([f"{i+1}. {step}" for i, step in enumerate(student_plan)])
        context += f"【Student 提交的草案计划】\n{plan_str}\n\n"
        
        context += "【你(Teacher)近期的验证历史】\n"
        for h in action_history[-10:]:
            context += f"Action: {h.get('action')}, Params: {json.dumps(h.get('params',{}), ensure_ascii=False)}\nResult: {str(h.get('result', ''))}\n\n"
            
        if is_last_step:
            context += f"这是最后一轮评估，你必须调用EVALUATE_PLAN给出最终审核结果。"
            
        context += "请使用工具验证计划可行性，或直接调用 EVALUATE_PLAN 给出审核结果。"
        return context

# ==========================================
# 模块4: 核心智能体系统编排器 (Orchestrator System)
# ==========================================
class StandardTools:
    """内部封装所有的核心标准工具"""
    @staticmethod
    async def finish(system, params, resp):
        await cl.Message(content=f"🎉 **管家确认任务完成**\n总结: {params.get('summary', '')}").send()
        WorkspaceManager.git_commit_and_push_with_msg(system.workspace_dir, "Finished User Request Workflow.")
        system.stop_workflow = True
        return "Workflow finished."

    @staticmethod
    async def wait(system, params, resp):
        wait_time = int(params.get("wait_seconds", 10))
        if wait_time < 20: wait_time = 20
        await cl.Message(content=f"⏳ 等待 `{wait_time}` 秒收集日志信息... (发送新指令可立即打断)").send()
        try:
            await asyncio.wait_for(system.interrupt_event.wait(), timeout=min(wait_time, 600))
            system.interrupt_event.clear()
            await cl.Message(content="⚡ 等待已被打断，立即响应最新指令！").send()
            return "Wait interrupted by user new requests."
        except asyncio.TimeoutError:
            return f"Waited {wait_time}s."

    @staticmethod
    async def kill_task(system, params, resp):
        tid = params.get("task_id", "")
        res = system.task_manager.kill_task(tid)
        await cl.Message(content=f"🔪 杀死任务: `{tid}`").send()
        return res

    @staticmethod
    async def spawn_coder(system, params, resp):
        instruction = params.get("instruction", "")
        tid, msg = system.task_manager.spawn_coder(instruction)
        await cl.Message(content=f"🧑‍💻 下发编程任务: `{tid}`\n指令: {instruction[:100]}...").send()
        return f"{msg} (Task ID: {tid})"

    @staticmethod
    async def spawn_run(system, params, resp):
        script = params.get("run_script", "")
        tid, msg = system.task_manager.spawn_run(script)
        await cl.Message(content=f"⚙️ 下发运行任务: `{tid}`\n命令: {script}").send()
        return f"{msg} (Task ID: {tid})"

    @staticmethod
    async def read_file(system, params, resp):
        fn = params.get("filename", "")
        instruction = params.get("instruction", "")
        path = os.path.join(system.workspace_dir, fn)
        if os.path.exists(path):
            if fn.lower().endswith(".pdf"):
                try:
                    from utils import PDFReader
                    gemini_api_key = os.environ.get("JIANYI_API_KEY", "")
                    if not gemini_api_key:
                        return "Error: JIANYI_API_KEY not set for reading PDFs."
                    temp_out = os.path.join(system.workspace_dir, "temp_pdf_read_result.txt")
                    prompt = instruction + "\n" + (system.prompt_file if system.prompt_file else "You are an AI research assistant.")
                    reader = PDFReader(api_key=gemini_api_key, system_prompt=prompt, context_window_size=1)
                    reader.read_pdf(path, temp_out, user_prompt=instruction if instruction else "Summarize the paper's main idea.")
                    if os.path.exists(temp_out):
                        with open(temp_out, "r", encoding="utf-8") as f:
                            res = f.read()
                        os.remove(temp_out)
                        return res
                    return "PDF Read failed to produce output."
                except Exception as e:
                    return f"PDF Read Error: {e}"
            else:
                try:
                    with open(path, "r", encoding="utf-8") as f: 
                        lines = f.readlines()
                    res = "".join([f"{i+1}: {line}" for i, line in enumerate(lines)])
                    return res
                except Exception as e: return f"Read Error: {e}"
        return "File Not Found."

    @staticmethod
    async def find_tool(system, params, resp):
        keyword = params.get("keyword", "")
        if not keyword: return "Error: keyword is empty."
        results = []
        for root, dirs, files in os.walk(system.workspace_dir):
            if '.git' in root or '__pycache__' in root:
                continue
            for fn in files:
                if fn.endswith(('.py', '.txt', '.md', '.bat', '.sh', '.tex')):
                    fpath = os.path.join(root, fn)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        for i, line in enumerate(lines):
                            if keyword in line:
                                start = max(0, i - 2)
                                end = min(len(lines), i + 3)
                                snippet = "".join(lines[start:end])
                                results.append(f"File: {os.path.relpath(fpath, system.workspace_dir)} Line: {i+1}\n{snippet}")
                    except Exception:
                        pass
        if not results: return "No matches found."
        return f"Found {len(results)} matches.\n\n" + "\n---\n".join(results[:20])

    @staticmethod
    # import os

    async def modify_code(system, params, resp):
        fn = params.get("filename", "")
        start_line = params.get("start_line")
        end_line = params.get("end_line")
        old_code = params.get("old_code", "") # 仅用作安全校验
        new_code = params.get("new_code", "") # 替换后的新代码
        
        path = os.path.join(system.workspace_dir, fn)
        if not os.path.exists(path):
            return f"Error: File {fn} not found."
            
        try:
            # 1. 基础行号校验与转换 (1-indexed to 0-indexed)
            start_idx = int(start_line) - 1
            end_idx = int(end_line) - 1
            
            if start_idx < 0 or end_idx < start_idx:
                return f"Error: Invalid line range {start_line} to {end_line}."

            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
            if end_idx >= len(lines):
                return f"Error: end_line {end_line} exceeds file length ({len(lines)} lines)."

            # 2. 提取目标区域的真实代码
            # Python 切片是左闭右开，所以 end_idx 需要 +1
            target_lines = lines[start_idx : end_idx + 1]
            
            # 3. 鲁棒的安全校验（防呆机制）：忽略空行和前后空格
            def normalize_code(text_or_lines):
                """将代码标准化：按行分割，去掉每行首尾空白，并剔除纯空行"""
                if isinstance(text_or_lines, str):
                    lines_list = text_or_lines.splitlines()
                else:
                    lines_list = text_or_lines
                return [line.strip() for line in lines_list if line.strip()]

            # 如果 LLM 提供了 old_code，我们进行“模糊比对”
            if old_code:
                norm_target = normalize_code(target_lines)
                norm_old = normalize_code(old_code)
                
                if norm_target != norm_old:
                    # 校验失败时，把真实的行代码返回给 LLM，帮助它纠正幻觉
                    actual_code_str = "".join(target_lines)
                    # return (
                    #     f"Error: Code mismatch at lines {start_line}-{end_line}.\n"
                    #     f"You expected to replace:\n{old_code}\n\n"
                    #     f"But the actual code at lines {start_line}-{end_line} is:\n{actual_code_str}\n"
                    #     f"Please check the line numbers and try again."
                    # )

            # 4. 处理新代码并确保换行符正确
            # 使用 splitlines() 去除 LLM 可能乱加的 \n，然后统一规范添加 \n
            if new_code.strip() == "":
                new_lines = []  # 支持纯删除操作
            else:
                new_lines = [line + "\n" for line in new_code.splitlines()]

            # 5. 执行替换：[起始行前] + [新代码] + [结束行后]
            final_lines = lines[:start_idx] + new_lines + lines[end_idx + 1:]

            # 6. 写入文件并提交
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(final_lines)
                
            # 假设 WorkspaceManager 是你系统里的 Git 管理类
            WorkspaceManager.git_commit_and_push_with_msg(system.workspace_dir, f"MODIFY_CODE updated {fn}")
            
            # 计算行数变化给 LLM 一个反馈
            lines_diff = len(new_lines) - len(target_lines)
            diff_msg = f" (File length changed by {lines_diff} lines)" if lines_diff != 0 else ""
            
            return f"Successfully replaced lines {start_line} to {end_line} in {fn}.{diff_msg}"
            
        except ValueError:
            return "Error: start_line and end_line must be integers."
        except Exception as e:
            return f"MODIFY_CODE Error: {e}"
    # async def modify_code(system, params, resp):
    #     fn = params.get("filename", "")
    #     start_line = params.get("start_line", 1)
    #     old_code = params.get("old_code", "")
    #     new_code = params.get("new_code", "")
    #     path = os.path.join(system.workspace_dir, fn)
    #     if not os.path.exists(path):
    #         return f"Error: File {fn} not found."
    #     try:
    #         with open(path, "r", encoding="utf-8") as f:
    #             lines = f.readlines()
    #         old_lines = old_code.splitlines(keepends=True)
    #         if not old_lines: return "Error: old_code is empty."
    #         start_idx = int(start_line) - 1
    #         if start_idx < 0 or start_idx >= len(lines):
    #             return f"Error: start_line {start_line} out of bounds."
    #         actual_old = "".join(lines[start_idx:start_idx + len(old_lines)])
    #         if actual_old.strip() != old_code.strip():
    #             return f"Error: Code mismatch at line {start_line}.\nExpected:\n{old_code}\nActually found:\n{actual_old}"
    #         new_lines = new_code.splitlines(keepends=True)
    #         lines = lines[:start_idx] + new_lines + lines[start_idx + len(old_lines):]
    #         with open(path, "w", encoding="utf-8") as f:
    #             f.writelines(lines)
    #         WorkspaceManager.git_commit_and_push_with_msg(system.workspace_dir, f"MODIFY_CODE updated {fn}")
    #         return f"Successfully modified {fn} at line {start_line}."
    #     except Exception as e:
    #         return f"MODIFY_CODE Error: {e}"

    @staticmethod
    async def write_file(system, params, resp):
        files = WorkspaceManager.extract_files_from_response(resp)
        if files:
            saved_files = WorkspaceManager.save_files_to_workspace(files, system.workspace_dir)
            WorkspaceManager.git_commit_and_push_with_msg(system.workspace_dir, f"Orchestrator wrote files: {','.join(saved_files)}")
            return f"Wrote files: {', '.join(saved_files)}"
        return "Error: Markdown code blocks missing in response."

    @staticmethod
    async def search_literature(system, params, resp):
        queries = params.get("queries", [])
        try: return format_search_results_and_update_map(queries, system.doi_url_map)
        except Exception as e: return f"Literature Search Error: {e}"

    @staticmethod
    async def read_paper(system, params, resp):
        dois = params.get("dois", [])
        try:
            process_papers_to_read(dois, system.doi_url_map, system.kb_txt_path)
            return read_knowledge_base(system.kb_txt_path)
        except Exception as e: return f"Read Paper Error: {e}"

    @staticmethod
    async def record_data(system, params, resp):
        data_record = params.get("data", "")
        with open(system.data_record_txt, "a", encoding="utf-8") as f:
            f.write(f"--- Round {system.rounds} Data Record ---\n{data_record}\n\n")
        WorkspaceManager.git_commit_and_push_with_msg(system.workspace_dir, "Recorded crucial experiment data.")
        return "Data successfully recorded."

    @staticmethod
    async def run_code(system, params, resp):
        import platform # if not already imported
        run_script = params.get("run_script", "")
        script_path = os.path.join(system.workspace_dir, f"sync_run_tool.{'bat' if platform.system() == 'Windows' else 'sh'}")
        
        env_type = system.task_manager.env_type
        env_name_or_path = system.task_manager.env_name_or_path
        
        script_content = run_script
        if env_type == "Conda" and env_name_or_path:
            script_content = (f"call conda activate {env_name_or_path}\n{run_script}" if platform.system() == "Windows" else f"source activate {env_name_or_path}\n{run_script}")
        elif env_type == "Venv" and env_name_or_path:
            script_content = (f"call {env_name_or_path}\\Scripts\\activate.bat\n{run_script}" if platform.system() == "Windows" else f"source {env_name_or_path}/bin/activate\n{run_script}")

        with open(script_path, "w", encoding="utf-8") as f: f.write(script_content)
        cmd = f'cmd.exe /c "{script_path} & exit"' if platform.system() == "Windows" else f'bash "{script_path}"'
        
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        def run_proc():
            return subprocess.run(cmd, shell=True, cwd=system.workspace_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', text=True, env=env)
            
        proc = await asyncio.to_thread(run_proc)
        return f"Execute Success: {proc.returncode == 0}\nConsole Output:\n{proc.stdout}\n"

    @staticmethod
    async def finish_step(system, params, resp):
        if not system.plan_mode: return "Error: Not in Plan Mode."
        system.plan_index += system.concurrent_plan_steps
        if system.plan_index >= len(system.plan):
            return "All plan steps have been finished. You must call the FINISH tool in JSON format to exit the cycle."
        return "Step marked as done. Next steps will be provided."

class AgentSystem:
    def __init__(self, workspace_dir, settings, interrupt_event, user_interrupt_requests, student_planner_prompt = None, teacher_critic_prompt = None):
        self.workspace_dir = workspace_dir
        self.settings = settings
        self.interrupt_event = interrupt_event
        self.user_interrupt_requests = user_interrupt_requests
        
        self.orchestrator_model = settings.get("orchestrator_model", "gemini-3-pro-preview")
        self.max_rounds = settings.get("max_rounds", 500)
        self.max_files = settings.get("max_files_per_dir", 50)
        self.prompt_file = settings.get("prompt_file", "")
        self.student_planner_prompt = STUDENT_PLANNER_PROMPT
        self.teacher_critic_prompt = TEACHER_CRITIC_PROMPT
        if student_planner_prompt is not None:
            self.student_planner_prompt = student_planner_prompt
        if teacher_critic_prompt is not None:
            self.teacher_critic_prompt = teacher_critic_prompt
        self.adversarial_mode = settings.get("adversarial_mode",True)
        
        # Plan Mode Settings
        self.plan_mode = settings.get("plan_mode", False)
        self.concurrent_plan_steps = settings.get("concurrent_plan_steps", 1)
        self.plan = []
        self.plan_index = 0
        self.max_plan_iterations = 5
        self.max_adversarial_plan_iterations = 3

        # LLM Instances
        log_dir = os.path.join(workspace_dir, "log")
        os.makedirs(log_dir, exist_ok=True)
        self.orchestrator = LLMAgent(model=self.orchestrator_model, log_file=os.path.join(log_dir, 'orchestrator.log'))
        self.task_manager = TaskManager(
            max_concurrent=settings.get("max_concurrent_tasks", 3),
            workspace_dir=workspace_dir,
            coder_model_name=settings.get("coder_model", "gemini-3.1-pro-preview"),
            env_type=settings.get("env_type", "None"),
            env_name_or_path=settings.get("env_name_or_path", "")
        )
        self.task_manager.system = self
        
        # Tools & Extensibility
        self.tool_registry = ToolRegistry()
        self.context_builder = BaseContextBuilder()
        self.setup_default_tools()

        # State vars
        self.rounds = 0
        self.action_history = []
        self.summaries = ""
        self.stop_workflow = False
        
        # Files
        self.summary_txt = os.path.join(workspace_dir, "experiment_summary.txt")
        self.data_record_txt = os.path.join(workspace_dir, "recorded_data.txt")
        self.kb_txt_path = os.path.join(workspace_dir, "knowledge_base.txt")
        self.doi_url_map = {}

    def setup_default_tools(self):
        self.tool_registry.register(Tool("FINISH", "结束任务", StandardTools.finish))
        self.tool_registry.register(Tool("WAIT", "等待任务", StandardTools.wait))
        self.tool_registry.register(Tool("KILL_TASK", "杀任务", StandardTools.kill_task))
        self.tool_registry.register(Tool("SPAWN_CODER", "分发编程", StandardTools.spawn_coder))
        self.tool_registry.register(Tool("SPAWN_RUN", "分发运行", StandardTools.spawn_run))
        self.tool_registry.register(Tool("READ_FILE", "读文件", StandardTools.read_file))
        self.tool_registry.register(Tool("WRITE_FILE", "写文件", StandardTools.write_file))
        self.tool_registry.register(Tool("SEARCH_LITERATURE", "搜文献", StandardTools.search_literature))
        self.tool_registry.register(Tool("READ_PAPER", "读文献", StandardTools.read_paper))
        self.tool_registry.register(Tool("RECORD_DATA", "记录数据", StandardTools.record_data))
        self.tool_registry.register(Tool("FINISH_STEP", "完成计划步", StandardTools.finish_step))
        self.tool_registry.register(Tool("FIND_TOOL", "查找内容", StandardTools.find_tool))
        self.tool_registry.register(Tool("MODIFY_CODE", "修改代码", StandardTools.modify_code))
        self.tool_registry.register(Tool("RUN_CODE", "执行代码", StandardTools.run_code))

    async def _generate_plan(self, request_text):
        await cl.Message(content="🧠 **开启高级规划模式 (Multi-Step & Adversarial Planner)**\n正在进行深度环境探索与任务拆解...").send()
        
        # 从 settings 中读取配置（如果没有配置，使用默认值）
        # adversarial_mode = self.settings.get("adversarial_mode", True)
        # max_plan_iterations = 5 # 限制Agent在规划阶段单次尝试的最大Tool调用次数
        
        # 初始化 Teacher Agent (如果开启了对抗模式)
        teacher_agent = LLMAgent(model=self.orchestrator_model, log_file=os.path.join(self.workspace_dir, 'log', 'teacher_planner.log')) if self.adversarial_mode else None

        plan_approved = False
        user_feedback = ""
        teacher_feedback = ""
        student_plan_prev = []

        # 第一层循环：Human-in-the-Loop (用户审核)
        adversarial_iter = 0
        while not plan_approved:
            student_plan = []
            student_history = []
            self.orchestrator.clear_history()
            await cl.Message(content="🕵️ **[Student Planner]** 开始探索工作空间并构建计划...").send()

            # 第二层循环：Student 工具调用与计划生成
            for step_idx in range(self.max_plan_iterations):
                is_final_step = ((step_idx+1) == self.max_plan_iterations)
                try:
                    workspace_tree = WorkspaceManager.get_workspace_state_recursive(self.workspace_dir, self.max_files)
                except:
                    logger.error(f"Failed to get workspace state for {self.workspace_dir}")
                hardware_status = SystemMonitor.get_hardware_status()
                try:
                # 构建 Student 的 Context
                    context = PlannerContextBuilder.build_student_context(
                        request_text, user_feedback, teacher_feedback, workspace_tree, hardware_status, student_history, student_plan_prev, is_final_step
                    )
                except Exception as e:
                    logger.error(f"Failed to build student context: {e}")
                
                async with cl.Step(name=f"Student Plan Step {step_idx+1}") as step:
                    try:
                        resp, _ = await asyncio.to_thread(self.orchestrator.get_response_stream, context, self.student_planner_prompt)
                    except Exception as e:
                        step.output = f"API 异常: {e}"
                        await asyncio.sleep(2)
                        continue

                    action_json = LLMAgent.robust_extract_json(resp)
                    if not action_json:
                        student_history.append({"action": "ERROR", "params": {}, "result": "JSON解析失败"})
                        step.output = "解析 JSON 失败"
                        continue

                    action = action_json.get("Action")
                    params = action_json.get("Action_Params", {})
                    step.output = f"**Thoughts:** {action_json.get('Thoughts', '')}\n**Action:** `{action}`"

                    # 拦截特殊的提交流程工具
                    student_plan = []
                    if action == "SUBMIT_PLAN":
                        student_plan = params.get("Plan", [])
                        if len(student_plan)>0:
                            student_plan_prev = student_plan
                        break
                    
                    # 调用普通系统工具
                    res = await self.tool_registry.execute(action, self, params, resp)
                    student_history.append({"action": action, "params": params, "result": res})

            if not student_plan:
                student_plan = ["(系统降级) 基于常识完成用户请求", "检查结果并结束"]
                await cl.Message(content="⚠️ Student 超过最大循环次数未提交有效计划，已使用降级预案。").send()

            # --- 对抗模式：Teacher 审核循环 ---
            if self.adversarial_mode:
                teacher_history = []
                teacher_passed = False
                teacher_agent.clear_history()
                
                await cl.Message(content="👨‍🏫 **[Teacher Critic]** 收到 Student 草案，正在执行交叉验证...").send()

                # 第三层循环：Teacher 工具调用与计划审核
                for t_step_idx in range(self.max_plan_iterations):
                    is_final_step = ((t_step_idx+1) == self.max_plan_iterations)
                    workspace_tree = WorkspaceManager.get_workspace_state_recursive(self.workspace_dir, self.max_files)
                    
                    context = PlannerContextBuilder.build_teacher_context(
                        request_text, student_plan, workspace_tree, teacher_history, is_final_step
                    )

                    async with cl.Step(name=f"Teacher Critic Step {t_step_idx+1}") as step:
                        try:
                            resp, _ = await asyncio.to_thread(teacher_agent.get_response_stream, context, self.teacher_critic_prompt)
                        except Exception as e:
                            step.output = f"API 异常: {e}"
                            await asyncio.sleep(2)
                            continue

                        action_json = LLMAgent.robust_extract_json(resp)
                        if not action_json: continue

                        action = action_json.get("Action")
                        params = action_json.get("Action_Params", {})
                        step.output = f"**Thoughts:** {action_json.get('Thoughts', '')}\n**Action:** `{action}`"

                        if action == "EVALUATE_PLAN":
                            teacher_passed = params.get("passed", False)
                            teacher_feedback = params.get("feedback", "No feedback provided.")
                            break

                        # Teacher 同样可以使用工具探查环境
                        res = await self.tool_registry.execute(action, self, params, resp)
                        teacher_history.append({"action": action, "params": params, "result": res})

                if (not teacher_passed) and (adversarial_iter < self.max_adversarial_plan_iterations):
                    await cl.Message(content=f"❌ **Teacher 打回了计划！**\n**反馈意见:** {teacher_feedback}\n🔄 Student 即将重新制定计划...").send()
                    adversarial_iter = adversarial_iter + 1
                    user_feedback = "" # 清空用户反馈，专注于解决 Teacher 的反馈
                    continue # 回到第一层循环，让 Student 重新生成
                elif teacher_passed:
                    await cl.Message(content="✅ **Teacher 审核通过！** 认为计划逻辑严密可行。").send()
                elif adversarial_iter >= self.max_adversarial_plan_iterations:
                    await cl.Message(content="⚠️ **Teacher 超过最大反馈次数，仍未审核通过！**\n🔄 系统将使用 Student 原计划。").send()


            # --- 人工审核阶段 (Human-in-the-Loop) ---
            plan_str_markdown = "\n".join([f"**{i+1}.** {step}" for i, step in enumerate(student_plan)])
            
            # 使用 Chainlit 的 AskUserMessage 挂起后端并请求用户输入
            res = await cl.AskUserMessage(
                content=f"📝 **最终生成的执行计划草案**：\n\n{plan_str_markdown}\n\n👉 **请审核**：如果同意该计划，请输入 `y` 或 `yes`；如果认为需要调整，请直接输入您的**修改意见**，Agent 将根据您的意见重新制定计划。",
                timeout=3600 # 留给用户1小时的阅读和回复时间
            ).send()

            if res:
                user_reply = res['output'].strip()
                if user_reply.lower() in ['y', 'yes', 'ok', '同意', '通过']:
                    plan_approved = True
                    self.plan = student_plan
                    self.plan_mode = True
                    await cl.Message(content="🎉 用户审核通过！即将按照上述计划推进系统执行...").send()
                else:
                    user_feedback = user_reply
                    teacher_feedback = "" # 覆盖 Teacher 反馈，以用户最高优先级为主
                    await cl.Message(content=f"⚠️ **用户退回了计划！**\n**用户意见:** {user_feedback}\n🔄 正在发回 Student 重新规划...").send()
            else:
                # 超时处理
                plan_approved = True
                self.plan = student_plan
                self.plan_mode = True
                await cl.Message(content="⏳ 用户长时间未响应，系统默认计划通过，即将继续执行...").send()

    async def execute_workflow(self, orchestrator_system_prompt = None):
        os.makedirs(self.workspace_dir, exist_ok=True)
        WorkspaceManager.git_init(self.workspace_dir)
        await cl.Message(content=f"🚀 开始执行。工作目录: `{self.workspace_dir}` | 计划模式: {self.plan_mode}").send()

        # 读取或恢复状态
        logger.info(f"Loading state...")
        state = WorkspaceManager.load_state(self.workspace_dir)
        logger.info(f"State loaded")
        if state:
            self.rounds, self.action_history, self.summaries = state.get("rounds", 0), state.get("tool_calls_history", []), state.get("summaries", "")
            if self.plan_mode:
                self.plan = state.get('plan',[])
                self.plan_index = state.get('plan_index',0)
                if len(self.plan) > 0:
                    await cl.Message(content=f"🔄 系统从上次保存的计划恢复执行,原计划共有{len(self.plan)}步，从{self.plan_index+1}步开始继续执行").send()
            await cl.Message(content=f"🔄 系统从第 {self.rounds} 轮行动热启动恢复执行").send()
        elif os.path.exists(self.summary_txt):
            with open(self.summary_txt, 'r', encoding='utf-8', errors='ignore') as f: self.summaries = f.read()

        request_text = "No specific request found."
        req_file = os.path.join(self.workspace_dir, self.settings.get("request_file", "review.txt"))
        if os.path.exists(req_file):
            with open(req_file, 'r', encoding='utf-8') as f: request_text = f.read()

        orchestrator_sys_prompt = DEFAULT_ORCHESTRATOR_PROMPT
        if self.prompt_file and os.path.exists(self.prompt_file):
            with open(self.prompt_file, 'r', encoding='utf-8') as f: orchestrator_sys_prompt = f.read()

        # 生成计划
        if self.plan_mode and not self.plan:
            await self._generate_plan(request_text)

        # 主循环
        while self.rounds < self.max_rounds and not self.stop_workflow:
            self.rounds += 1
            self.orchestrator.clear_history()

            if self.user_interrupt_requests:
                new_reqs = "\n".join(self.user_interrupt_requests)
                request_text += f"\n\n【🚀 用户在运行中追加的紧急要求 ({time.strftime('%H:%M:%S')})】:\n{new_reqs}"
                self.user_interrupt_requests.clear()

            workspace_tree = WorkspaceManager.get_workspace_state_recursive(self.workspace_dir, self.max_files)
            hardware_status = SystemMonitor.get_hardware_status()
            
            active_tasks = self.task_manager.get_active_tasks()
            active_tasks_info = "当前没有正在运行的任务。" if not active_tasks else ""
            for tid, t in active_tasks.items():
                recent_logs = "".join(list(t.log_history)[-15:])
                if len(recent_logs) > 6000:
                    recent_logs = recent_logs[-5000:]
                active_tasks_info += f"\n--- [运行中] {tid} ({t.task_type}) (已运行 {int(time.time() - t.start_time)} 秒) ---\n最新日志片段:\n{recent_logs}\n"
            
            finished_tasks_info_list = []
            finished_tasks_info = ""
            for tid, t in self.task_manager.get_finished_tasks_and_clear().items():
                final_logs = "".join(t.log_history)
                finished_tasks_info += f"任务 {tid} 结束。状态: {t.status}. 结果: {t.result_summary}\n Logs:{final_logs}\n"
                if len(final_logs) > 6000:
                    final_logs = final_logs[-5000:]
                finished_tasks_info_list.append({'task_id': tid,'status': t.status,'result_summary': t.result_summary,'final_logs':final_logs})
                new_result = t.result_summary + final_logs
                
                # 检查 action_history 中是否已经存在相同的 task_id 且 result 完全一致的记录
                is_duplicate = any(
                    h.get("action") == "ASYNC_TASK_FINISH" and 
                    h.get("params", {}).get("task_id") == tid and 
                    h.get("result") == new_result 
                    for h in self.action_history
                )
                
                if not is_duplicate:
                    self.action_history.append({
                        "action": "ASYNC_TASK_FINISH", 
                        "params": {"task_id": tid}, 
                        "result": new_result
                    })
                
                # self.action_history.append({"action": "ASYNC_TASK_FINISH", "params": {"task_id": tid}, "result": t.result_summary+final_logs})
            finished_tasks_info_trunc = finished_tasks_info_list[-15:]
            finished_tasks_info = ""
            for info in finished_tasks_info_trunc:
                finished_tasks_info += f"任务 {info['task_id']} 结束。状态: {info['status']}. 结果: {info['result_summary']}\n Logs:{info['final_logs']}\n"
            
            # 利用抽离的 ContextBuilder 构建 Prompt
            context_prompt = self.context_builder.build_context(
                self, request_text, active_tasks_info, finished_tasks_info, workspace_tree, hardware_status
            )

            # 使用流式消息实时输出 LLM 的 Thoughts
            stream_msg = cl.Message(content="")
            await stream_msg.send()

            # ★ 在流式调用前，清除可能遗留的 interrupt_event
            #   (防止上一轮遗留的 set 状态导致本轮立即中断)
            was_interrupted = False

            async with cl.Step(name=f"Round {self.rounds} 思考与决策") as step:
                try:
                    resp, _ = await self.orchestrator.get_response_stream_async(
                        context_prompt, orchestrator_sys_prompt,
                        on_token_callback=stream_msg.stream_token,
                        cancel_event=self.interrupt_event
                    )
                    
                    await stream_msg.update()
                except Exception as e:
                    step.output = f"❌ API 调用失败: {e}，将在 5 秒后重试..."
                    try:
                        await asyncio.wait_for(self.interrupt_event.wait(), timeout=5.0)
                        self.interrupt_event.clear()
                    except asyncio.TimeoutError: pass
                    continue

                # ★ 如果流式过程中被用户中断，立刻跳过本轮 action 执行
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    was_interrupted = True
                    step.output = "⚡ 用户中断了本轮思考，即将响应新指令..."

                if was_interrupted:
                    await cl.Message(content="⚡ **已中断当前推理，正在处理您的新指令...**").send()
                    continue  # 直接跳到下一轮，在 while 循环顶部会读取 user_interrupt_requests
                    
                ################### This is a test #################
#                 resp = """
#                 ```json
# {
#     "Thoughts": "The training script `Train_geometric.py` previously failed due to Unicode encoding issues with emojis in a Windows environment. I have now removed the emojis and simplified the print statements. I will restart the training process to verify the implementation and begin recording results. The model uses a Set Transformer for context encoding and an INR-based velocity field with Fourier features and SDF values as geometric conditioning.",
#     "Action": "SPAWN_RUN",
#     "Action_Params": {
#         "run_script": "python Train_geometric.py"
#     }
# }
# ```
#                 """
                
                action_json = LLMAgent.robust_extract_json(resp)
                if not action_json:
                    step.output = "⚠️ 未能解析 JSON 指令"
                    self.action_history.append({"action": "PARSE_ERROR", "params": {}, "result": "Failed to parse JSON."})
                    continue

                action = action_json.get("Action")
                params = action_json.get("Action_Params", {})
                current_summary = action_json.get('summary', "")
                step.output = f"**Thoughts:** {action_json.get('Thoughts', '')}\n\n**Action:** `{action}`\n**Params:** \n```json\n{json.dumps(params, indent=2)}\n```"

            if current_summary:
                self.summaries += f"Round {self.rounds}: {current_summary}\n"
                with open(self.summary_txt, "a", encoding="utf-8") as f:
                    f.write(f"--- Round {self.rounds} ---\n{current_summary}\n\n")

            # 动态执行 Tool Registry
            res = await self.tool_registry.execute(action, self, params, resp)
            self.action_history.append({"action": action, "params": params, "result": res})

            WorkspaceManager.save_state(self.workspace_dir, self.rounds, self.action_history, self.summaries, self.plan_mode, self.plan, self.plan_index)

            if action not in ["WAIT", "SPAWN_CODER", "SPAWN_RUN", "FINISH"] and not self.stop_workflow:
                # ★ 短暂让出事件循环，让 Chainlit 有机会处理用户消息
                try:
                    await asyncio.wait_for(self.interrupt_event.wait(), timeout=0.5)
                    self.interrupt_event.clear()
                except asyncio.TimeoutError: pass

        # 结束时清理
        for tid in list(self.task_manager.tasks.keys()):
            self.task_manager.kill_task(tid)

# ==========================================
# 模块5: 界面交互与回调封装 (Chainlit UI Wrapper)
# ==========================================
class ChainlitUI:
    @staticmethod
    def select_directory():
        root = tk.Tk()
        root.withdraw() 
        root.attributes('-topmost', True) 
        folder_path = filedialog.askdirectory(title="选择工作空间路径")
        root.destroy()
        return folder_path

    @staticmethod
    async def run_orchestrator_workflow():
        settings = cl.user_session.get("settings", {})
        workspace_dir = cl.user_session.get("workspace_dir")
        interrupt_event = cl.user_session.get("interrupt_event")
        user_interrupt_requests = cl.user_session.get("user_interrupt_requests")
        
        system = AgentSystem(workspace_dir, settings, interrupt_event, user_interrupt_requests)
        cl.user_session.set("agent_system", system) # 可选，用于外部调试

        await system.execute_workflow()
        
        await cl.Message(content="🏁 **工作流安全退出**，所有后台子任务均已终止。").send()
        cl.user_session.set("is_running", False)

