import { clsx } from 'clsx';
import React from 'react';

export const HermesPanel = ({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) => (
  <section className={clsx('rounded-[28px] border border-[#e7ddcf] bg-white/92 shadow-[0_18px_60px_rgba(164,137,98,0.10)]', className)}>
    {children}
  </section>
);

export const HermesPill = ({
  children,
  tone = 'neutral',
}: {
  children: React.ReactNode;
  tone?: 'neutral' | 'ok' | 'warning' | 'danger';
}) => {
  const tones = {
    neutral: 'bg-[#f6f1e8] text-[#6a6259] border-[#e7ddcf]',
    ok: 'bg-[#eef9f0] text-[#157a34] border-[#caebd1]',
    warning: 'bg-[#fff5e7] text-[#a15b00] border-[#f0d8ae]',
    danger: 'bg-[#fff0ef] text-[#ab3b33] border-[#f1c8c4]',
  };
  return (
    <span className={clsx('inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-semibold tracking-[0.16em] uppercase', tones[tone])}>
      {children}
    </span>
  );
};

export const HermesMetric = ({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
}) => (
  <div className="rounded-[24px] border border-[#ede4d8] bg-[#fcfaf7] px-4 py-4">
    <div className="text-[11px] uppercase tracking-[0.18em] text-[#9a8f80]">{label}</div>
    <div className="mt-2 text-2xl font-black tracking-tight text-[#221d16]">{value}</div>
    {hint ? <div className="mt-2 text-sm leading-6 text-[#7b7268]">{hint}</div> : null}
  </div>
);

export const HermesSectionTitle = ({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description?: string;
}) => (
  <div>
    <div className="text-[11px] uppercase tracking-[0.2em] text-[#a59682]">{eyebrow}</div>
    <h2 className="mt-2 text-[28px] font-black tracking-tight text-[#201a13]">{title}</h2>
    {description ? <p className="mt-3 max-w-3xl text-sm leading-7 text-[#6f655a]">{description}</p> : null}
  </div>
);

export const HermesEmpty = ({ text }: { text: string }) => (
  <div className="rounded-[24px] border border-dashed border-[#e3d7c7] bg-[#faf6f0] px-5 py-10 text-center text-sm text-[#8d8276]">
    {text}
  </div>
);

const HERMES_LABEL_MAP: Record<string, string> = {
  active: '运行中',
  ready: '就绪',
  available: '可用',
  missing: '缺失',
  enabled: '已启用',
  disabled: '已停用',
  scheduled: '已调度',
  contract_ready: '合同就绪',
  prompt_contract: '提示词合同',
  schedule_prompt: '调度合同',
  core: '核心',
  registry: '注册表',
  state: '状态',
  automation: '自动化',
  control: '控制',
  integration: '集成',
  scheduler_registry: '调度注册表',
  markdown_prompt: '提示词文档',
  ui_default_alias: '界面默认别名',
  platform_alias: '平台别名',
  fast: '快速',
  balanced: '均衡',
  slow: '深度',
  low: '低',
  medium: '中',
  high: '高',
  live: '实盘',
  'dry-run': '演练',
  dry_run: '演练',
  sim: '模拟',
  mock: '模拟',
  on: '开启',
  off: '关闭',
  warning: '告警',
  unknown: '未知',
  monitoring: '监控',
  research: '研究',
  strategy: '策略',
  risk: '风控',
  governance: '治理',
  execution: '执行',
  cron: '定时',
  general: '通用',
};

const HERMES_ROLE_MAP: Record<string, string> = {
  assistant: 'Hermes',
  user: '你',
  system: '系统',
};

const HERMES_SERVICE_UNIT_MAP: Record<string, string> = {
  openclaw: 'Hermes 网关',
  feishu_longconn: '飞书长连',
  control_plane: '控制平面',
  scheduler: '调度服务',
};

export const formatHermesLabel = (value?: string) => {
  if (!value) return '--';
  return HERMES_LABEL_MAP[value] || value;
};

export const formatHermesRole = (value?: string) => {
  if (!value) return '--';
  return HERMES_ROLE_MAP[value] || value;
};

export const formatHermesServiceUnitName = (value?: string) => {
  if (!value) return '--';
  return HERMES_SERVICE_UNIT_MAP[value] || value;
};

export const formatHermesTime = (value?: string) => {
  if (!value) return '--';
  return value.replace('T', ' ').slice(0, 19);
};
