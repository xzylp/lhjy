import React from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  BellRing,
  Bot,
  ChevronRight,
  Cpu,
  ShieldAlert,
  Wallet,
  Waves,
} from 'lucide-react';
import {
  fetchAccountState,
  fetchAuditLogs,
  fetchOperationsComponents,
  fetchOperationsHealthCheck,
  fetchReadiness,
  fetchSupervisionBoard,
} from '../api';
import { MetricCard, StatusBadge, TonePill } from '../components/Common';

const OverviewPage = () => {
  const { data: readiness, isPending: readinessPending } = useQuery({
    queryKey: ['readiness'],
    queryFn: fetchReadiness,
    refetchInterval: 15000,
  });
  const { data: account } = useQuery({
    queryKey: ['accountState'],
    queryFn: fetchAccountState,
    refetchInterval: 15000,
  });
  const { data: supervision } = useQuery({
    queryKey: ['supervisionBoard'],
    queryFn: fetchSupervisionBoard,
    refetchInterval: 10000,
  });
  const { data: components } = useQuery({
    queryKey: ['operationsComponents'],
    queryFn: fetchOperationsComponents,
    refetchInterval: 20000,
  });
  const { data: opsHealth } = useQuery({
    queryKey: ['operationsHealthCheck'],
    queryFn: fetchOperationsHealthCheck,
    refetchInterval: 60000,
  });
  const { data: audits } = useQuery({
    queryKey: ['audits'],
    queryFn: () => fetchAuditLogs(12),
    refetchInterval: 10000,
  });

  const summary = account?.metrics || {};
  const supervisionItems = supervision?.items || [];
  const attentionItems = supervision?.attention_items || [];
  const readinessChecks = readiness?.checks || [];
  const componentItems = components?.components || [];
  const equityPositions = account?.equity_positions || [];
  const degradedChecks = readinessChecks.filter((item: any) => ['warning', 'invalid', 'blocked'].includes(String(item.status)));
  const workingAgents = supervisionItems.filter((item: any) => ['working', 'ok', 'active'].includes(String(item.status)));
  const overdueAgents = supervisionItems.filter((item: any) => ['overdue', 'needs_work', 'error'].includes(String(item.status)));
  const latestAudits = audits?.records || [];
  const opsSummary = opsHealth?.summary_lines || [];
  const opsOutputLines = opsHealth?.output_lines || [];

  if (readinessPending && !readiness) {
    return <div className="p-8 text-stone-500 animate-pulse">正在装载交易主控台...</div>;
  }

  return (
    <div className="space-y-8">
      <section className="overflow-hidden rounded-[32px] border border-stone-300 bg-[linear-gradient(135deg,_rgba(21,24,27,0.96),_rgba(46,38,31,0.96))] px-6 py-6 text-white shadow-2xl shadow-stone-900/10 md:px-8 md:py-8">
        <div className="grid gap-8 xl:grid-cols-[1.25fr_0.95fr]">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <TonePill status={readiness?.status || 'unknown'}>{`readiness ${readiness?.status || 'unknown'}`}</TonePill>
              <TonePill status={account?.cache_mode || 'cached'}>{`账户缓存 ${account?.cache_mode || 'unknown'}`}</TonePill>
              <TonePill status={supervision?.cycle_state || 'idle'}>{supervision?.cycle_state || 'idle'}</TonePill>
            </div>
            <h1 className="mt-5 max-w-3xl text-3xl font-black tracking-tight md:text-4xl">
              控制台要回答的是今天能不能打、谁在干活、哪里在拖后腿。
            </h1>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-stone-300">
              当前总览统一展示账户新鲜度、Agent 履职、执行边界与链路降级，不再只盯服务存活。程序负责给工具和围栏，Agent 负责消费工具、提交机会、接受质询。
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <div className="inline-flex items-center gap-2 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-stone-200">
                <Activity className="h-4 w-4 text-amber-300" />
                账户与监督面自动轮询中，账户 15s / 监督 10s
              </div>
              <div className="inline-flex items-center gap-2 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-stone-200">
                <BellRing className="h-4 w-4 text-amber-300" />
                {supervision?.notify_recommended ? '当前建议机器人催办' : '当前无需额外催办'}
              </div>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <SignalPanel
              icon={Wallet}
              label="账户状态"
              value={`¥${((summary.total_asset || 0) / 10000).toFixed(2)}w`}
              detail={`现金 ¥${((summary.cash || 0) / 10000).toFixed(2)}w · 股票仓位 ${((summary.current_total_ratio || 0) * 100).toFixed(1)}%`}
            />
            <SignalPanel
              icon={Bot}
              label="Agent 履职"
              value={`${workingAgents.length}/${supervisionItems.length || 0}`}
              detail={`需关注 ${attentionItems.length} 个 · 超时/怠工 ${overdueAgents.length} 个`}
            />
            <SignalPanel
              icon={ShieldAlert}
              label="执行边界"
              value={readiness?.status || 'unknown'}
              detail={degradedChecks[0] ? `${degradedChecks[0]?.name}: ${degradedChecks[0]?.detail}` : '当前无额外降级详情'}
            />
            <SignalPanel
              icon={Cpu}
              label="控制面链路"
              value={componentItems.length ? `${componentItems.length} 组件` : '--'}
              detail={componentItems.map((item: any) => `${item.name}:${item.status}`).slice(0, 3).join(' · ')}
            />
          </div>
        </div>
      </section>

      <section className="grid grid-cols-1 gap-5 xl:grid-cols-4">
        <MetricCard
          title="股票预算剩余"
          value={`¥${((summary.available_test_trade_value || 0) / 10000).toFixed(2)}w`}
          subValue={`测试预算 ${((summary.stock_test_budget_amount || 0) / 10000).toFixed(2)}w`}
          icon={Wallet}
        />
        <MetricCard
          title="总资产"
          value={`¥${((summary.total_asset || 0) / 10000).toFixed(2)}w`}
          subValue={`当日盈亏 ${formatSigned(summary.daily_pnl)}`}
          icon={Waves}
        />
        <MetricCard
          title="催办队列"
          value={attentionItems.length}
          subValue={supervision?.summary_lines?.[supervision?.summary_lines.length - 1] || '暂无额外说明'}
          icon={Bot}
        />
        <MetricCard
          title="降级项"
          value={degradedChecks.length}
          subValue={degradedChecks.length ? degradedChecks.map((item: any) => item.name).join(' / ') : '当前无降级'}
          icon={ShieldAlert}
        />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">当前关键阻断 / 降级</div>
              <h2 className="mt-2 text-2xl font-black tracking-tight text-slate-900">先看影响决策链的事实</h2>
            </div>
            <StatusBadge status={readiness?.status || 'unknown'} />
          </div>
          <div className="mt-6 space-y-3">
            {readinessChecks.map((item: any) => (
              <div
                key={item.name}
                className="flex flex-col gap-3 rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4 md:flex-row md:items-start md:justify-between"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <TonePill status={item.status}>{item.status}</TonePill>
                    <span className="text-sm font-bold text-slate-900">{item.name}</span>
                  </div>
                  <div className="mt-2 text-sm leading-6 text-stone-600">{item.detail || '无详细说明'}</div>
                </div>
                <ChevronRight className="h-4 w-4 shrink-0 text-stone-300" />
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-6">
          <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">账户持仓</div>
                <div className="mt-2 text-2xl font-black tracking-tight text-slate-900">
                  当前持仓 {account?.position_count || 0} 只
                </div>
              </div>
              <TonePill status={account?.fresh ? 'ok' : 'warning'}>
                {account?.fresh ? 'fresh' : 'stale'}
              </TonePill>
            </div>
            <div className="mt-4 space-y-3">
              {equityPositions.length ? (
                equityPositions.slice(0, 5).map((item: any) => {
                  const marketValue = Number(item.quantity || 0) * Number(item.last_price || 0);
                  const pnl = (Number(item.last_price || 0) - Number(item.cost_price || 0)) * Number(item.quantity || 0);
                  return (
                    <div key={item.symbol} className="rounded-2xl border border-stone-200 px-4 py-4">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <div className="text-sm font-bold text-slate-900">{item.symbol}</div>
                          <div className="mt-1 text-xs text-stone-500">
                            持仓 {formatInteger(item.quantity)} 股 · 可用 {formatInteger(item.available)} 股
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="text-sm font-bold text-slate-900">¥{formatCurrency(marketValue)}</div>
                          <div className={`mt-1 text-xs ${pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                            浮盈亏 {formatSigned(pnl)}
                          </div>
                        </div>
                      </div>
                      <div className="mt-3 text-xs leading-6 text-stone-600">
                        成本 {formatPrice(item.cost_price)} · 最新 {formatPrice(item.last_price)}
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="rounded-2xl bg-stone-50 px-4 py-4 text-sm leading-6 text-stone-600">
                  {account?.error || account?.summary_lines?.[0] || '当前没有股票持仓，或者账户缓存尚未返回持仓明细。'}
                </div>
              )}
            </div>
          </div>

          <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">统一巡检</div>
                <div className="mt-2 text-2xl font-black tracking-tight text-slate-900">health_check.sh</div>
              </div>
              <TonePill status={opsHealth?.status || 'unknown'}>
                {opsHealth?.status || 'unknown'}
              </TonePill>
            </div>
            <div className="mt-4 space-y-2 text-sm leading-6 text-stone-600">
              <div>脚本: {opsHealth?.script_path || '--'}</div>
              <div>退出码: {opsHealth?.exit_code ?? '--'} · 时间: {formatDateTime(opsHealth?.checked_at)}</div>
              {opsSummary.map((line: string, index: number) => (
                <div key={`${line}-${index}`} className="rounded-2xl bg-stone-50 px-3 py-2 text-sm text-slate-700">
                  {line}
                </div>
              ))}
            </div>
            <details className="mt-4 rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-3">
              <summary className="cursor-pointer text-sm font-bold text-slate-900">展开查看完整巡检输出</summary>
              <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all text-xs leading-6 text-stone-700">
                {opsOutputLines.length ? opsOutputLines.join('\n') : '当前没有巡检输出。'}
              </pre>
            </details>
          </div>

          <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
            <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">Agent 工作热度</div>
            <div className="mt-4 space-y-3">
              {supervisionItems.slice(0, 6).map((item: any) => (
                <div key={item.agent_id} className="rounded-2xl bg-stone-50 px-4 py-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-bold text-slate-900">{item.agent_id}</div>
                      <div className="mt-1 text-xs text-stone-500">{item.activity_label || '活动痕迹'}</div>
                    </div>
                    <TonePill status={item.status}>{item.status}</TonePill>
                  </div>
                  <div className="mt-3 text-sm leading-6 text-stone-600">
                    {item.reasons?.[0] || '暂无说明'}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
            <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">最近审计流水</div>
            <div className="mt-4 space-y-3">
              {latestAudits.slice(0, 6).map((record: any, idx: number) => (
                <div key={record.audit_id || idx} className="rounded-2xl border border-stone-200 px-4 py-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-xs font-bold uppercase tracking-[0.2em] text-stone-500">{record.category}</div>
                    <div className="text-[11px] text-stone-400">{record.timestamp?.split('T')[1]?.slice(0, 8) || '--:--:--'}</div>
                  </div>
                  <div className="mt-2 text-sm leading-6 text-slate-700">{record.message}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
};

const SignalPanel = ({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: any;
  label: string;
  value: string;
  detail: string;
}) => (
  <div className="rounded-[28px] border border-white/10 bg-white/5 p-5">
    <div className="flex items-center justify-between">
      <div className="text-[11px] uppercase tracking-[0.22em] text-stone-400">{label}</div>
      <Icon className="h-4 w-4 text-amber-300" />
    </div>
    <div className="mt-3 text-2xl font-black tracking-tight">{value}</div>
    <div className="mt-2 text-sm leading-6 text-stone-300">{detail}</div>
  </div>
);

const formatSigned = (value: number | undefined) => {
  const numeric = Number(value || 0);
  return `${numeric >= 0 ? '+' : ''}${numeric.toFixed(2)}`;
};

const formatCurrency = (value: number | undefined) => {
  return Number(value || 0).toFixed(2);
};

const formatPrice = (value: number | undefined) => {
  return Number(value || 0).toFixed(3);
};

const formatInteger = (value: number | undefined) => {
  return Number(value || 0).toFixed(0);
};

const formatDateTime = (value: string | undefined) => {
  if (!value) {
    return '--';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString('zh-CN', { hour12: false });
};

export default OverviewPage;
