from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .pipeline import PipelineState, RenWeavePipeline
from .provider import ModelProfile


@dataclass(slots=True)
class TranslationRequest:
    project: str
    workspace: str
    provider: str
    source_language: str
    target_language: str
    api_key: str = ""
    install: bool = False
    overwrite_existing: bool = False
    renpy_sdk: str = ""
    require_engine_validation: bool = False

    def validate(self) -> None:
        project = Path(self.project).expanduser()
        provider = Path(self.provider).expanduser()
        if not project.exists():
            raise ValueError("请选择存在的 Ren'Py 游戏目录或程序")
        if not self.workspace.strip():
            raise ValueError("请选择工作目录")
        if not provider.is_file():
            raise ValueError("请选择有效的模型配置 JSON")
        if not self.target_language.strip():
            raise ValueError("请输入目标语言代码或名称")
        if self.renpy_sdk and not Path(self.renpy_sdk).expanduser().exists():
            raise ValueError("指定的 Ren'Py SDK 路径不存在")


def execute_translation(
    request: TranslationRequest,
    *,
    pipeline_factory: Callable[[str], RenWeavePipeline] = RenWeavePipeline,
) -> PipelineState:
    """Execute the exact CLI pipeline; the API key remains memory-only."""
    request.validate()
    profile = ModelProfile.load(request.provider)
    if request.api_key:
        profile.api_key = request.api_key
    profile.validate()
    pipeline = pipeline_factory(request.workspace)
    state = pipeline.translate(
        request.project,
        request.source_language.strip() or "auto",
        request.target_language.strip(),
        profile,
        install=request.install,
        overwrite_existing=request.overwrite_existing,
        renpy_sdk_path=request.renpy_sdk or None,
        require_engine_validation=request.require_engine_validation,
    )
    if state.stage != "complete" or state.failed_scene_ids:
        detail = state.error or f"{len(state.failed_scene_ids)} 个场景未完成"
        raise RuntimeError(detail)
    return state


STAGE_LABELS = {
    "created": "准备任务",
    "discovered": "识别 Ren'Py 项目",
    "acquired": "安全采集与解包脚本",
    "decompiled": "反编译已完成",
    "indexed": "建立场景与剧情索引",
    "knowledge_ready": "确定性知识层已完成",
    "synthesizing": "理解世界观、角色与术语",
    "narrative_ready": "剧情知识层已完成",
    "translating": "按场景翻译",
    "validated": "场景译文校验完成",
    "refining": "跨场景一致性精修",
    "refined": "全局精修完成",
    "building": "生成 Ren'Py 语言脚本",
    "validating_build": "隔离验证生成脚本",
    "complete": "翻译包已完成",
    "failed": "任务失败",
}


