import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, AlertTriangle, Bot, Database, Radar, RefreshCcw, ShieldAlert, TrendingUp, Wallet } from 'lucide-react';
import { fetchMissionControl, fetchServiceStatus, restartService } from '../api';
import { MetricCard, StatusBadge, TonePill } from '../components/Common';

const OverviewPage = () => {
  const queryClient = useQueryClient();
  const { data, isPending } = useQuery({
    queryKey: ['missionControl'],
    queryFn: fetchMissionControl,
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
    staleTime: 1000,
  });
  const { data: serviceStatus } = useQuery({
    queryKey: ['serviceStatus'],
    queryFn: fetchServiceStatus,
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
    staleTime: 1000,
  });

  const restartMutation = useMutation({
    mutationFn: () => restartService('control-plane'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['missionControl'] });
      queryClient.invalidateQueries({ queryKey: ['serviceStatus'] });
      window.setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['missionControl'] });
        queryClient.invalidateQueries({ queryKey: ['serviceStatus'] });
      }, 4000);
    },
  });

  if (isPending && !data) {
    return <div className="p-8 text-stone-500 animate-pulse">正在装载任务主链...</div>;
  }

  const market = data?.market || {};
  const account = data?.account || {};
  const metrics = account?.metrics || {};
  const blockers = data?.blockers || [];
  const runtime = data?.runtime || {};
  const topPicks = runtime?.top_picks || [];
  const fastOpportunity = data?.fast_opportunity_scan || {};
  const execution = data?.execution || {};
  const precheck = execution?.precheck || {};
  const dispatch = execution?.dispatch || {};
  const history = data?.history || {};
  const latestHistoryDaily = history?.latest_daily || {};
  const latestHistoryMinute = history?.latest_minute || {};
  const latestHistoryBehavior = history?.latest_behavior_profiles || {};
  const historySummaryLines = history?.summary_lines || [];
  const supervision = data?.supervision || {};
  const positions = account?.equity_positions || [];
  const timeline = data?.timeline || [];
  const bots = data?.feishu_bots?.items || [];
  const serviceDetail = serviceStatus?.details?.['control-plane'] || {};
  const accountSummaryLines = account?.summary_lines || [];

  return (
    <div className="space-y-8">
      <section className="overflow-hidden rounded-[32px] border border-stone-300 bg-[linear-gradient(135deg,_rgba(21,24,27,0.96),_rgba(46,38,31,0.96))] px-6 py-6 text-white shadow-2xl shadow-stone-900/10 md:px-8 md:py-8">
        <div className="grid gap-8 xl:grid-cols-[1.2fr_0.8fr]">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <TonePill status={data?.readiness?.status || 'unknown'}>{`readiness ${data?.readiness?.status || 'unknown'}`}</TonePill>
              <TonePill status={market?.regime_label || 'unknown'}>{market?.regime_label || 'unknown'}</TonePill>
              <TonePill status={dispatch?.status || 'unknown'}>{dispatch?.status || 'dispatch_unknown'}</TonePill>
            </div>
            <h1 className="mt-5 max-w-3xl text-3xl font-black tracking-tight md:text-4xl">
              首页直接回答今天卡在哪、谁在推进、能不能继续下单。
            </h1>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-stone-300">
              这里统一聚合市场态势、机会流、讨论收敛、执行预检、持仓巡视、监督催办和当前阻断，不再让你到处翻页面找状态。
            </p>
            <div className="mt-6 flex flex-wrap gap-3 text-sm text-stone-200">
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                市场态势：{market?.regime_label || 'unknown'} / 热门板块 {(market?.hot_sectors || []).slice(0, 3).join('、') || '暂无'}
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                执行预检：通过 {precheck?.approved_count || 0} / 阻断 {precheck?.blocked_count || 0}
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                监督状态：attention {supervision?.attention_items?.length || 0} / notify {supervision?.notify_items?.length || 0}
              </div>
            </div>
          </div>

          <div className="rounded-[28px] border border-white/10 bg-white/5 p-5">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">控制面</div>
                <div className="mt-2 text-2xl font-black tracking-tight">control-plane</div>
              </div>
              <StatusBadge status={serviceStatus?.services?.['control-plane'] || 'unknown'} className="text-white" />
            </div>
            <div className="mt-4 space-y-2 text-sm text-stone-300">
              <div>scope：{serviceDetail?.scope || '--'}</div>
              <div>unit：{serviceDetail?.unit || '--'}</div>
              <div>机器人：{bots.length} 个已注册</div>
            </div>
            <button
              type="button"
              onClick={() => restartMutation.mutate()}
              disabled={restartMutation.isPending}
              className="mt-5 inline-flex items-center rounded-2xl border border-white/15 bg-white/10 px-4 py-3 text-sm font-bold text-white transition hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RefreshCcw className="mr-2 h-4 w-4" />
              {restartMutation.isPending ? '正在重启控制面' : '一键重启控制面'}
            </button>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-1 gap-5 xl:grid-cols-5">
        <MetricCard
          title="总资产"
          value={`¥${((metrics?.total_asset || 0) / 10000).toFixed(2)}w`}
          subValue={
            account?.status === 'ok'
              ? `现金 ¥${((metrics?.cash || 0) / 10000).toFixed(2)}w`
              : accountSummaryLines[0] || `账户状态 ${account?.status || 'unknown'}`
          }
          icon={Wallet}
        />
        <MetricCard
          title="实时机会流"
          value={(fastOpportunity?.items || []).length}
          subValue={(fastOpportunity?.summary_lines || []).slice(0, 1).join('') || '暂无结构化机会摘要'}
          icon={Radar}
        />
        <MetricCard
          title="当前阻断"
          value={blockers.length}
          subValue={blockers[0]?.title || '当前无显著阻断'}
          icon={AlertTriangle}
        />
        <MetricCard
          title="监督催办"
          value={supervision?.notify_items?.length || 0}
          subValue={(supervision?.summary_lines || []).slice(0, 1).join('') || '暂无催办'}
          icon={Bot}
        />
        <MetricCard
          title="历史底座"
          value={latestHistoryDaily?.row_count || latestHistoryMinute?.row_count || 0}
          subValue={historySummaryLines[0] || '当前还没有历史入湖摘要'}
          icon={Database}
        />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.08fr_0.92fr]">
        <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">当前卡点</div>
              <h2 className="mt-2 text-2xl font-black tracking-tight text-slate-900">先看影响主链推进的事实</h2>
            </div>
            <TonePill status={blockers.length ? blockers[0]?.severity || 'warning' : 'ok'}>
              {blockers.length ? '存在阻断' : '主链畅通'}
            </TonePill>
          </div>
          <div className="mt-6 space-y-3">
            {blockers.length ? (
              blockers.map((item: any, index: number) => (
                <div key={`${item.type}-${index}`} className="rounded-2xl border border-stone-200 bg-stone-50/80 px-4 py-4">
                  <div className="flex items-center gap-2">
                    <TonePill status={item.severity || 'warning'}>{item.severity || 'warning'}</TonePill>
                    <div className="text-sm font-bold text-slate-900">{item.title}</div>
                  </div>
                  <div className="mt-2 text-sm leading-6 text-stone-600">{item.detail}</div>
                  {item.path ? <div className="mt-2 text-xs text-stone-400">入口：{item.path}</div> : null}
                </div>
              ))
            ) : (
              <div className="rounded-2xl border border-emerald-100 bg-emerald-50 px-4 py-4 text-sm text-emerald-700">
                当前未发现需要优先处置的主链阻断。
              </div>
            )}
          </div>
        </div>

        <div className="space-y-6">
          <Panel title="历史底座与增量作业" eyebrow="数据底座">
            <div className="space-y-3 text-sm">
              <StatLine icon={Database} label="日线最近写入" value={`${latestHistoryDaily?.row_count || 0} 行 / ${latestHistoryDaily?.ingested_symbol_count || latestHistoryDaily?.symbol_count || 0} 只`} />
              <StatLine icon={Database} label="分钟线最近写入" value={`${latestHistoryMinute?.row_count || 0} 行 / ${latestHistoryMinute?.symbol_count || 0} 只`} />
              <StatLine icon={Database} label="股性画像最近写入" value={`${latestHistoryBehavior?.row_count || 0} 条 / ${latestHistoryBehavior?.symbol_count || 0} 只`} />
            </div>
            <div className="mt-4 rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4 text-xs leading-6 text-stone-600">
              <div>Parquet：{history?.capabilities?.parquet_enabled ? 'on' : 'off'}</div>
              <div>DuckDB：{history?.capabilities?.duckdb_enabled ? 'on' : 'off'}</div>
              <div>Lake：{history?.capabilities?.lake_root || '--'}</div>
              <div>DB：{history?.capabilities?.db_path || '--'}</div>
            </div>
            <div className="mt-4 space-y-2 text-xs leading-6 text-stone-600">
              {historySummaryLines.slice(0, 4).map((line: string, index: number) => (
                <div key={index}>{line}</div>
              ))}
            </div>
          </Panel>

          <Panel title="机会流与 runtime 推荐" eyebrow="市场机会">
            <div className="space-y-3">
              {topPicks.length ? (
                topPicks.slice(0, 6).map((item: any, index: number) => (
                  <div key={`${item.symbol}-${index}`} className="rounded-2xl border border-stone-200 px-4 py-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-bold text-slate-900">{item.symbol}</div>
                        <div className="mt-1 text-xs text-stone-500">
                          {(item.source || 'runtime').toUpperCase()} · {item.assigned_playbook || item.playbook || '未指定战法'}
                        </div>
                      </div>
                      <div className="text-sm font-semibold text-slate-700">{item.selection_score ?? '--'}</div>
                    </div>
                  </div>
                ))
              ) : (
                <EmptyLine text="当前 runtime top picks 为空。" />
              )}
            </div>
          </Panel>

          <Panel title="执行链与持仓" eyebrow="执行 / 盯盘">
            <div className="space-y-3 text-sm">
              <StatLine icon={ShieldAlert} label="执行派发" value={dispatch?.status || 'unknown'} />
              <StatLine icon={TrendingUp} label="通过预检" value={String(precheck?.approved_count || 0)} />
              <StatLine icon={Activity} label="阻断数量" value={String(precheck?.blocked_count || 0)} />
              <StatLine icon={Wallet} label="当前持仓数" value={String(positions.length || 0)} />
            </div>
            <div className="mt-4 rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4 text-xs leading-6 text-stone-600">
              <div>账户来源：{account?.dashboard_source || account?.cache_mode || '--'}</div>
              <div>账户状态：{account?.status || '--'} {typeof account?.age_seconds === 'number' ? `· age ${Math.round(account.age_seconds)}s` : ''}</div>
              {(accountSummaryLines || []).slice(0, 2).map((line: string, index: number) => (
                <div key={index}>{line}</div>
              ))}
            </div>
            <div className="mt-4 space-y-2 text-xs leading-6 text-stone-600">
              {(precheck?.summary_lines || dispatch?.summary_lines || []).slice(0, 4).map((line: string, index: number) => (
                <div key={index}>{line}</div>
              ))}
            </div>
          </Panel>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.88fr_1.12fr]">
        <Panel title="三机器人分工" eyebrow="飞书">
          <div className="space-y-3">
            {bots.length ? bots.map((item: any) => (
              <div key={item.role} className="rounded-2xl border border-stone-200 px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-bold text-slate-900">{item.bot_name || item.label}</div>
                    <div className="mt-1 text-xs text-stone-500">{item.routing_scope}</div>
                  </div>
                  <TonePill status={item.reported_status || 'unknown'}>{item.reported_status || 'unknown'}</TonePill>
                </div>
                <div className="mt-2 text-xs text-stone-500">bot_id：{item.bot_id || '未配置'} / 最近心跳：{item.last_heartbeat_at || '暂无'}</div>
              </div>
            )) : <EmptyLine text="当前还没有机器人注册信息。" />}
          </div>
        </Panel>

        <Panel title="全流程时间线" eyebrow="Timeline">
          <div className="space-y-3">
            {timeline.length ? timeline.map((item: any, index: number) => (
              <div key={`${item.stage}-${index}`} className="rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm font-bold text-slate-900">{item.title || item.stage}</div>
                  <div className="text-xs text-stone-400">{item.created_at || '--'}</div>
                </div>
                <div className="mt-2 text-sm leading-6 text-stone-600">{item.detail || '暂无明细'}</div>
              </div>
            )) : <EmptyLine text="当前还没有沉淀出可展示的时间线事件。" />}
          </div>
        </Panel>
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

const EmptyLine = ({ text }: { text: string }) => (
  <div className="rounded-2xl border border-dashed border-stone-200 px-4 py-6 text-sm text-stone-400">{text}</div>
);

const StatLine = ({ icon: Icon, label, value }: { icon: any; label: string; value: string }) => (
  <div className="flex items-center justify-between rounded-2xl border border-stone-200 px-4 py-3">
    <div className="inline-flex items-center gap-2 text-stone-600">
      <Icon className="h-4 w-4 text-stone-400" />
      <span>{label}</span>
    </div>
    <div className="text-sm font-bold text-slate-900">{value}</div>
  </div>
);

export default OverviewPage;
