import React from 'react';
import { NavLink } from 'react-router-dom';
import { Activity, Bot, ChartCandlestick, ShieldAlert, SlidersHorizontal } from 'lucide-react';
import { StatusBadge } from './Common';

const NAV_ITEMS = [
  { to: '/dashboard/overview', icon: ChartCandlestick, label: '交易总控台', desc: '状态、仓位、链路、催办' },
  { to: '/dashboard/agents', icon: Bot, label: 'Agent 履职', desc: '活跃度、提案、催办理由' },
  { to: '/dashboard/discussion', icon: Activity, label: '机会池讨论', desc: '候选、证据、去向' },
  { to: '/dashboard/risk', icon: ShieldAlert, label: '风控围栏', desc: '阻断、预算、执行口径' },
  { to: '/dashboard/governance', icon: SlidersHorizontal, label: '调参与治理', desc: '自然语言调参与提案' },
];

export const Layout = ({ children }: { children: React.ReactNode }) => {
  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(245,158,11,0.12),_transparent_32%),linear-gradient(180deg,_#f7f4ec_0%,_#f5efe2_46%,_#efe7d6_100%)] text-slate-900">
      <div className="mx-auto flex min-h-screen max-w-[1600px]">
        <aside className="hidden w-[320px] flex-col border-r border-stone-300/70 bg-[#1d1f1e] px-6 py-8 text-stone-200 lg:flex">
          <div className="rounded-3xl border border-white/10 bg-white/5 p-5 shadow-2xl shadow-black/20">
            <div className="flex items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-amber-300 text-[#1d1f1e]">
                <ChartCandlestick className="h-6 w-6" />
              </div>
              <div>
                <div className="text-lg font-black tracking-tight text-white">Ashare Mission Control</div>
                <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">Agent is brain, program is service</div>
              </div>
            </div>
            <div className="mt-5 space-y-2 text-sm leading-relaxed text-stone-300">
              <p>程序负责数据、执行、监督与电子围栏。</p>
              <p>控制台负责把事实、分工、阻断与机会票摆到台面上，而不是只看服务存活。</p>
            </div>
          </div>

          <nav className="mt-8 space-y-2">
            {NAV_ITEMS.map(({ to, icon, label, desc }) => (
              <SidebarItem key={to} to={to} icon={icon} label={label} desc={desc} />
            ))}
          </nav>

          <div className="mt-auto rounded-3xl border border-white/10 bg-white/5 p-5">
            <div className="text-[11px] uppercase tracking-[0.22em] text-stone-400">今日操作视角</div>
            <div className="mt-3 space-y-2 text-sm text-stone-200">
              <div>先事实，后判断。</div>
              <div>先预演，后正式执行。</div>
              <div>监督 agent，不机械催工具。</div>
            </div>
          </div>
        </aside>

        <div className="flex min-h-screen flex-1 flex-col">
          <header className="sticky top-0 z-20 border-b border-stone-300/70 bg-[#f7f2e7]/90 px-5 py-4 backdrop-blur md:px-8">
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="text-[11px] uppercase tracking-[0.26em] text-stone-500">量化交易主控平台</div>
                <div className="mt-1 text-xl font-black tracking-tight text-slate-900">盘面事实、Agent 履职、执行边界</div>
              </div>
              <div className="flex items-center gap-4 rounded-2xl border border-stone-300 bg-white/80 px-4 py-3 shadow-sm">
                <StatusBadge status="live" />
                <div className="h-5 w-px bg-stone-200" />
                <div className="text-right">
                  <div className="text-[10px] uppercase tracking-[0.22em] text-stone-400">控制面</div>
                  <div className="text-xs font-semibold text-stone-700">Go Platform + Linux Control Plane</div>
                </div>
              </div>
            </div>
          </header>

          <main className="flex-1 px-4 py-5 md:px-8 md:py-8">
            {children}
          </main>
        </div>
      </div>
    </div>
  );
};

const SidebarItem = ({ to, icon: Icon, label, desc }: { to: string; icon: any; label: string; desc: string }) => (
  <NavLink
    to={to}
    className={({ isActive }) =>
      [
        'group block rounded-3xl border px-4 py-4 transition-all duration-200',
        isActive
          ? 'border-amber-300/70 bg-gradient-to-br from-amber-200 to-orange-100 text-slate-900 shadow-lg shadow-amber-950/10'
          : 'border-white/10 bg-white/5 text-stone-200 hover:border-white/20 hover:bg-white/10',
      ].join(' ')
    }
  >
    <div className="flex items-start gap-3">
      <div className="mt-0.5 flex h-10 w-10 items-center justify-center rounded-2xl bg-black/10 group-hover:bg-black/15">
        <Icon className="h-5 w-5" />
      </div>
      <div>
        <div className="font-bold tracking-tight">{label}</div>
        <div className="mt-1 text-xs leading-relaxed text-current/70">{desc}</div>
      </div>
    </div>
  </NavLink>
);
