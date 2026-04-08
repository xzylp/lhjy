from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


class WindowsServiceGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ashare-system-v2 控制台")
        self.geometry("1260x860")
        self.minsize(1120, 760)

        self.project_dir = Path(__file__).resolve().parents[1]
        self.scripts_dir = self.project_dir / "scripts"
        self.manual_script = self.scripts_dir / "windows_service.ps1"
        self.unattended_script = self.scripts_dir / "start_unattended.ps1"
        self.stop_unattended_script = self.scripts_dir / "stop_unattended.ps1"
        self.ops_proxy_script = self.scripts_dir / "windows_ops_proxy.cmd"
        self.ops_proxy_vbs = self.scripts_dir / "windows_ops_proxy.vbs"
        self.ops_proxy_manifest = self.project_dir / ".ashare_state" / "ops_proxy_endpoints.json"
        self.ops_proxy_token_file = self.project_dir / ".ashare_state" / "ops_proxy_token.txt"
        self.log_dir = self.project_dir / "logs"
        self.icon_path = self.project_dir / "assets" / "icons" / "candlestick-chart.ico"
        self.env_file = self.project_dir / ".env"
        self.log_files = {
            "startup.log": self.log_dir / "startup.log",
            "api_service.log": self.log_dir / "api_service.log",
            "api_service.err": self.log_dir / "api_service.err",
            "scheduler.log": self.log_dir / "scheduler.log",
            "scheduler.err": self.log_dir / "scheduler.err",
        }
        if self.icon_path.exists():
            try:
                self.iconbitmap(default=str(self.icon_path))
            except Exception:
                pass

        self.bind_host_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="8100")
        self.no_scheduler_var = tk.BooleanVar(value=False)
        self.auto_refresh_logs_var = tk.BooleanVar(value=True)
        self.meeting_edit_mode_var = tk.BooleanVar(value=False)
        self.log_file_var = tk.StringVar(value="startup.log")
        self.ops_proxy_port_var = tk.StringVar(value="18791")
        self.test_args_var = tk.StringVar(value="tests/ -v")

        self.health_var = tk.StringVar(value="未检查")
        self.mode_var = tk.StringVar(value="-")
        self.service_var = tk.StringVar(value="-")
        self.environment_var = tk.StringVar(value="-")
        self.ops_proxy_var = tk.StringVar(value="未检查")
        self.readiness_var = tk.StringVar(value="-")
        self.account_id_var = tk.StringVar(value="-")
        self.exec_market_var = tk.StringVar(value="-")
        self.cash_asset_var = tk.StringVar(value="-")
        self.test_budget_var = tk.StringVar(value="-")
        self.repo_budget_var = tk.StringVar(value="-")
        self.position_pending_var = tk.StringVar(value="-")
        self.api_pid_var = tk.StringVar(value="-")
        self.scheduler_pid_var = tk.StringVar(value="-")
        self.watchdog_pid_var = tk.StringVar(value="-")
        self.last_update_var = tk.StringVar(value="-")
        self.readiness_summary_var = tk.StringVar(value="-")
        self.trade_date_var = tk.StringVar(value="-")
        self.config_mode_var = tk.StringVar(value="-")
        self.config_live_enable_var = tk.StringVar(value="-")
        self.config_env_var = tk.StringVar(value="-")
        self.mode_banner_var = tk.StringVar(value="配置加载中")
        self.mode_hint_var = tk.StringVar(value="等待服务状态")
        self.case_selector_var = tk.StringVar(value="")
        self.meeting_title_var = tk.StringVar(value="")
        self._case_option_map: dict[str, str] = {}

        self._queue: queue.Queue[tuple[str, str | dict]] = queue.Queue()
        self._refresh_running = False

        self._build_ui()
        self.after(200, self._drain_queue)
        self.after(300, self.refresh_state)

    def _build_ui(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        bg_main = "#0f111a"
        bg_panel = "#1a1d27"
        bg_btn = "#262b3d"
        fg_text = "#a6accd"
        fg_highlight = "#82aaff"
        fg_success = "#c3e88d"
        font_main = ("Microsoft YaHei UI", 10)
        font_mono = ("Consolas", 10)

        self.configure(bg=bg_main)
        
        style.configure(".", background=bg_main, foreground=fg_text, font=font_main)
        style.configure("TFrame", background=bg_main)
        style.configure("Panel.TFrame", background=bg_panel)
        style.configure("TLabelframe", background=bg_main, foreground=fg_highlight, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabelframe.Label", background=bg_main, foreground=fg_highlight)
        style.configure("TLabel", background=bg_main, foreground=fg_text)
        style.configure("Panel.TLabel", background=bg_panel)
        style.configure("TButton", background=bg_btn, foreground=fg_highlight, borderwidth=0, padding=4, focusthickness=3, focuscolor="none")
        style.map("TButton", background=[("active", "#313852")], foreground=[("active", "#ffffff")])
        style.configure("TCheckbutton", background=bg_main, foreground=fg_text)
        style.map("TCheckbutton", background=[("active", bg_main)])
        style.configure("TCombobox", fieldbackground=bg_btn, background=bg_btn, foreground=fg_text, borderwidth=0)
        style.configure("TNotebook", background=bg_main, borderwidth=0)
        style.configure("TNotebook.Tab", background=bg_btn, foreground=fg_text, padding=[12, 4], borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", bg_main)], foreground=[("selected", fg_highlight)])

        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        self.mode_banner = tk.Label(
            root,
            textvariable=self.mode_banner_var,
            anchor="w",
            justify="left",
            padx=16,
            pady=12,
            bg="#2f5d3a",
            fg="white",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.mode_banner.pack(fill=tk.X, pady=(0, 4))

        hint_label = tk.Label(root, textvariable=self.mode_hint_var, fg="#7a849e", bg=bg_main, anchor="w")
        hint_label.pack(fill=tk.X, pady=(0, 10))

        main_paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True)

        left_frame = ttk.Frame(main_paned)
        right_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=1)
        main_paned.add(right_frame, weight=3)

        config_frame = ttk.LabelFrame(left_frame, text="配置")
        config_frame.pack(fill=tk.X, pady=(0, 10))
        config_frame.columnconfigure(1, weight=1)

        project_text = tk.StringVar(value=str(self.project_dir))
        ttk.Label(config_frame, text="项目路径").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(config_frame, state="readonly", textvariable=project_text).grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=6)

        ttk.Label(config_frame, text="BindHost").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(config_frame, textvariable=self.bind_host_var, width=15).grid(row=1, column=1, sticky="w", padx=6, pady=6)
        ttk.Label(config_frame, text="Port").grid(row=1, column=2, sticky="w", padx=6, pady=6)
        ttk.Entry(config_frame, textvariable=self.port_var, width=8).grid(row=1, column=3, sticky="w", padx=6, pady=6)
        ttk.Label(config_frame, text="OpsPort").grid(row=2, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(config_frame, textvariable=self.ops_proxy_port_var, width=8).grid(row=2, column=1, sticky="w", padx=6, pady=6)

        ttk.Checkbutton(config_frame, text="启动时不拉起 scheduler", variable=self.no_scheduler_var).grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        ttk.Label(config_frame, text="配置模式:").grid(row=4, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, textvariable=self.config_mode_var).grid(row=4, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, text="实盘提交:").grid(row=5, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, textvariable=self.config_live_enable_var).grid(row=5, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, text="部署环境:").grid(row=6, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, textvariable=self.config_env_var).grid(row=6, column=1, sticky="w", padx=6, pady=2)

        config_btn_frame = ttk.Frame(config_frame)
        config_btn_frame.grid(row=7, column=0, columnspan=4, sticky="ew", padx=6, pady=8)
        ttk.Button(config_btn_frame, text="切 Paper", command=lambda: self.switch_mode("paper")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        ttk.Button(config_btn_frame, text="切 Live", command=lambda: self.switch_mode("live")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(config_btn_frame, text="环境 Dev", command=lambda: self.switch_environment("dev")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(config_btn_frame, text="环境 Prod", command=lambda: self.switch_environment("prod")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        action_frame = ttk.LabelFrame(left_frame, text="服务控制")
        action_frame.pack(fill=tk.X, pady=(0, 10))
        for index in range(3):
            action_frame.columnconfigure(index, weight=1)
        ttk.Button(action_frame, text="启动服务", command=lambda: self.run_manual_action("start")).grid(row=0, column=0, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="停止服务", command=lambda: self.run_manual_action("stop")).grid(row=0, column=1, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="重启服务", command=lambda: self.run_manual_action("restart")).grid(row=0, column=2, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="拉起 Watchdog", command=self.start_watchdog).grid(row=1, column=0, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="停止 Watchdog", command=self.stop_watchdog).grid(row=1, column=1, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="刷新状态", command=self.refresh_state).grid(row=1, column=2, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="启动代理", command=self.start_ops_proxy).grid(row=2, column=0, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="停止代理", command=self.stop_ops_proxy).grid(row=2, column=1, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="运行测试", command=self.run_tests_via_proxy).grid(row=2, column=2, padx=4, pady=6, sticky="ew")
        ttk.Entry(action_frame, textvariable=self.test_args_var).grid(row=3, column=0, columnspan=3, padx=4, pady=(0, 6), sticky="ew")

        quick_frame = ttk.LabelFrame(left_frame, text="快捷入口")
        quick_frame.pack(fill=tk.X, pady=(0, 10))
        quick_btn_frame1 = ttk.Frame(quick_frame)
        quick_btn_frame1.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(quick_btn_frame1, text="日志目录", command=lambda: self.open_path(self.log_dir)).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(quick_btn_frame1, text="API 日志", command=lambda: self.open_path(self.log_dir / "api_service.log")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        quick_btn_frame2 = ttk.Frame(quick_frame)
        quick_btn_frame2.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(quick_btn_frame2, text="Scheduler 日志", command=lambda: self.open_path(self.log_dir / "scheduler.log")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(quick_btn_frame2, text="Startup 日志", command=lambda: self.open_path(self.log_dir / "startup.log")).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        status_frame = ttk.LabelFrame(left_frame, text="运行状态")
        status_frame.pack(fill=tk.BOTH, expand=True)
        status_frame.columnconfigure(1, weight=1)
        
        rows = [
            ("Health", self.health_var), ("Mode", self.mode_var),
            ("Service", self.service_var), ("Environment", self.environment_var),
            ("OpsProxy", self.ops_proxy_var),
            ("Readiness", self.readiness_var), ("Account", self.account_id_var),
            ("Exec/Market", self.exec_market_var), ("Cash/Asset", self.cash_asset_var),
            ("TestBudget", self.test_budget_var), ("RepoBudget", self.repo_budget_var),
            ("Positions/Pending", self.position_pending_var), ("TradeDate", self.trade_date_var),
            ("API PIDs", self.api_pid_var), ("Scheduler PIDs", self.scheduler_pid_var),
            ("Watchdog PIDs", self.watchdog_pid_var), ("LastUpdate", self.last_update_var),
        ]
        for row_index, (label_text, var) in enumerate(rows):
            ttk.Label(status_frame, text=label_text).grid(row=row_index, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(status_frame, textvariable=var, foreground=fg_success).grid(row=row_index, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(status_frame, text="Readiness摘要").grid(row=len(rows), column=0, sticky="nw", padx=6, pady=2)
        ttk.Label(status_frame, textvariable=self.readiness_summary_var, wraplength=200, justify="left", foreground=fg_success).grid(row=len(rows), column=1, sticky="w", padx=6, pady=2)

        # Right side: Output & Info Panel
        right_paned = ttk.Panedwindow(right_frame, orient=tk.VERTICAL)
        right_paned.pack(fill=tk.BOTH, expand=True, padx=(10, 0))

        notebook_frame = ttk.Frame(right_paned)
        command_frame = ttk.LabelFrame(right_paned, text="命令输出面板")
        right_paned.add(notebook_frame, weight=3) # Info panel gets more weight
        right_paned.add(command_frame, weight=1)

        notebook = ttk.Notebook(notebook_frame)
        notebook.pack(fill=tk.BOTH, expand=True)
        
        def make_dark_text(parent) -> tk.Text:
            t = tk.Text(parent, wrap="word", bg="#0a0c12", fg=fg_success, insertbackground=fg_highlight, font=font_mono, selectbackground="#313852", borderwidth=0, padx=8, pady=8)
            return t

        log_tab = ttk.Frame(notebook)
        notebook.add(log_tab, text="日志监控")
        log_toolbar = ttk.Frame(log_tab)
        log_toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(log_toolbar, text="选择日志:").pack(side=tk.LEFT)
        selector = ttk.Combobox(log_toolbar, textvariable=self.log_file_var, values=list(self.log_files.keys()), state="readonly", width=15)
        selector.pack(side=tk.LEFT, padx=6)
        selector.bind("<<ComboboxSelected>>", lambda _event: self.refresh_state())
        ttk.Checkbutton(log_toolbar, text="自动刷新", variable=self.auto_refresh_logs_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(log_toolbar, text="刷新", command=self.refresh_state).pack(side=tk.LEFT, padx=6)
        ttk.Button(log_toolbar, text="外部打开", command=self.open_selected_log).pack(side=tk.LEFT, padx=6)
        
        self.log_preview = make_dark_text(log_tab)
        self.log_preview.configure(wrap="none")
        self.log_preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=(0, 6))
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_preview.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 6))
        self.log_preview.configure(yscrollcommand=log_scroll.set)

        meeting_tab = ttk.Frame(notebook)
        notebook.add(meeting_tab, text="会议纪要")
        meeting_toolbar = ttk.Frame(meeting_tab)
        meeting_toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(meeting_toolbar, text="生成纪要", command=self.start_meeting_flow).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(meeting_toolbar, text="保存纪要", command=self.save_meeting_flow).pack(side=tk.LEFT, padx=6)
        ttk.Button(meeting_toolbar, text="载入草稿", command=self.load_meeting_draft).pack(side=tk.LEFT, padx=6)
        ttk.Button(meeting_toolbar, text="刷新", command=self.refresh_state).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(meeting_toolbar, text="暂停覆盖", variable=self.meeting_edit_mode_var).pack(side=tk.RIGHT, padx=6)
        ttk.Entry(meeting_toolbar, textvariable=self.meeting_title_var).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=6)
        
        self.meeting_text = make_dark_text(meeting_tab)
        self.meeting_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=(0, 6))
        meeting_scroll = ttk.Scrollbar(meeting_tab, orient="vertical", command=self.meeting_text.yview)
        meeting_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 6))
        self.meeting_text.configure(yscrollcommand=meeting_scroll.set)

        pool_tab = ttk.Frame(notebook)
        notebook.add(pool_tab, text="选股池与观点")
        pool_toolbar = ttk.Frame(pool_tab)
        pool_toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(pool_toolbar, text="查阅单票:").pack(side=tk.LEFT)
        self.case_selector = ttk.Combobox(pool_toolbar, textvariable=self.case_selector_var, state="readonly", width=15)
        self.case_selector.pack(side=tk.LEFT, padx=6)
        ttk.Button(pool_toolbar, text="明细", command=self.show_selected_case_detail).pack(side=tk.LEFT, padx=6)
        ttk.Button(pool_toolbar, text="刷新", command=self.refresh_state).pack(side=tk.LEFT, padx=6)
        
        pool_paned = ttk.Panedwindow(pool_tab, orient=tk.HORIZONTAL)
        pool_paned.pack(fill=tk.BOTH, expand=True)
        pool_left = ttk.Frame(pool_paned)
        pool_right = ttk.Frame(pool_paned)
        pool_paned.add(pool_left, weight=1)
        pool_paned.add(pool_right, weight=1)

        def add_scrollable_text(parent) -> tk.Text:
            t = make_dark_text(parent)
            t.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
            s = ttk.Scrollbar(parent, orient="vertical", command=t.yview)
            s.pack(side=tk.RIGHT, fill=tk.Y, pady=6)
            t.configure(yscrollcommand=s.set)
            return t

        ttk.Label(pool_left, text="选股池摘要").pack(anchor="w", padx=6)
        self.pool_text = add_scrollable_text(pool_left)
        ttk.Label(pool_right, text="单票投票明细").pack(anchor="w", padx=6)
        self.case_detail_text = add_scrollable_text(pool_right)

        self.output = make_dark_text(command_frame)
        self.output.configure(bg="#050608", fg="#82aaff")
        self.output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
        output_scroll = ttk.Scrollbar(command_frame, orient="vertical", command=self.output.yview)
        output_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=6)
        self.output.configure(yscrollcommand=output_scroll.set)

        self._append_output("SYS> GUI 启动成功。已切换至量化分析界面的深色科技风格。可以查看状态、操作服务、获取选股与日志。")

    def _append_output(self, text: str) -> None:
        self.output.insert(tk.END, text.rstrip() + "\n")
        self.output.see(tk.END)

    def _set_text_widget(self, widget: tk.Text, text: str, *, allow_when_focused: bool = False) -> None:
        normalized = text if text.endswith("\n") else text + "\n"
        current = widget.get("1.0", tk.END)
        if current == normalized:
            return
        if not allow_when_focused and self.focus_get() is widget:
            return
        yview = widget.yview()
        xview = widget.xview()
        insert_index = widget.index(tk.INSERT)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        try:
            widget.yview_moveto(yview[0])
            widget.xview_moveto(xview[0])
            if allow_when_focused:
                widget.mark_set(tk.INSERT, insert_index)
        except Exception:
            pass

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_output(str(payload))
            elif kind == "state":
                self._apply_state(payload if isinstance(payload, dict) else {})
        self.after(200, self._drain_queue)

    def _apply_state(self, payload: dict) -> None:
        health = payload.get("health") or {}
        ops_proxy = payload.get("ops_proxy") or {}
        readiness = payload.get("readiness") or {}
        settings = payload.get("settings") or {}
        account_state = payload.get("account_state") or {}
        account_metrics = account_state.get("metrics") or {}
        inspection = readiness.get("pending_order_inspection") or {}
        processes = payload.get("processes") or {}
        latest_case = payload.get("latest_case") or {}
        meeting = payload.get("meeting") or {}
        vote_board = payload.get("vote_board") or {}

        self.health_var.set(health.get("status", "unavailable"))
        self.mode_var.set(health.get("mode", "-"))
        self.service_var.set(health.get("service", "-"))
        self.environment_var.set(health.get("environment", settings.get("environment", "-")))
        self.ops_proxy_var.set(ops_proxy.get("status", "unavailable"))
        self.readiness_var.set(readiness.get("status", "-"))
        self.account_id_var.set(str(readiness.get("account_id") or account_state.get("account_id") or "-"))
        self.exec_market_var.set(f"{settings.get('execution_mode', '-')} / {settings.get('market_mode', '-')}")

        cash = account_metrics.get("cash")
        total_asset = account_metrics.get("total_asset")
        if isinstance(cash, (int, float)) and isinstance(total_asset, (int, float)):
            self.cash_asset_var.set(f"{cash:.2f} / {total_asset:.2f}")
        else:
            self.cash_asset_var.set("-")

        stock_budget = account_metrics.get("stock_test_budget_amount")
        stock_budget_remaining = account_metrics.get("available_test_trade_value")
        if isinstance(stock_budget, (int, float)) and isinstance(stock_budget_remaining, (int, float)):
            self.test_budget_var.set(f"{stock_budget_remaining:.2f} / {stock_budget:.2f}")
        else:
            self.test_budget_var.set("-")

        reverse_repo_value = account_metrics.get("reverse_repo_value")
        reverse_repo_reserved_amount = account_metrics.get("reverse_repo_reserved_amount")
        if isinstance(reverse_repo_value, (int, float)) and isinstance(reverse_repo_reserved_amount, (int, float)):
            self.repo_budget_var.set(f"{reverse_repo_value:.2f} / {reverse_repo_reserved_amount:.2f}")
        else:
            self.repo_budget_var.set("-")

        position_count = account_state.get("position_count")
        if isinstance(position_count, int):
            self.position_pending_var.set(
                f"positions={position_count} pending={inspection.get('pending_count', 0)} warning={inspection.get('warning_count', 0)}"
            )
        else:
            self.position_pending_var.set("-")

        self.trade_date_var.set(latest_case.get("trade_date", "-"))
        env_config = payload.get("env_config") or {}
        self.config_mode_var.set(str(env_config.get("ASHARE_RUN_MODE", "-")))
        self.config_live_enable_var.set(str(env_config.get("ASHARE_LIVE_ENABLE", "-")))
        self.config_env_var.set(str(env_config.get("ASHARE_ENV", "-")))
        self.api_pid_var.set(self._format_pid_list(processes.get("api_pids")))
        self.scheduler_pid_var.set(self._format_pid_list(processes.get("scheduler_pids")))
        self.watchdog_pid_var.set(self._format_pid_list(processes.get("watchdog_pids")))
        self.last_update_var.set(payload.get("updated_at", "-"))
        self._update_mode_banner(
            health_status=str(health.get("status", "unavailable")),
            service_mode=str(health.get("mode", "-")),
            config_mode=str(env_config.get("ASHARE_RUN_MODE", "-")),
            live_enable=str(env_config.get("ASHARE_LIVE_ENABLE", "-")),
            environment=str(env_config.get("ASHARE_ENV", "-")),
        )

        summary_lines = readiness.get("summary_lines") or []
        self.readiness_summary_var.set(" | ".join(summary_lines[:2]) if summary_lines else "-")

        self._set_text_widget(self.log_preview, str(payload.get("log_preview", "")))
        if not self.meeting_edit_mode_var.get():
            self._set_text_widget(self.meeting_text, str(payload.get("meeting_text", "")))
        self._set_text_widget(self.pool_text, str(payload.get("pool_text", "")))
        self._set_text_widget(self.case_detail_text, str(payload.get("case_detail_text", "请选择一只股票查看投票明细。")))
        if not self.meeting_edit_mode_var.get():
            self.meeting_title_var.set(str(meeting.get("title") or f"{self.trade_date_var.get()} 选股讨论会"))
        self._update_case_selector(vote_board.get("items") or [])

    @staticmethod
    def _format_pid_list(values: list[int] | None) -> str:
        if not values:
            return "-"
        return ", ".join(str(item) for item in values)

    def _parse_port(self, show_error: bool) -> int | None:
        try:
            return int(self.port_var.get().strip())
        except ValueError:
            if show_error:
                messagebox.showerror("参数错误", "Port 必须是整数。")
            return None

    def _validated_port(self) -> int | None:
        return self._parse_port(show_error=True)

    def _run_in_thread(self, target) -> None:
        threading.Thread(target=target, daemon=True).start()

    @staticmethod
    def _hidden_subprocess_kwargs(extra_creationflags: int = 0) -> dict:
        kwargs: dict = {}
        if sys.platform.startswith("win"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | extra_creationflags
        return kwargs

    def _powershell_file(self, script: Path, extra_args: list[str]) -> subprocess.CompletedProcess[str]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *extra_args,
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **self._hidden_subprocess_kwargs(),
        )

    def _powershell_command(self, command_text: str) -> subprocess.CompletedProcess[str]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command_text,
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **self._hidden_subprocess_kwargs(),
        )

    def run_manual_action(self, action: str) -> None:
        port = self._validated_port()
        if port is None:
            return

        def worker() -> None:
            if self._ops_proxy_available():
                result = self._ops_proxy_request(
                    "/actions/service",
                    {
                        "action": action,
                        "bind_host": self.bind_host_var.get().strip(),
                        "port": port,
                        "no_scheduler": self.no_scheduler_var.get(),
                    },
                    method="POST",
                )
                self._queue.put(("log", f"> ops-proxy service action={action}"))
                self._queue.put(("log", json.dumps(result, ensure_ascii=False, indent=2) if result else "ops proxy 请求失败"))
                self._refresh_state_sync()
                return
            args = [
                "-Action",
                action,
                "-ProjectDir",
                str(self.project_dir),
                "-BindHost",
                self.bind_host_var.get().strip(),
                "-Port",
                str(port),
            ]
            if self.no_scheduler_var.get():
                args.append("-NoScheduler")
            self._queue.put(("log", f"> windows_service.ps1 -Action {action}"))
            result = self._powershell_file(self.manual_script, args)
            output = (result.stdout or "") + (result.stderr or "")
            self._queue.put(("log", output.strip() if output.strip() else f"{action} 执行完成，无额外输出。"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def start_watchdog(self) -> None:
        port = self._validated_port()
        if port is None:
            return

        def worker() -> None:
            if self._ops_proxy_available():
                result = self._ops_proxy_request(
                    "/actions/watchdog",
                    {
                        "action": "start",
                        "no_scheduler": self.no_scheduler_var.get(),
                    },
                    method="POST",
                )
                self._queue.put(("log", f"> ops-proxy watchdog start"))
                self._queue.put(("log", json.dumps(result, ensure_ascii=False, indent=2) if result else "ops proxy 请求失败"))
                self._refresh_state_sync()
                return
            state = self._collect_state()
            watchdog_pids = (state.get("processes") or {}).get("watchdog_pids") or []
            if watchdog_pids:
                self._queue.put(("log", f"Watchdog 已在运行: {self._format_pid_list(watchdog_pids)}"))
                self._queue.put(("state", state))
                return

            args = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.unattended_script),
                "-BindHost",
                self.bind_host_var.get().strip(),
                "-Port",
                str(port),
            ]
            if self.no_scheduler_var.get():
                args.append("-NoScheduler")

            creationflags = 0
            for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
                creationflags |= getattr(subprocess, flag_name, 0)
            try:
                popen_kwargs = {"cwd": str(self.project_dir)}
                popen_kwargs.update(self._hidden_subprocess_kwargs(extra_creationflags=creationflags))
                subprocess.Popen(args, **popen_kwargs)
                self._queue.put(("log", "Watchdog 已发起。日志请看 logs/startup.log"))
            except Exception as exc:
                self._queue.put(("log", f"启动 Watchdog 失败: {exc}"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def stop_watchdog(self) -> None:
        def worker() -> None:
            if self._ops_proxy_available():
                result = self._ops_proxy_request(
                    "/actions/watchdog",
                    {"action": "stop"},
                    method="POST",
                )
                self._queue.put(("log", "> ops-proxy watchdog stop"))
                self._queue.put(("log", json.dumps(result, ensure_ascii=False, indent=2) if result else "ops proxy 请求失败"))
                self._refresh_state_sync()
                return
            command = (
                "$p = @(Get-CimInstance Win32_Process -ErrorAction Stop | "
                "Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*start_unattended.ps1*' } | "
                "Select-Object -ExpandProperty ProcessId); "
                "if ($p.Count -eq 0) { Write-Output 'no watchdog process'; exit 0 }; "
                "$p | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction Stop; Write-Output ('stopped watchdog PID=' + $_) }"
            )
            self._queue.put(("log", "> stop watchdog"))
            result = self._powershell_command(command)
            output = (result.stdout or "") + (result.stderr or "")
            self._queue.put(("log", output.strip() if output.strip() else "停止 Watchdog 完成。"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def start_ops_proxy(self) -> None:
        def worker() -> None:
            if self._ops_proxy_available():
                self._queue.put(("log", "Ops Proxy 已在运行。"))
                self._refresh_state_sync()
                return
            try:
                args = [str(self.ops_proxy_script), "--port", str(self._parse_ops_proxy_port() or 18791)]
                creationflags = 0
                for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
                    creationflags |= getattr(subprocess, flag_name, 0)
                popen_kwargs = {"cwd": str(self.project_dir)}
                popen_kwargs.update(self._hidden_subprocess_kwargs(extra_creationflags=creationflags))
                subprocess.Popen(args, **popen_kwargs)
                self._queue.put(("log", "Ops Proxy 已发起启动。"))
            except Exception as exc:
                self._queue.put(("log", f"启动 Ops Proxy 失败: {exc}"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def stop_ops_proxy(self) -> None:
        def worker() -> None:
            command = (
                "$p = @(Get-CimInstance Win32_Process -ErrorAction Stop | "
                "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*windows_ops_proxy.py*' } | "
                "Select-Object -ExpandProperty ProcessId); "
                "if ($p.Count -eq 0) { Write-Output 'no ops proxy process'; exit 0 }; "
                "$p | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction Stop; Write-Output ('stopped ops proxy PID=' + $_) }"
            )
            result = self._powershell_command(command)
            output = (result.stdout or "") + (result.stderr or "")
            self._queue.put(("log", output.strip() if output.strip() else "停止 Ops Proxy 完成。"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def run_tests_via_proxy(self) -> None:
        test_args = [item for item in self.test_args_var.get().strip().split() if item]

        def worker() -> None:
            if not self._ops_proxy_available():
                self._queue.put(("log", "Ops Proxy 未运行，无法通过代理执行测试。"))
                return
            result = self._ops_proxy_request(
                "/actions/tests",
                {"args": test_args},
                method="POST",
            )
            self._queue.put(("log", f"> ops-proxy tests {' '.join(test_args) if test_args else '(default)'}"))
            self._queue.put(("log", json.dumps(result, ensure_ascii=False, indent=2) if result else "ops proxy 请求失败"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def refresh_state(self) -> None:
        if self._refresh_running:
            self.after(5000, self.refresh_state)
            return
        self._run_in_thread(self._refresh_state_sync)

    def start_meeting_flow(self) -> None:
        port = self._validated_port()
        if port is None:
            return

        def worker() -> None:
            workspace_context = self._fetch_workspace_context(port)
            discussion_context = (workspace_context.get("discussion_context") if workspace_context else {}) or self._fetch_discussion_context(port)
            trade_date = self._resolve_trade_date(workspace_context, discussion_context)
            readiness = self._fetch_readiness(port)
            account_state = self._fetch_account_state(port, readiness.get("account_id") if readiness else None)
            vote_board = self._build_vote_board_from_context(discussion_context, trade_date)
            if not vote_board:
                vote_board = self._fetch_vote_board(port, trade_date)

            payload = self._build_meeting_payload(
                trade_date,
                readiness,
                account_state=account_state,
                discussion_context=discussion_context,
                workspace_context=workspace_context,
                vote_board=vote_board,
            )
            if not payload:
                latest_case = self._fetch_latest_case(port)
                trade_date = latest_case.get("trade_date")
                summary = self._fetch_discussion_summary(port, trade_date)
                reply_pack = self._fetch_reply_pack(port, trade_date)
                vote_board = self._fetch_vote_board(port, trade_date)
                payload = self._build_meeting_payload(
                    trade_date,
                    readiness,
                    account_state=account_state,
                    summary=summary,
                    reply_pack=reply_pack,
                    vote_board=vote_board,
                )
            if not payload:
                self._queue.put(("log", "生成会议纪要失败：当前没有交易日或选股池摘要。"))
                return

            result = self._post_json(f"http://127.0.0.1:{port}/system/meetings/record", payload)
            if not result or not result.get("ok"):
                self._queue.put(("log", f"会议纪要写入失败: {result or 'request_failed'}"))
                return

            meeting = result.get("meeting", {})
            self._queue.put(("log", f"会议纪要已生成: {meeting.get('title', '-') }"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def save_meeting_flow(self) -> None:
        port = self._validated_port()
        if port is None:
            return

        def worker() -> None:
            trade_date = self.trade_date_var.get().strip()
            payload = self._build_manual_meeting_payload(trade_date)
            if not payload:
                self._queue.put(("log", "保存会议纪要失败：当前内容为空。"))
                return
            result = self._post_json(f"http://127.0.0.1:{port}/system/meetings/record", payload)
            if not result or not result.get("ok"):
                self._queue.put(("log", f"保存会议纪要失败: {result or 'request_failed'}"))
                return
            meeting = result.get("meeting", {})
            self.meeting_edit_mode_var.set(False)
            self._queue.put(("log", f"会议纪要已保存: {meeting.get('title', '-') }"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def load_meeting_draft(self) -> None:
        port = self._validated_port()
        if port is None:
            return

        def worker() -> None:
            workspace_context = self._fetch_workspace_context(port)
            discussion_context = (workspace_context.get("discussion_context") if workspace_context else {}) or self._fetch_discussion_context(port)
            trade_date = self._resolve_trade_date(workspace_context, discussion_context)
            readiness = self._fetch_readiness(port)
            account_state = self._fetch_account_state(port, readiness.get("account_id") if readiness else None)
            vote_board = self._build_vote_board_from_context(discussion_context, trade_date)
            if not vote_board:
                vote_board = self._fetch_vote_board(port, trade_date)
            payload = self._build_meeting_payload(
                trade_date,
                readiness,
                account_state=account_state,
                discussion_context=discussion_context,
                workspace_context=workspace_context,
                vote_board=vote_board,
            )
            if not payload:
                latest_case = self._fetch_latest_case(port)
                trade_date = latest_case.get("trade_date")
                summary = self._fetch_discussion_summary(port, trade_date)
                reply_pack = self._fetch_reply_pack(port, trade_date)
                vote_board = self._fetch_vote_board(port, trade_date)
                payload = self._build_meeting_payload(
                    trade_date,
                    readiness,
                    account_state=account_state,
                    summary=summary,
                    reply_pack=reply_pack,
                    vote_board=vote_board,
                )
            if not payload:
                self._queue.put(("log", "载入会议草稿失败：当前没有交易日或选股池摘要。"))
                return
            self.meeting_edit_mode_var.set(True)
            current = self._collect_state()
            current["meeting_text"] = payload.get("summary", "")
            current["meeting"] = {"title": payload.get("title", "")}
            self._queue.put(("state", current))
            self._queue.put(("log", "已载入当前选股池会议草稿，可继续手工编辑。"))

        self._run_in_thread(worker)

    def show_selected_case_detail(self) -> None:
        port = self._validated_port()
        if port is None:
            return
        selector = self.case_selector_var.get().strip()
        case_id = self._case_option_map.get(selector)
        if not case_id:
            messagebox.showinfo("提示", "请先选择一只股票。")
            return

        def worker() -> None:
            payload = self._fetch_case_vote_detail(port, case_id)
            if not payload:
                current = self._collect_state()
                items = (current.get("vote_board") or {}).get("items") or []
                fallback_case = next((item for item in items if item.get("case_id") == case_id), None)
                if not fallback_case:
                    self._queue.put(("log", f"获取单票投票明细失败: {case_id}"))
                    return
                payload = {"case": fallback_case}
            detail_text = self._format_case_detail_text(payload)
            current = self._collect_state()
            current["case_detail_text"] = detail_text
            self._queue.put(("state", current))

        self._run_in_thread(worker)

    def switch_mode(self, mode: str) -> None:
        port = self._validated_port()
        if port is None:
            return
        target_mode = mode.strip().lower()
        if target_mode not in {"paper", "live"}:
            messagebox.showerror("参数错误", f"不支持的模式: {mode}")
            return
        tip = "切到 live 会重启服务，并启用实盘提交开关。" if target_mode == "live" else "切到 paper 会重启服务，并关闭实盘提交开关。"
        if not messagebox.askyesno("确认切换", tip):
            return

        def worker() -> None:
            env_updates = {
                "ASHARE_RUN_MODE": target_mode,
                "ASHARE_LIVE_ENABLE": "true" if target_mode == "live" else "false",
            }
            self._write_env_updates(env_updates)
            self._queue.put(("log", f"已写入 .env: mode={target_mode} live_enable={env_updates['ASHARE_LIVE_ENABLE']}"))
            args = [
                "-Action",
                "restart",
                "-ProjectDir",
                str(self.project_dir),
                "-BindHost",
                self.bind_host_var.get().strip(),
                "-Port",
                str(port),
            ]
            if self.no_scheduler_var.get():
                args.append("-NoScheduler")
            result = self._powershell_file(self.manual_script, args)
            output = (result.stdout or "") + (result.stderr or "")
            self._queue.put(("log", output.strip() if output.strip() else f"已重启服务并切到 {target_mode}。"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def switch_environment(self, environment: str) -> None:
        port = self._validated_port()
        if port is None:
            return
        target_environment = environment.strip().lower()
        if target_environment not in {"dev", "prod"}:
            messagebox.showerror("参数错误", f"不支持的环境: {environment}")
            return
        tip = (
            "切到 prod 会重启服务。注意：当前代码里 prod 主要是部署标签，不会单独决定是否实盘。"
            if target_environment == "prod"
            else "切到 dev 会重启服务，并保留当前 paper/live 配置。"
        )
        if not messagebox.askyesno("确认切换环境", tip):
            return

        def worker() -> None:
            self._write_env_updates({"ASHARE_ENV": target_environment})
            self._queue.put(("log", f"已写入 .env: environment={target_environment}"))
            args = [
                "-Action",
                "restart",
                "-ProjectDir",
                str(self.project_dir),
                "-BindHost",
                self.bind_host_var.get().strip(),
                "-Port",
                str(port),
            ]
            if self.no_scheduler_var.get():
                args.append("-NoScheduler")
            result = self._powershell_file(self.manual_script, args)
            output = (result.stdout or "") + (result.stderr or "")
            self._queue.put(("log", output.strip() if output.strip() else f"已重启服务并切到 {target_environment}。"))
            self._refresh_state_sync()

        self._run_in_thread(worker)

    def _refresh_state_sync(self) -> None:
        self._refresh_running = True
        try:
            self._queue.put(("state", self._collect_state()))
        finally:
            self._refresh_running = False
            self.after(5000, self.refresh_state)

    def _collect_state(self) -> dict:
        port = self._parse_port(show_error=False)
        workspace_context = self._fetch_workspace_context(port)
        discussion_context = (workspace_context.get("discussion_context") if workspace_context else {}) or self._fetch_discussion_context(port)
        runtime_context = (workspace_context.get("runtime_context") if workspace_context else {}) or self._fetch_runtime_context(port)
        monitor_context = (workspace_context.get("monitor_context") if workspace_context else {}) or self._fetch_monitor_context(port)
        latest_case = self._resolve_latest_case(workspace_context, discussion_context, runtime_context, port)
        trade_date = latest_case.get("trade_date")
        readiness = self._fetch_readiness(port)
        account_id = readiness.get("account_id") if readiness else None
        account_state = self._fetch_account_state(port, account_id)
        meeting = self._fetch_meeting(port)
        vote_board = self._build_vote_board_from_context(discussion_context, trade_date)
        if not vote_board and trade_date:
            vote_board = self._fetch_vote_board(port, trade_date)
        default_case_detail = self._build_default_case_detail(vote_board.get("items") or [], port)
        return {
            "health": self._fetch_health(port),
            "ops_proxy": self._fetch_ops_proxy_health(),
            "readiness": readiness,
            "account_state": account_state,
            "settings": self._fetch_settings(port),
            "env_config": self._read_env_config(),
            "processes": self._fetch_processes(),
            "workspace_context": workspace_context,
            "discussion_context": discussion_context,
            "runtime_context": runtime_context,
            "monitor_context": monitor_context,
            "latest_case": latest_case,
            "meeting": meeting,
            "vote_board": vote_board,
            "log_preview": self._fetch_log_preview() if self.auto_refresh_logs_var.get() else self.log_preview.get("1.0", tk.END),
            "meeting_text": self._format_meeting_text(
                meeting,
                account_state=account_state,
                discussion_context=discussion_context,
                workspace_context=workspace_context,
            ),
            "pool_text": self._format_pool_text(
                trade_date=trade_date,
                account_state=account_state,
                workspace_context=workspace_context,
                discussion_context=discussion_context,
                monitor_context=monitor_context,
                vote_board=vote_board,
            ),
            "case_detail_text": default_case_detail,
            "updated_at": self._now_text(),
        }

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _fetch_json(self, uri: str) -> dict | None:
        try:
            with urllib.request.urlopen(uri, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError:
            return None
        except Exception:
            return None

    def _post_json(self, uri: str, payload: dict) -> dict | None:
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                uri,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError:
            return None
        except Exception:
            return None

    def _parse_ops_proxy_port(self) -> int | None:
        try:
            return int(self.ops_proxy_port_var.get().strip())
        except ValueError:
            return None

    def _read_ops_proxy_manifest(self) -> dict:
        if not self.ops_proxy_manifest.exists():
            return {}
        try:
            return json.loads(self.ops_proxy_manifest.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _read_ops_proxy_token(self) -> str:
        if self.ops_proxy_token_file.exists():
            try:
                return self.ops_proxy_token_file.read_text(encoding="utf-8").strip()
            except Exception:
                return ""
        manifest = self._read_ops_proxy_manifest()
        token_file = manifest.get("token_file")
        if not token_file:
            return ""
        try:
            return Path(str(token_file)).read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _ops_proxy_base_url(self) -> str:
        manifest = self._read_ops_proxy_manifest()
        candidates: list[str] = []
        preferred = manifest.get("preferred_wsl_url")
        if isinstance(preferred, str) and preferred.strip():
            candidates.append(preferred.strip())
        for item in manifest.get("candidate_urls") or []:
            if isinstance(item, str) and item.strip():
                candidates.append(item.strip())
        port = self._parse_ops_proxy_port() or 18791
        candidates.append(f"http://127.0.0.1:{port}")
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            return candidate.rstrip("/")
        return f"http://127.0.0.1:{port}"

    def _fetch_ops_proxy_health(self) -> dict:
        base = self._ops_proxy_base_url()
        return self._fetch_json(f"{base}/health") or {"status": "unavailable", "service": "windows-ops-proxy"}

    def _ops_proxy_request(self, path: str, payload: dict | None = None, method: str = "GET") -> dict | None:
        base = self._ops_proxy_base_url()
        token = self._read_ops_proxy_token()
        uri = f"{base}{path}"
        headers = {}
        if token:
            headers["X-Ashare-Token"] = token
        try:
            if method.upper() == "GET":
                request = urllib.request.Request(uri, headers=headers, method="GET")
            else:
                data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json; charset=utf-8"
                request = urllib.request.Request(uri, data=data, headers=headers, method=method.upper())
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError:
            return None
        except Exception:
            return None

    def _ops_proxy_available(self) -> bool:
        health = self._fetch_ops_proxy_health()
        return health.get("status") == "ok"

    def _fetch_health(self, port: int | None) -> dict:
        if port is None:
            return {"status": "invalid-port", "mode": "-", "service": "-"}
        return self._fetch_json(f"http://127.0.0.1:{port}/health") or {"status": "unavailable", "mode": "-", "service": "-"}

    def _fetch_workspace_context(self, port: int | None) -> dict:
        if port is None:
            return {}
        return (
            self._fetch_json(f"http://127.0.0.1:{port}/data/workspace-context/latest")
            or self._fetch_json(f"http://127.0.0.1:{port}/system/workspace-context")
            or {}
        )

    def _fetch_discussion_context(self, port: int | None) -> dict:
        if port is None:
            return {}
        return (
            self._fetch_json(f"http://127.0.0.1:{port}/data/discussion-context/latest")
            or self._fetch_json(f"http://127.0.0.1:{port}/system/discussions/meeting-context")
            or {}
        )

    def _fetch_monitor_context(self, port: int | None) -> dict:
        if port is None:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/data/monitor-context/latest") or {}

    def _fetch_runtime_context(self, port: int | None) -> dict:
        if port is None:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/data/runtime-context/latest") or {}

    def _fetch_readiness(self, port: int | None) -> dict:
        if port is None:
            return {"status": "invalid-port", "account_id": "-", "summary_lines": []}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/readiness") or {
            "status": "unavailable",
            "account_id": "-",
            "summary_lines": ["就绪接口不可达"],
        }

    def _fetch_account_state(self, port: int | None, account_id: str | None) -> dict:
        if port is None:
            return {}
        uri = f"http://127.0.0.1:{port}/system/account-state"
        if account_id and account_id != "-":
            uri += f"?account_id={urllib.parse.quote(str(account_id))}"
        return self._fetch_json(uri) or {}

    def _fetch_settings(self, port: int | None) -> dict:
        if port is None:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/settings") or {}

    def _fetch_latest_case(self, port: int | None) -> dict:
        if port is None:
            return {}
        payload = self._fetch_json(f"http://127.0.0.1:{port}/system/cases?limit=1") or {}
        items = payload.get("items") or []
        return items[0] if items else {}

    def _fetch_meeting(self, port: int | None) -> dict:
        if port is None:
            return {"available": False}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/meetings/latest") or {"available": False}

    def _fetch_discussion_summary(self, port: int | None, trade_date: str | None) -> dict:
        if port is None or not trade_date:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/discussions/summary?trade_date={trade_date}") or {}

    def _fetch_reply_pack(self, port: int | None, trade_date: str | None) -> dict:
        if port is None or not trade_date:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/discussions/reply-pack?trade_date={trade_date}") or {}

    def _fetch_vote_board(self, port: int | None, trade_date: str | None) -> dict:
        if port is None or not trade_date:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/discussions/vote-board?trade_date={trade_date}&limit=10") or {}

    def _fetch_case_vote_detail(self, port: int | None, case_id: str) -> dict:
        if port is None:
            return {}
        return self._fetch_json(f"http://127.0.0.1:{port}/system/cases/{urllib.parse.quote(case_id)}/vote-detail") or {}

    def _fetch_processes(self) -> dict:
        command = """
$api = @(Get-CimInstance Win32_Process -ErrorAction Stop |
    Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*ashare_system.run serve*' } |
    Select-Object -ExpandProperty ProcessId)
$sched = @(Get-CimInstance Win32_Process -ErrorAction Stop |
    Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*ashare_system.run scheduler*' } |
    Select-Object -ExpandProperty ProcessId)
$watch = @(Get-CimInstance Win32_Process -ErrorAction Stop |
    Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*start_unattended.ps1*' } |
    Select-Object -ExpandProperty ProcessId)
[pscustomobject]@{
    api_pids = $api
    scheduler_pids = $sched
    watchdog_pids = $watch
} | ConvertTo-Json -Compress
""".strip()
        result = self._powershell_command(command)
        output = (result.stdout or "").strip()
        if not output:
            return {"api_pids": [], "scheduler_pids": [], "watchdog_pids": []}
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            self._queue.put(("log", f"解析进程状态失败: {output}"))
            return {"api_pids": [], "scheduler_pids": [], "watchdog_pids": []}
        return {
            "api_pids": self._normalize_pid_list(payload.get("api_pids")),
            "scheduler_pids": self._normalize_pid_list(payload.get("scheduler_pids")),
            "watchdog_pids": self._normalize_pid_list(payload.get("watchdog_pids")),
        }

    def _read_env_config(self) -> dict[str, str]:
        if not self.env_file.exists():
            return {}
        data: dict[str, str] = {}
        for line in self.env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            data[key.strip()] = value.strip()
        return data

    def _write_env_updates(self, updates: dict[str, str]) -> None:
        lines = self.env_file.read_text(encoding="utf-8", errors="replace").splitlines() if self.env_file.exists() else []
        pending = dict(updates)
        output: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                output.append(line)
                continue
            key, _value = line.split("=", 1)
            key = key.strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
            else:
                output.append(line)
        for key, value in pending.items():
            output.append(f"{key}={value}")
        self.env_file.write_text("\n".join(output) + "\n", encoding="utf-8")

    def _fetch_log_preview(self, tail_lines: int = 120) -> str:
        path = self.log_files.get(self.log_file_var.get(), self.log_dir / "startup.log")
        if not path.exists():
            return f"===== {path.name} =====\n文件不存在。"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return f"===== {path.name} =====\n读取失败: {exc}"
        clipped = lines[-tail_lines:]
        body = "\n".join(clipped) if clipped else "(空文件)"
        return f"===== {path.name} 最近 {len(clipped)} 行 =====\n{body}"

    def _update_mode_banner(
        self,
        health_status: str,
        service_mode: str,
        config_mode: str,
        live_enable: str,
        environment: str,
    ) -> None:
        normalized_config_mode = config_mode.lower()
        normalized_service_mode = service_mode.lower()
        live_enabled = live_enable.lower() == "true"
        environment_text = environment or "-"

        if health_status != "ok":
            banner_text = f"服务不可用 | 配置模式={config_mode} | LiveEnable={live_enable} | Env={environment_text}"
            banner_bg = "#7a2e2e"
            hint_text = "健康检查未通过，当前看到的是 .env 配置，不代表服务已经按该配置启动。"
        elif normalized_config_mode == "live" and live_enabled and normalized_service_mode == "live":
            banner_text = f"LIVE 实盘模式 | Env={environment_text}"
            banner_bg = "#a63a3a"
            hint_text = "当前配置与服务状态一致，允许进入实盘提交链路。"
        elif normalized_config_mode == "paper" and not live_enabled and normalized_service_mode == "paper":
            banner_text = f"PAPER 模拟模式 | Env={environment_text}"
            banner_bg = "#2f5d3a"
            hint_text = "当前配置与服务状态一致，执行链路保持非实盘。"
        else:
            banner_text = (
                f"模式切换中 | 配置={config_mode}, live_enable={live_enable} | 服务上报={service_mode} | Env={environment_text}"
            )
            banner_bg = "#8a6a18"
            hint_text = "配置和服务上报尚未一致，通常表示服务正在重启或还没按新配置完全拉起。"

        self.mode_banner_var.set(banner_text)
        self.mode_hint_var.set(hint_text)
        self.mode_banner.configure(bg=banner_bg)

    @staticmethod
    def _resolve_trade_date(*payloads: dict | None) -> str | None:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            trade_date = payload.get("trade_date")
            if trade_date:
                return str(trade_date)
        return None

    def _resolve_latest_case(
        self,
        workspace_context: dict,
        discussion_context: dict,
        runtime_context: dict,
        port: int | None,
    ) -> dict:
        latest_case = self._fetch_latest_case(port)
        trade_date = latest_case.get("trade_date") or self._resolve_trade_date(
            workspace_context,
            discussion_context,
            runtime_context,
        )
        if trade_date and not latest_case.get("trade_date"):
            latest_case = {**latest_case, "trade_date": trade_date}
        return latest_case

    @staticmethod
    def _normalize_vote_item(item: dict | None) -> dict | None:
        if not isinstance(item, dict):
            return None
        symbol = str(item.get("symbol") or "").strip()
        case_id = str(item.get("case_id") or "").strip()
        if not symbol and not case_id:
            return None
        name = str(item.get("name") or "").strip()
        symbol_display = str(item.get("symbol_display") or "").strip()
        if not symbol_display:
            symbol_display = f"{symbol} {name or symbol}".strip()
        runtime_snapshot = item.get("runtime_snapshot") or {}
        headline_reason = (
            item.get("headline_reason")
            or item.get("selected_reason")
            or item.get("rejected_reason")
            or runtime_snapshot.get("summary")
            or item.get("reason")
            or "暂无结论理由"
        )
        headline_line = item.get("headline_line")
        if not headline_line:
            headline_line = (
                f"{symbol_display} | 状态={item.get('final_status', '-')} | 排名={item.get('rank', '-')} | "
                f"分数={item.get('selection_score', '-')} | 风控={item.get('risk_gate', '-')} "
                f"审计={item.get('audit_gate', '-')} | 理由={headline_reason}"
            )
        normalized = dict(item)
        normalized["symbol_display"] = symbol_display
        normalized["headline_reason"] = headline_reason
        normalized["headline_line"] = headline_line
        return normalized

    def _build_vote_board_from_context(self, discussion_context: dict, trade_date: str | None) -> dict:
        if not discussion_context:
            return {}
        reply_pack = discussion_context.get("reply_pack") or {}
        items: list[dict] = []
        for group in ("selected", "watchlist", "rejected"):
            for raw_item in reply_pack.get(group) or []:
                normalized = self._normalize_vote_item(raw_item)
                if normalized:
                    items.append(normalized)

        summary_lines = list(discussion_context.get("summary_lines") or [])
        if not summary_lines:
            summary_lines = list(reply_pack.get("overview_lines") or [])
        if items and not summary_lines:
            summary_lines = [
                item.get("headline_line", "")
                for item in items[: min(5, len(items))]
                if item.get("headline_line")
            ]

        resolved_trade_date = discussion_context.get("trade_date") or trade_date
        return {
            "trade_date": resolved_trade_date,
            "case_count": int(reply_pack.get("case_count", len(items)) or len(items)),
            "counts": {
                "selected": int(reply_pack.get("selected_count", len(reply_pack.get("selected") or [])) or 0),
                "watchlist": int(reply_pack.get("watchlist_count", len(reply_pack.get("watchlist") or [])) or 0),
                "rejected": int(reply_pack.get("rejected_count", len(reply_pack.get("rejected") or [])) or 0),
            },
            "items": items,
            "summary_lines": summary_lines,
            "summary_text": "\n".join(summary_lines),
        }

    def _format_meeting_text(
        self,
        meeting: dict,
        account_state: dict | None = None,
        discussion_context: dict | None = None,
        workspace_context: dict | None = None,
    ) -> str:
        if not meeting or meeting.get("available") is False:
            draft = self._build_meeting_payload(
                self._resolve_trade_date(discussion_context or {}, workspace_context or {}),
                readiness={},
                account_state=account_state or {},
                discussion_context=discussion_context or {},
                workspace_context=workspace_context or {},
                vote_board=self._build_vote_board_from_context(
                    discussion_context or {},
                    self._resolve_trade_date(discussion_context or {}, workspace_context or {}),
                ),
            )
            if draft:
                return (
                    "当前没有最新会议纪要。\n\n以下为基于统一上下文生成的会议草稿预览：\n\n"
                    + (draft.get("summary") or "(空)")
                )
            return "当前没有最新会议纪要。\n\n可在本页点击“生成会议纪要”，按当前选股池与投票摘要自动写入一份盘前/盘中会议记录。"
        lines = [
            f"标题: {meeting.get('title', '-')}",
            f"会议ID: {meeting.get('meeting_id', '-')}",
            f"记录时间: {meeting.get('recorded_at', '-')}",
            "",
            "摘要:",
            meeting.get("summary", "") or "(空)",
        ]
        participants = meeting.get("participants") or []
        decisions = meeting.get("decisions") or []
        lines.extend(["", "参会者:", "、".join(str(item) for item in participants) if participants else "(空)"])
        lines.append("")
        lines.append("决议:")
        if decisions:
            lines.extend(f"{index}. {item}" for index, item in enumerate(decisions, start=1))
        else:
            lines.append("(空)")
        return "\n".join(lines)

    def _build_manual_meeting_payload(self, trade_date: str | None) -> dict | None:
        title = self.meeting_title_var.get().strip() or (f"{trade_date} 手工会议纪要" if trade_date and trade_date != "-" else "手工会议纪要")
        summary = self.meeting_text.get("1.0", tk.END).strip()
        if not summary:
            return None
        decisions: list[str] = []
        for line in summary.splitlines():
            stripped = line.strip()
            if stripped.startswith("1.") or stripped.startswith("2.") or stripped.startswith("3.") or stripped.startswith("4."):
                decisions.append(stripped.split(".", 1)[1].strip())
        return {
            "title": title,
            "summary": summary,
            "participants": ["main", "ashare"],
            "decisions": decisions[:10],
        }

    def _format_pool_text(
        self,
        *,
        trade_date: str | None,
        account_state: dict,
        workspace_context: dict,
        discussion_context: dict,
        monitor_context: dict,
        vote_board: dict,
    ) -> str:
        if not trade_date and not workspace_context:
            return "当前没有可用交易日，也没有候选池数据。"
        resolved_trade_date = trade_date or workspace_context.get("trade_date")
        lines = [f"交易日: {resolved_trade_date or '-'}", ""]

        account_metrics = account_state.get("metrics") or {}
        stock_budget = account_metrics.get("stock_test_budget_amount")
        stock_remaining = account_metrics.get("available_test_trade_value")
        reverse_repo_value = account_metrics.get("reverse_repo_value")
        reverse_repo_reserved_amount = account_metrics.get("reverse_repo_reserved_amount")
        minimum_total_invested_amount = account_metrics.get("minimum_total_invested_amount")
        if any(isinstance(value, (int, float)) for value in (stock_budget, reverse_repo_value, minimum_total_invested_amount)):
            lines.append("测试资金:")
            if isinstance(minimum_total_invested_amount, (int, float)):
                lines.append(f"总持仓基线: {minimum_total_invested_amount:.2f}")
            if isinstance(reverse_repo_value, (int, float)) and isinstance(reverse_repo_reserved_amount, (int, float)):
                lines.append(f"逆回购: {reverse_repo_value:.2f} / {reverse_repo_reserved_amount:.2f}")
            if isinstance(stock_remaining, (int, float)) and isinstance(stock_budget, (int, float)):
                lines.append(f"股票测试预算: {stock_remaining:.2f} / {stock_budget:.2f}")
            lines.append("")

        workspace_lines = workspace_context.get("summary_lines") or []
        if workspace_lines:
            lines.append("工作区总览:")
            lines.extend(workspace_lines[:4])
            lines.append("")

        reply_pack = discussion_context.get("reply_pack") or {}
        summary_lines = (
            discussion_context.get("summary_lines")
            or (discussion_context.get("client_brief") or {}).get("lines")
            or (discussion_context.get("final_brief") or {}).get("lines")
            or reply_pack.get("overview_lines")
            or []
        )
        if summary_lines:
            lines.append("讨论摘要:")
            lines.extend(summary_lines)
            lines.append("")
        else:
            lines.append("讨论摘要: 无")
            lines.append("")

        selected = reply_pack.get("selected_lines") or []
        watchlist = reply_pack.get("watchlist_lines") or []
        rejected = reply_pack.get("rejected_lines") or []
        challenge_exchange = (
            discussion_context.get("challenge_exchange_lines")
            or (discussion_context.get("client_brief") or {}).get("challenge_exchange_lines")
            or reply_pack.get("challenge_exchange_lines")
            or []
        )
        persuasion_summary = (
            discussion_context.get("persuasion_summary_lines")
            or (discussion_context.get("client_brief") or {}).get("persuasion_summary_lines")
            or reply_pack.get("persuasion_summary_lines")
            or []
        )
        lines.append("入选:")
        lines.extend(selected if selected else ["(空)"])
        lines.append("")
        lines.append("观察:")
        lines.extend(watchlist if watchlist else ["(空)"])
        lines.append("")
        lines.append("淘汰:")
        lines.extend(rejected if rejected else ["(空)"])
        lines.append("")
        if challenge_exchange:
            lines.append("关键交锋:")
            lines.extend(challenge_exchange)
            lines.append("")
        if persuasion_summary:
            lines.append("讨论收敛:")
            lines.extend(persuasion_summary)
            lines.append("")

        latest_pool_snapshot = monitor_context.get("latest_pool_snapshot") or {}
        heartbeat_freshness = monitor_context.get("heartbeat_freshness") or {}
        polling_status = monitor_context.get("polling_status") or {}
        if latest_pool_snapshot or heartbeat_freshness or polling_status:
            counts = latest_pool_snapshot.get("counts") or {}
            lines.append("盯盘状态:")
            if counts:
                lines.append(
                    "candidate={candidate} focus={focus} execution={execution} watchlist={watchlist} rejected={rejected}".format(
                        candidate=counts.get("candidate_pool", 0),
                        focus=counts.get("focus_pool", 0),
                        execution=counts.get("execution_pool", 0),
                        watchlist=counts.get("watchlist", 0),
                        rejected=counts.get("rejected", 0),
                    )
                )
            if heartbeat_freshness:
                lines.append(
                    f"heartbeat fresh={heartbeat_freshness.get('is_fresh')} level={heartbeat_freshness.get('staleness_level')} expires_at={heartbeat_freshness.get('expires_at', '-')}"
                )
            candidate_poll = polling_status.get("candidate") or {}
            if candidate_poll:
                lines.append(
                    f"candidate 轮询 interval={candidate_poll.get('interval_seconds')}s due_now={candidate_poll.get('due_now')} next_due_at={candidate_poll.get('next_due_at') or '-'}"
                )
            lines.append("")

        vote_summary = vote_board.get("summary_lines") or []
        items = vote_board.get("items") or []
        lines.append("投票摘要:")
        lines.extend(vote_summary if vote_summary else ["(空)"])
        if items:
            lines.append("")
            lines.append("前 5 只投票头条:")
            lines.extend(item.get("headline_line", "") for item in items[:5] if item.get("headline_line"))
        return "\n".join(lines)

    def _update_case_selector(self, items: list[dict]) -> None:
        options: list[str] = []
        mapping: dict[str, str] = {}
        for item in items:
            label = item.get("symbol_display") or item.get("headline_line") or item.get("case_id") or ""
            case_id = item.get("case_id")
            if not label or not case_id:
                continue
            options.append(label)
            mapping[label] = case_id
        self._case_option_map = mapping
        self.case_selector["values"] = options
        if options:
            if self.case_selector_var.get() not in mapping:
                self.case_selector_var.set(options[0])
        else:
            self.case_selector_var.set("")

    def _build_default_case_detail(self, items: list[dict], port: int | None) -> str:
        if not items:
            return "当前没有投票明细。"
        selected_label = self.case_selector_var.get().strip()
        case_id = self._case_option_map.get(selected_label)
        if not case_id:
            first = items[0]
            case_id = first.get("case_id")
        if not case_id:
            return "当前没有投票明细。"
        payload = self._fetch_case_vote_detail(port, case_id)
        if not payload:
            fallback_case = next((item for item in items if item.get("case_id") == case_id), None)
            if fallback_case:
                return self._format_case_detail_text({"case": fallback_case})
            return "当前无法读取单票投票明细。"
        return self._format_case_detail_text(payload)

    def _format_case_detail_text(self, payload: dict) -> str:
        case = payload.get("case") or {}
        if not case:
            return "当前没有单票投票明细。"
        headline_reason = (
            case.get("headline_reason")
            or case.get("selected_reason")
            or case.get("rejected_reason")
            or ((case.get("runtime_snapshot") or {}).get("summary"))
            or case.get("reason")
            or "-"
        )
        lines = [
            f"股票: {case.get('symbol_display', case.get('symbol', '-'))}",
            f"状态: {case.get('final_status', '-')}",
            f"排名/分数: {case.get('rank', '-')} / {case.get('selection_score', '-')}",
            f"风控/审计: {case.get('risk_gate', '-')} / {case.get('audit_gate', '-')}",
            f"结论: {headline_reason}",
            "",
        ]
        discussion = case.get("discussion") or {}
        discussion_digest = case.get("discussion_digest") or {}
        if discussion:
            lines.append("讨论要点:")
            lines.append(f"支持点: {'；'.join(discussion.get('support_points') or []) or '(空)'}")
            lines.append(f"反对点: {'；'.join(discussion.get('oppose_points') or []) or '(空)'}")
            lines.append(f"证据缺口: {'；'.join(discussion.get('evidence_gaps') or []) or '(空)'}")
            lines.append(f"二轮问题: {'；'.join(discussion.get('questions_for_round_2') or []) or '(空)'}")
            lines.append(f"关键交锋: {'；'.join(discussion.get('challenge_exchange_lines') or []) or '(空)'}")
            lines.append(f"改判记录: {'；'.join(discussion.get('revision_notes') or []) or '(空)'}")
            lines.append(f"剩余争议: {'；'.join(discussion.get('remaining_disputes') or []) or '(空)'}")
            lines.append("")
        digest_lines = case.get("discussion_digest_lines") or []
        if discussion_digest or digest_lines:
            lines.append("讨论摘要:")
            lines.extend(digest_lines or ["(空)"])
            lines.append("")
        rounds = case.get("rounds") or {}
        for round_key in ("round_1", "round_2"):
            round_payload = rounds.get(round_key) or {}
            lines.append(f"{round_key.upper()}:")
            round_lines = round_payload.get("lines") or []
            lines.extend(round_lines if round_lines else ["(空)"])
            lines.append("")
        latest = case.get("latest_opinions") or {}
        if latest:
            lines.append("最新立场:")
            for agent_id, item in latest.items():
                reasons = "；".join(item.get("reasons") or []) or "-"
                lines.append(f"{agent_id} | round={item.get('round')} | stance={item.get('stance')} | confidence={item.get('confidence')} | 理由={reasons}")
        return "\n".join(lines)

    def _build_meeting_payload(
        self,
        trade_date: str | None,
        readiness: dict,
        account_state: dict | None = None,
        summary: dict | None = None,
        reply_pack: dict | None = None,
        vote_board: dict | None = None,
        discussion_context: dict | None = None,
        workspace_context: dict | None = None,
    ) -> dict | None:
        resolved_trade_date = trade_date or self._resolve_trade_date(discussion_context or {}, workspace_context or {})
        if not resolved_trade_date:
            return None

        resolved_reply_pack = dict(reply_pack or {})
        discussion_lines: list[str] = list((summary or {}).get("summary_lines") or [])
        if discussion_context:
            resolved_reply_pack = resolved_reply_pack or dict(discussion_context.get("reply_pack") or {})
            client_brief = discussion_context.get("client_brief") or {}
            final_brief = discussion_context.get("final_brief") or {}
            discussion_lines = (
                list(client_brief.get("lines") or [])
                or list(discussion_context.get("summary_lines") or [])
                or list(final_brief.get("lines") or [])
                or list(resolved_reply_pack.get("overview_lines") or [])
            )

        resolved_vote_board = dict(vote_board or {})
        if not resolved_vote_board and discussion_context:
            resolved_vote_board = self._build_vote_board_from_context(discussion_context, resolved_trade_date)

        selected_lines = list(resolved_reply_pack.get("selected_lines") or [])
        watchlist_lines = list(resolved_reply_pack.get("watchlist_lines") or [])
        rejected_lines = list(resolved_reply_pack.get("rejected_lines") or [])
        challenge_exchange_lines = list(
            (discussion_context or {}).get("challenge_exchange_lines")
            or ((discussion_context or {}).get("client_brief") or {}).get("challenge_exchange_lines")
            or resolved_reply_pack.get("challenge_exchange_lines")
            or []
        )
        persuasion_summary_lines = list(
            (discussion_context or {}).get("persuasion_summary_lines")
            or ((discussion_context or {}).get("client_brief") or {}).get("persuasion_summary_lines")
            or resolved_reply_pack.get("persuasion_summary_lines")
            or []
        )
        vote_lines = list(resolved_vote_board.get("summary_lines") or [])
        readiness_lines = list(readiness.get("summary_lines") or [])
        workspace_lines = list((workspace_context or {}).get("summary_lines") or [])
        account_metrics = (account_state or {}).get("metrics") or {}
        funding_lines: list[str] = []
        minimum_total_invested_amount = account_metrics.get("minimum_total_invested_amount")
        reverse_repo_reserved_amount = account_metrics.get("reverse_repo_reserved_amount")
        reverse_repo_value = account_metrics.get("reverse_repo_value")
        stock_test_budget_amount = account_metrics.get("stock_test_budget_amount")
        available_test_trade_value = account_metrics.get("available_test_trade_value")
        if isinstance(minimum_total_invested_amount, (int, float)):
            funding_lines.append(f"测试总持仓基线 {minimum_total_invested_amount:.2f}")
        if isinstance(reverse_repo_value, (int, float)) and isinstance(reverse_repo_reserved_amount, (int, float)):
            funding_lines.append(f"逆回购 {reverse_repo_value:.2f} / {reverse_repo_reserved_amount:.2f}")
        if isinstance(stock_test_budget_amount, (int, float)) and isinstance(available_test_trade_value, (int, float)):
            funding_lines.append(f"股票测试预算剩余 {available_test_trade_value:.2f} / {stock_test_budget_amount:.2f}")
        if not any([discussion_lines, selected_lines, watchlist_lines, rejected_lines, challenge_exchange_lines, persuasion_summary_lines, vote_lines, workspace_lines]):
            return None

        summary_text_lines = [
            f"会议主题：{resolved_trade_date} 选股讨论收敛会",
            "",
            "系统状态：",
            *(readiness_lines[:2] or ["(空)"]),
            "",
        ]
        if funding_lines:
            summary_text_lines.extend(["测试资金：", *funding_lines, ""])
        if workspace_lines:
            summary_text_lines.extend(["工作区总览：", *(workspace_lines[:4] or ["(空)"]), ""])
        summary_text_lines.extend(
            [
                "讨论摘要：",
                *(discussion_lines or ["(空)"]),
                "",
                "入选：",
                *(selected_lines or ["(空)"]),
                "",
                "观察：",
                *(watchlist_lines or ["(空)"]),
                "",
                "淘汰：",
                *(rejected_lines or ["(空)"]),
                "",
                "关键交锋：",
                *(challenge_exchange_lines or ["(空)"]),
                "",
                "讨论收敛：",
                *(persuasion_summary_lines or ["(空)"]),
                "",
                "投票摘要：",
                *(vote_lines[:6] or ["(空)"]),
            ]
        )
        decisions = []
        if selected_lines:
            decisions.append("保持入选池，进入后续执行前检查。")
        elif watchlist_lines:
            decisions.append("当前无入选标的，继续观察池跟踪。")
        else:
            decisions.append("当前无可执行标的，等待下一轮候选刷新。")
        if readiness.get("status"):
            decisions.append(f"系统就绪状态：{readiness.get('status')}")

        return {
            "title": f"{resolved_trade_date} 选股讨论会",
            "summary": "\n".join(summary_text_lines),
            "participants": ["main", "ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"],
            "decisions": decisions,
        }

    @staticmethod
    def _normalize_pid_list(value) -> list[int]:
        if value is None:
            return []
        if isinstance(value, list):
            return [int(item) for item in value]
        return [int(value)]

    def open_selected_log(self) -> None:
        self.open_path(self.log_files.get(self.log_file_var.get(), self.log_dir / "startup.log"))

    @staticmethod
    def open_path(path: Path) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except FileNotFoundError:
            messagebox.showerror("文件不存在", str(path))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))


def main() -> None:
    if not sys.platform.startswith("win"):
        raise SystemExit("windows_service_gui.py 仅支持 Windows 运行。")
    app = WindowsServiceGui()
    app.mainloop()


if __name__ == "__main__":
    main()
