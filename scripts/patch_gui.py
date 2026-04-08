import re
import sys
from pathlib import Path

def main():
    file_path = Path(r"d:\Coding\lhjy\ashare-system-v2\scripts\windows_service_gui.py")
    content = file_path.read_text(encoding="utf-8")
    
    start_pattern = r"    def _build_ui\(self\) -> None:"
    end_pattern = r"    def _append_output\(self, text: str\) -> None:"
    
    start_idx = content.find("    def _build_ui(self) -> None:")
    end_idx = content.find("    def _append_output(self, text: str) -> None:")
    
    if start_idx == -1 or end_idx == -1:
        print("Cannot find boundaries.")
        return

    new_ui = """    def _build_ui(self) -> None:
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

        ttk.Checkbutton(config_frame, text="启动时不拉起 scheduler", variable=self.no_scheduler_var).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        ttk.Label(config_frame, text="配置模式:").grid(row=3, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, textvariable=self.config_mode_var).grid(row=3, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, text="实盘提交:").grid(row=4, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, textvariable=self.config_live_enable_var).grid(row=4, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, text="部署环境:").grid(row=5, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(config_frame, textvariable=self.config_env_var).grid(row=5, column=1, sticky="w", padx=6, pady=2)

        config_btn_frame = ttk.Frame(config_frame)
        config_btn_frame.grid(row=6, column=0, columnspan=4, sticky="ew", padx=6, pady=8)
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
            ("Readiness", self.readiness_var), ("Account", self.account_id_var),
            ("Exec/Market", self.exec_market_var), ("Cash/Asset", self.cash_asset_var),
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

"""
    
    new_content = content[:start_idx] + new_ui + content[end_idx:]
    file_path.write_text(new_content, encoding="utf-8")
    print("Patched successfully.")

if __name__ == "__main__":
    main()
