"""仪表盘 API - 为系统提供可视化控制面板。"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse

from ..settings import AppSettings
from ..runtime_config import RuntimeConfigManager
from ..infra.audit_store import AuditStore


def build_router(
    settings: AppSettings,
    config_mgr: RuntimeConfigManager,
    audit_store: AuditStore | None = None,
    system_api_handler: Any | None = None, # 传一个能调用系统接口逻辑的对象
) -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    SERVICES = {
        "control-plane": "ashare-system-v2.service",
        "scheduler": "ashare-system-v2-scheduler.service",
        "feishu": "ashare-feishu-longconn.service",
        "openclaw": "openclaw-gateway.service",
    }

    def _get_service_status(service_name: str) -> str:
        try:
            # 检查用户级服务
            if "feishu" in service_name:
                cmd = ["systemctl", "--user", "is-active", service_name]
            else:
                cmd = ["systemctl", "is-active", service_name]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.stdout.strip()
        except Exception:
            return "unknown"

    @router.get("", response_class=HTMLResponse)
    async def get_dashboard(request: Request):
        # 获取基础信息
        run_mode = settings.run_mode
        live_enable = settings.live_trade_enabled
        
        # 获取服务状态
        service_statuses = {name: _get_service_status(unit) for name, unit in SERVICES.items()}
        
        # 获取最近审计
        recent_audits = []
        if audit_store:
            recent_audits = [r.model_dump() for r in audit_store.recent(limit=10)]

        # 简单 HTML 模板 (包含 Tailwind)
        html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AShare System V2 控制台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .status-active {{ color: #10b981; }}
        .status-inactive {{ color: #ef4444; }}
        .status-unknown {{ color: #6b7280; }}
    </style>
</head>
<body class="bg-gray-100 min-h-screen">
    <nav class="bg-slate-800 text-white p-4 shadow-lg">
        <div class="container mx-auto flex justify-between items-center">
            <h1 class="text-xl font-bold italic tracking-wider">ASHARE SYSTEM V2</h1>
            <div class="flex items-center space-x-4">
                <span class="px-3 py-1 rounded text-sm font-semibold { 'bg-red-600' if run_mode == 'live' else 'bg-blue-600' }">
                    {run_mode.upper()} MODE
                </span>
                <span class="text-sm opacity-75">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</span>
            </div>
        </div>
    </nav>

    <main class="container mx-auto py-8 px-4 grid grid-cols-1 md:grid-cols-3 gap-8">
        <!-- 核心状态卡片 -->
        <div class="bg-white rounded-xl shadow-md p-6 border-t-4 border-slate-800">
            <h2 class="text-lg font-bold mb-4 flex items-center">
                <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                核心运行状态
            </h2>
            <div class="space-y-4">
                <div class="flex justify-between items-center border-b pb-2">
                    <span class="text-gray-600">运行模式</span>
                    <span class="font-mono font-bold text-slate-700">{run_mode}</span>
                </div>
                <div class="flex justify-between items-center border-b pb-2">
                    <span class="text-gray-600">实盘开关</span>
                    <span class="font-bold { 'text-red-600' if live_enable else 'text-gray-400' }">
                        { '已开启 (LIVE)' if live_enable else '已关闭' }
                    </span>
                </div>
                <div class="pt-4 grid grid-cols-2 gap-2">
                    <div class="w-full bg-slate-50 text-slate-500 py-2 rounded text-sm font-medium text-center border">
                        运行模式已锁定为实盘
                    </div>
                    <form action="/dashboard/actions/toggle-live" method="post">
                        <button type="submit" class="w-full { 'bg-red-50 text-red-700 hover:bg-red-100' if live_enable else 'bg-green-50 text-green-700 hover:bg-green-100' } py-2 rounded text-sm font-medium transition border">
                            { '停用下单' if live_enable else '启用实盘' }
                        </button>
                    </form>
                </div>
            </div>
        </div>

        <!-- 服务管理卡片 -->
        <div class="bg-white rounded-xl shadow-md p-6 border-t-4 border-slate-800">
            <h2 class="text-lg font-bold mb-4 flex items-center">
                <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
                基础组件管理
            </h2>
            <div class="space-y-3">
                """ + "".join([
                    f"""
                    <div class="flex items-center justify-between group">
                        <div class="flex items-center space-x-3">
                            <div class="w-2 h-2 rounded-full { 'bg-green-500 shadow-sm shadow-green-200' if status == 'active' else 'bg-red-500' if status == 'inactive' else 'bg-gray-300' }"></div>
                            <span class="text-gray-700 font-medium">{name}</span>
                        </div>
                        <div class="flex space-x-1 opacity-0 group-hover:opacity-100 transition">
                            <form action="/dashboard/actions/service" method="post">
                                <input type="hidden" name="service" value="{name}">
                                <input type="hidden" name="action" value="restart">
                                <button type="submit" class="p-1 text-blue-600 hover:bg-blue-50 rounded">
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                                </button>
                            </form>
                        </div>
                        <span class="text-xs font-mono { 'text-green-600' if status == 'active' else 'text-red-500' }">{status}</span>
                    </div>
                    """ for name, status in service_statuses.items()
                ]) + """
            </div>
            <div class="mt-6 p-3 bg-amber-50 border border-amber-100 rounded text-xs text-amber-800">
                提示：重启 Control Plane 可能会导致当前页面短暂失去连接。
            </div>
        </div>

        <!-- 系统概览卡片 -->
        <div class="bg-white rounded-xl shadow-md p-6 border-t-4 border-slate-800">
            <h2 class="text-lg font-bold mb-4 flex items-center">
                <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 01-2 2h2a2 2 0 012-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path></svg>
                Agent 协作看板
            </h2>
            <div class="space-y-4">
                <div class="flex justify-between items-center text-sm">
                    <span class="text-gray-500">活跃学习产物</span>
                    <span class="font-bold text-slate-700">探测中...</span>
                </div>
                <div class="flex justify-between items-center text-sm">
                    <span class="text-gray-500">当前讨论周期</span>
                    <span class="px-2 py-0.5 bg-green-100 text-green-800 rounded text-xs font-medium">活动中</span>
                </div>
                <div class="mt-4">
                    <button onclick="location.reload()" class="w-full border border-slate-300 hover:bg-gray-50 text-gray-700 py-2 rounded text-sm transition">
                        刷新全站数据
                    </button>
                </div>
            </div>
        </div>

        <!-- 审计日志 -->
        <div class="md:col-span-3 bg-white rounded-xl shadow-md p-6 overflow-hidden">
            <h2 class="text-lg font-bold mb-4">最近系统审计日志</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead>
                        <tr class="bg-gray-50 text-gray-600 font-medium">
                            <th class="py-2 px-4">时间</th>
                            <th class="py-2 px-4">类别</th>
                            <th class="py-2 px-4">内容</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y">
                        """ + "".join([
                            f"""
                            <tr class="hover:bg-gray-50">
                                <td class="py-3 px-4 text-gray-400 font-mono whitespace-nowrap">{audit.get('timestamp', '')[:19].replace('T', ' ')}</td>
                                <td class="py-3 px-4"><span class="px-2 py-0.5 bg-slate-100 text-slate-600 rounded text-xs">{audit.get('category', 'system')}</span></td>
                                <td class="py-3 px-4 text-gray-700">{audit.get('message', '')}</td>
                            </tr>
                            """ for audit in recent_audits
                        ]) + """
                    </tbody>
                </table>
            </div>
        </div>
    </main>

    <footer class="container mx-auto mt-12 pb-12 text-center text-gray-400 text-sm">
        &copy; 2026 AShare System V2. Built for Autonomous Quant Trading.
    </footer>
</body>
</html>
        """
        return HTMLResponse(content=html_content)

    @router.post("/actions/mode")
    async def change_mode(mode: str = Form(...)):
        if mode != "live":
            raise HTTPException(status_code=409, detail="run_mode 已锁定为 live，禁止切换到非实盘模式")
        settings.run_mode = "live"  # type: ignore
        if audit_store:
            audit_store.append(category="dashboard", message="仪表盘确认运行模式保持 live")
        return HTMLResponse("<script>alert('运行模式固定为 live'); window.location='/dashboard';</script>")

    @router.post("/actions/toggle-live")
    async def toggle_live():
        new_val = not settings.live_trade_enabled
        settings.live_trade_enabled = new_val
        if audit_store:
            audit_store.append(category="dashboard", message=f"仪表盘触发实盘交易开关: {new_val}")
        
        return HTMLResponse(f"<script>alert('实盘交易已{'开启' if new_val else '停用'}'); window.location='/dashboard';</script>")

    @router.post("/actions/service")
    async def manage_service(service: str = Form(...), action: str = Form(...)):
        unit = SERVICES.get(service)
        if not unit:
            raise HTTPException(status_code=400, detail="Unknown service")
        
        if action not in ("start", "stop", "restart"):
            raise HTTPException(status_code=400, detail="Invalid action")

        try:
            is_user = "feishu" in service
            cmd = ["systemctl"]
            if is_user:
                cmd.append("--user")
            cmd.extend([action, unit])
            
            # 由于可能需要 sudo，这里在开发环境下可能失败，但如果是 systemd 运行且有权限则 ok
            # 注意：Control Plane 重启自己会导致 502/连接重置
            subprocess.Popen(cmd) 
            
            if audit_store:
                audit_store.append(category="dashboard", message=f"仪表盘触发服务动作: {service} {action}")
                
            return HTMLResponse(f"<script>alert('已提交 {action} 指令给 {service}'); window.location='/dashboard';</script>")
        except Exception as e:
            return HTMLResponse(f"<script>alert('操作失败: {str(e)}'); window.location='/dashboard';</script>")

    return router