class RenWeaveDesktopApp:
    def __init__(self, root, *, initial_project: str = "", initial_workspace: str = "") -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("RenWeave / 织译")
        self.root.geometry("850x720")
        self.root.minsize(760, 640)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_stage = ""

        self.project = tk.StringVar(value=initial_project)
        self.workspace = tk.StringVar(value=initial_workspace)
        self.provider = tk.StringVar()
        self.source_language = tk.StringVar(value="auto")
        self.target_language = tk.StringVar()
        self.api_key = tk.StringVar()
        self.renpy_sdk = tk.StringVar()
        self.install = tk.BooleanVar(value=False)
        self.require_engine = tk.BooleanVar(value=False)
        self.provider_summary = tk.StringVar(value="尚未导入模型配置")
        self.status = tk.StringVar(value="就绪：选择项目与模型后即可开始")

        self._build()
        if initial_project and not initial_workspace:
            self._suggest_workspace(initial_project)

    def _build(self) -> None:
        from tkinter import ttk

        outer = ttk.Frame(self.root, padding=24)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(12, weight=1)

        ttk.Label(outer, text="RenWeave / 织译", font=("Segoe UI", 22, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            outer,
            text="理解完整故事，自动完成解包、反编译、翻译、精修、验证与打包。",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 20))

        self._path_row(outer, 2, "游戏项目", self.project, self._browse_project)
        self._path_row(outer, 3, "工作目录", self.workspace, self._browse_workspace)
        self._path_row(outer, 4, "模型配置", self.provider, self._browse_provider)
        ttk.Label(outer, textvariable=self.provider_summary, foreground="#4b6478").grid(
            row=5, column=1, columnspan=2, sticky="w", pady=(0, 8)
        )

        ttk.Label(outer, text="源语言").grid(row=6, column=0, sticky="w", pady=6)
        ttk.Entry(outer, textvariable=self.source_language, width=24).grid(row=6, column=1, sticky="ew", pady=6)
        ttk.Label(outer, text="auto 可自动识别", foreground="#667788").grid(row=6, column=2, sticky="w", padx=(10, 0))
        ttk.Label(outer, text="目标语言").grid(row=7, column=0, sticky="w", pady=6)
        ttk.Entry(outer, textvariable=self.target_language, width=24).grid(row=7, column=1, sticky="ew", pady=6)
        ttk.Label(outer, text="任意语言代码或名称", foreground="#667788").grid(row=7, column=2, sticky="w", padx=(10, 0))
        ttk.Label(outer, text="API 密钥").grid(row=8, column=0, sticky="w", pady=6)
        ttk.Entry(outer, textvariable=self.api_key, show="•").grid(row=8, column=1, sticky="ew", pady=6)
        ttk.Label(outer, text="仅保存在本次运行内存", foreground="#667788").grid(row=8, column=2, sticky="w", padx=(10, 0))
        self._path_row(outer, 9, "Ren'Py SDK", self.renpy_sdk, self._browse_sdk, optional=True)

        options = ttk.Frame(outer)
        options.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        ttk.Checkbutton(options, text="完成后安装到游戏", variable=self.install).pack(side="left")
        ttk.Checkbutton(
            options,
            text="发布级：必须通过 Ren'Py 引擎编译验证",
            variable=self.require_engine,
        ).pack(side="left", padx=(20, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(8, 12))
        self.start_button = ttk.Button(actions, text="开始一键翻译", command=self._start)
        self.start_button.pack(side="left")
        self.progress = ttk.Progressbar(actions, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 0))

        log_frame = ttk.LabelFrame(outer, text="任务进度", padding=10)
        log_frame.grid(row=12, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, textvariable=self.status).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log = self.tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _path_row(self, parent, row, label, variable, command, *, optional: bool = False) -> None:
        display_label = f"{label}（可选）" if optional else label
        self.ttk.Label(parent, text=display_label).grid(row=row, column=0, sticky="w", pady=6)
        self.ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        self.ttk.Button(parent, text="选择…", command=command).grid(row=row, column=2, sticky="ew", padx=(10, 0), pady=6)

    def _browse_project(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title="选择 Ren'Py 游戏根目录或 game 目录")
        if selected:
            self.project.set(selected)
            if not self.workspace.get().strip():
                self._suggest_workspace(selected)

    def _suggest_workspace(self, project: str) -> None:
        source = Path(project).expanduser()
        name = source.parent.name if source.name.casefold() == "game" else source.name
        base = Path.home() / "Documents" / "RenWeaveWork"
        self.workspace.set(str(base / (name or "project")))

    def _browse_workspace(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title="选择或创建 RenWeave 工作目录", mustexist=False)
        if selected:
            self.workspace.set(selected)

    def _browse_provider(self) -> None:
        from tkinter import filedialog, messagebox

        selected = filedialog.askopenfilename(
            title="导入 OpenAI-compatible 模型配置",
            filetypes=(("JSON", "*.json"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        try:
            profile = ModelProfile.load(selected)
            profile.validate()
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("模型配置无效", str(exc))
            return
        self.provider.set(selected)
        self.provider_summary.set(f"{profile.name} · {profile.model} · {profile.base_url}")

    def _browse_sdk(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title="选择 Ren'Py SDK 目录")
        if selected:
            self.renpy_sdk.set(selected)

    def _request(self) -> TranslationRequest:
        return TranslationRequest(
            project=self.project.get().strip(),
            workspace=self.workspace.get().strip(),
            provider=self.provider.get().strip(),
            source_language=self.source_language.get().strip() or "auto",
            target_language=self.target_language.get().strip(),
            api_key=self.api_key.get(),
            install=self.install.get(),
            renpy_sdk=self.renpy_sdk.get().strip(),
            require_engine_validation=self.require_engine.get(),
        )

    def _start(self) -> None:
        from tkinter import messagebox

        if self.worker and self.worker.is_alive():
            return
        request = self._request()
        try:
            request.validate()
            profile = ModelProfile.load(request.provider)
            profile.validate()
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("无法开始", str(exc))
            return
        self.start_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set("正在启动一键翻译…")
        self.last_stage = ""
        self._append_log("任务已启动。所有分析与 AI 产物将写入独立工作目录。")
        self.worker = threading.Thread(target=self._run_worker, args=(request,), daemon=True)
        self.worker.start()
        self.root.after(350, self._poll)

    def _run_worker(self, request: TranslationRequest) -> None:
        try:
            state = execute_translation(request)
            self.events.put(("complete", state))
        except BaseException as exc:
            self.events.put(("error", exc))

    def _poll(self) -> None:
        from tkinter import messagebox

        state_path = Path(self.workspace.get().strip()) / "state.json"
        if state_path.is_file():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
                stage = str(payload.get("stage", ""))
                if stage and stage != self.last_stage:
                    self.last_stage = stage
                    label = STAGE_LABELS.get(stage, stage)
                    completed = len(payload.get("completed_scene_ids", []))
                    self.status.set(f"{label} · 已完成 {completed} 个场景")
                    self._append_log(label)
            except (OSError, ValueError, TypeError):
                pass
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            if self.worker and self.worker.is_alive():
                self.root.after(350, self._poll)
            return
        self.progress.stop()
        self.start_button.configure(state="normal")
        if kind == "complete":
            state = value
            self.status.set("完成：语言包目录与 RPA 归档均已生成")
            self._append_log(f"RPA：{state.package_path}")
            if state.installed_dir:
                self._append_log(f"已安装：{state.installed_dir}")
            messagebox.showinfo("RenWeave 已完成", f"语言包已生成：\n{state.package_path}")
        else:
            self.status.set("任务失败；工作区状态与日志已保留，可修复后重试")
            self._append_log(f"错误：{value}")
            messagebox.showerror("RenWeave 任务失败", str(value))

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def launch_gui(*, initial_project: str = "", initial_workspace: str = "") -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        raise RuntimeError("当前 Python 未安装 Tk，无法启动桌面界面") from exc
    try:
        root = tk.Tk()
    except Exception as exc:
        raise RuntimeError(f"无法启动桌面界面：{exc}") from exc
    RenWeaveDesktopApp(
        root,
        initial_project=initial_project,
        initial_workspace=initial_workspace,
    )
    root.mainloop()
    return 0


def main() -> int:
    return launch_gui()
