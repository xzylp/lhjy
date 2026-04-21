import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Lock, ShieldCheck, ShieldX } from 'lucide-react';
import { fetchMissionControl, fetchParameterConsistency, fetchPortfolioEfficiency } from '../api';
import { MetricCard, TonePill } from '../components/Common';

const RiskPage = () => {
  const { data: mission, isPending } = useQuery({
    queryKey: ['missionControl'],
    queryFn: fetchMissionControl,
    refetchInterval: 10000,
  });
  const { data: efficiency } = useQuery({
    queryKey: ['portfolioEfficiency'],
    queryFn: fetchPortfolioEfficiency,
    refetchInterval: 20000,
  });
  const { data: consistency } = useQuery({
    queryKey: ['parameterConsistency'],
    queryFn: fetchParameterConsistency,
    refetchInterval: 30000,
  });

  if (isPending && !mission) {
    return <div className="p-8 text-stone-400 animate-pulse">正在读取真实风控状态...</div>;
  }

  const precheck = mission?.execution?.precheck || {};
  const blockers = mission?.blockers || [];
  const blockedItems = (precheck?.items || []).filter((item: any) => !item.approved);
  const checks = consistency?.checks || [];

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-3xl font-black tracking-tight text-slate-900">风控与执行围栏</h1>
        <p className="mt-2 text-sm leading-7 text-stone-500">
          页面展示的都是当前真实仓位、当前有效围栏、今日阻断和参数一致性检查，不再使用写死演示值。
        </p>
      </section>

      <section className="grid grid-cols-1 gap-6 md:grid-cols-3">
        <MetricCard
          title="总仓位上限"
          value={`${((precheck?.equity_position_limit || 0) * 100).toFixed(1)}%`}
          subValue={`当前股票仓位 ${(Number(mission?.account?.metrics?.current_total_ratio || 0) * 100).toFixed(1)}%`}
          icon={ShieldCheck}
        />
        <MetricCard
          title="单票上限"
          value={`¥${Number(precheck?.max_single_amount || 0).toFixed(0)}`}
          subValue={`剩余单票预算参考 ${(blockedItems[0]?.remaining_single_value ?? precheck?.max_single_amount) || 0}`}
          icon={Lock}
        />
        <MetricCard
          title="今日阻断"
          value={blockedItems.length}
          subValue={blockedItems[0]?.primary_blocker_label || blockers[0]?.title || '暂无显著阻断'}
          icon={AlertTriangle}
        />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1fr_1fr]">
        <Panel title="执行预检阻断明细" eyebrow="Precheck">
          <div className="space-y-3">
            {blockedItems.length ? blockedItems.map((item: any) => (
              <div key={item.symbol} className="rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm font-bold text-slate-900">{item.symbol} {item.name || ''}</div>
                  <TonePill status="warning">{item.primary_blocker_label || 'blocked'}</TonePill>
                </div>
                <div className="mt-2 text-sm text-stone-600">预算 {item.budget_value || '--'} / 建议股数 {item.proposed_quantity || '--'}</div>
                {item.primary_recommended_next_action_label ? (
                  <div className="mt-2 text-xs text-stone-500">下一步：{item.primary_recommended_next_action_label}</div>
                ) : null}
              </div>
            )) : (
              <Empty text="当前没有候选被预检阻断。" />
            )}
          </div>
        </Panel>

        <Panel title="组合效率与参数一致性" eyebrow="Portfolio / Config">
          <div className="grid gap-3 md:grid-cols-2">
            <MetricMini label="现金占比" value={formatPct(efficiency?.cash_ratio)} />
            <MetricMini label="风险预算已用" value={formatPct(efficiency?.risk_budget_used)} />
            <MetricMini label="风险预算剩余" value={formatPct(efficiency?.risk_budget_remaining)} />
            <MetricMini label="组合 beta" value={formatNumber(efficiency?.portfolio_beta)} />
          </div>
          <div className="mt-5 space-y-3">
            {checks.length ? checks.slice(0, 8).map((item: any) => (
              <div key={item.name} className="rounded-2xl border border-stone-200 px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm font-bold text-slate-900">{item.name}</div>
                  <TonePill status={item.status || 'unknown'}>{item.status || 'unknown'}</TonePill>
                </div>
                <div className="mt-2 text-sm leading-6 text-stone-600">{item.detail || '暂无细节'}</div>
              </div>
            )) : <Empty text="参数一致性检查当前不可用。" />}
          </div>
        </Panel>
      </section>

      <section className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
        <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">当前全局阻断</div>
        <div className="mt-2 text-2xl font-black tracking-tight text-slate-900">主链风险卡</div>
        <div className="mt-5 space-y-3">
          {blockers.length ? blockers.map((item: any, index: number) => (
            <div key={`${item.type}-${index}`} className="rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4">
              <div className="inline-flex items-center gap-2 text-sm font-bold text-rose-600">
                <ShieldX className="h-4 w-4" />
                <span>{item.title}</span>
              </div>
              <div className="mt-2 text-sm leading-6 text-stone-600">{item.detail}</div>
            </div>
          )) : <Empty text="当前没有全局风险阻断。" />}
        </div>
      </section>
    </div>
  );
};

const Panel = ({ eyebrow, title, children }: { eyebrow: string; title: string; children: React.ReactNode }) => (
  <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
    <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">{eyebrow}</div>
    <div className="mt-2 text-2xl font-black tracking-tight text-slate-900">{title}</div>
    <div className="mt-5">{children}</div>
  </div>
);

const MetricMini = ({ label, value }: { label: string; value: string }) => (
  <div className="rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4">
    <div className="text-[11px] uppercase tracking-[0.18em] text-stone-400">{label}</div>
    <div className="mt-2 text-lg font-bold text-slate-900">{value}</div>
  </div>
);

const Empty = ({ text }: { text: string }) => (
  <div className="rounded-2xl border border-dashed border-stone-200 px-4 py-6 text-sm text-stone-400">{text}</div>
);

const formatPct = (value: any) => `${(Number(value || 0) * 100).toFixed(1)}%`;
const formatNumber = (value: any) => Number(value || 0).toFixed(2);

export default RiskPage;
