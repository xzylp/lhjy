import React from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import {
  ArrowLeft,
  Bot,
  Boxes,
  BrainCircuit,
  CalendarClock,
  MessageSquareMore,
  Network,
  NotebookPen,
  Package,
  ScanSearch,
  Settings2,
  ShieldEllipsis,
  UserRoundCog,
} from 'lucide-react';

const NAV_ITEMS = [
  { to: '/dashboard/hermes/chat', label: '对话', icon: MessageSquareMore },
  { to: '/dashboard/hermes/sessions', label: '会话', icon: NotebookPen },
  { to: '/dashboard/hermes/profiles', label: '角色', icon: UserRoundCog },
  { to: '/dashboard/hermes/persona', label: '人格', icon: BrainCircuit },
  { to: '/dashboard/hermes/office', label: '办公台', icon: ScanSearch },
  { to: '/dashboard/hermes/models', label: '模型', icon: Bot },
  { to: '/dashboard/hermes/skills', label: '技能', icon: Boxes },
  { to: '/dashboard/hermes/memory', label: '记忆', icon: Package },
  { to: '/dashboard/hermes/tools', label: '工具', icon: ShieldEllipsis },
  { to: '/dashboard/hermes/schedules', label: '调度', icon: CalendarClock },
  { to: '/dashboard/hermes/gateway', label: '网关', icon: Network },
  { to: '/dashboard/hermes/settings', label: '设置', icon: Settings2 },
];

const TITLE_MAP: Record<string, string> = {
  '/dashboard/hermes/chat': '新对话',
  '/dashboard/hermes/sessions': '会话',
  '/dashboard/hermes/profiles': '角色',
  '/dashboard/hermes/persona': '人格',
  '/dashboard/hermes/office': '办公台',
  '/dashboard/hermes/models': '模型',
  '/dashboard/hermes/skills': '技能',
  '/dashboard/hermes/memory': '记忆',
  '/dashboard/hermes/tools': '工具',
  '/dashboard/hermes/schedules': '调度',
  '/dashboard/hermes/gateway': '网关',
  '/dashboard/hermes/settings': '设置',
};

export const HermesLayout = ({ children }: { children: React.ReactNode }) => {
  const location = useLocation();
  const title = TITLE_MAP[location.pathname] || 'Hermes';

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(251,191,36,0.16),_transparent_24%),radial-gradient(circle_at_bottom_right,_rgba(249,115,22,0.10),_transparent_22%),linear-gradient(180deg,_#f7f1e8_0%,_#f3eee7_100%)] text-[#1f1b16]">
      <div className="mx-auto flex min-h-screen max-w-[1660px] gap-5 px-3 py-3 md:px-4">
        <aside className="hidden w-[238px] shrink-0 rounded-[30px] border border-[#ebdfcf] bg-[#faf7f2]/95 p-4 shadow-[0_24px_80px_rgba(147,113,59,0.08)] lg:flex lg:flex-col">
          <div className="rounded-[24px] bg-[linear-gradient(135deg,_#fff8ea_0%,_#f9edd8_100%)] px-4 py-6">
            <div className="text-[28px] font-black tracking-tight text-transparent bg-clip-text bg-[linear-gradient(180deg,_#ffc62e_0%,_#ee9f00_100%)] drop-shadow-[0_1px_0_rgba(92,55,0,0.18)]">
              HERMES-AGENT
            </div>
            <div className="mt-3 text-xs leading-6 text-[#7a6c58]">
              通用控制平台，统一会话、能力接口、调度、记忆与业务域挂载。
            </div>
          </div>

          <nav className="mt-5 space-y-1.5">
            {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  [
                    'flex items-center gap-3 rounded-2xl px-3 py-3 text-sm transition',
                    isActive
                      ? 'bg-[#edf3ff] text-[#1c315c] shadow-sm'
                      : 'text-[#5d5348] hover:bg-white hover:text-[#1c1a16]',
                  ].join(' ')
                }
              >
                <Icon className="h-4 w-4" />
                <span className="font-medium">{label}</span>
              </NavLink>
            ))}
          </nav>

          <div className="mt-auto rounded-[24px] border border-[#e9dfd1] bg-white/85 px-4 py-4">
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#ab9a86]">操作说明</div>
            <div className="mt-3 text-sm leading-7 text-[#6f6458]">
              平台层负责统一入口，业务层负责真实执行。Hermes 不是闲聊框，而是控制与编排台。
            </div>
          </div>
        </aside>

        <div className="min-w-0 flex-1 rounded-[32px] border border-[#eadfce] bg-[#fffdf9]/90 shadow-[0_28px_120px_rgba(131,100,58,0.10)]">
          <header className="sticky top-0 z-20 rounded-t-[32px] border-b border-[#efe4d4] bg-[#fffdf9]/88 px-5 py-4 backdrop-blur md:px-7">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div className="flex items-center gap-3 text-sm text-[#7d7266]">
                <button className="inline-flex h-10 w-10 items-center justify-center rounded-2xl border border-[#ece1d2] bg-white text-[#6b6154]">
                  <span className="text-lg leading-none">☰</span>
                </button>
                <div className="text-sm">
                  <span className="text-[#8f8374]">Hermes</span>
                  <span className="mx-2 text-[#c3b7a8]">•</span>
                  <span className="font-semibold text-[#201b15]">{title}</span>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <NavLink
                  to="/dashboard"
                  className="inline-flex items-center gap-2 rounded-full border border-[#ece1d2] bg-white px-4 py-2 text-sm font-medium text-[#6b6154] transition hover:bg-[#fff7ea] hover:text-[#1f1b16]"
                >
                  <ArrowLeft className="h-4 w-4" />
                  <span>返回主控</span>
                </NavLink>
                <div className="hidden min-w-[220px] items-center rounded-full border border-[#eee3d4] bg-white px-4 py-2 md:flex">
                  <input
                    className="w-full border-0 bg-transparent text-sm text-[#584f45] outline-none placeholder:text-[#b1a595]"
                    placeholder="搜索"
                    readOnly
                  />
                </div>
                <button className="inline-flex h-10 w-10 items-center justify-center rounded-2xl border border-[#ece1d2] bg-white text-[#7d7266]">
                  ⟳
                </button>
              </div>
            </div>
          </header>

          <main className="px-4 py-5 md:px-6 md:py-6">{children}</main>
        </div>
      </div>
    </div>
  );
};
