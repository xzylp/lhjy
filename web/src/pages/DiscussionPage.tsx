import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ArrowUpRight, CheckCircle2, ShieldAlert } from 'lucide-react';
import { fetchOpportunityFlow } from '../api';
import { TonePill } from '../components/Common';

const DiscussionPage = () => {
  const [activeTab, setActiveTab] = useState<'selected' | 'watchlist' | 'rejected'>('selected');
  const { data, isPending } = useQuery({
    queryKey: ['opportunityFlow'],
    queryFn: fetchOpportunityFlow,
    refetchInterval: 15000,
  });

  const buckets = useMemo(() => ({
    selected: data?.selected || [],
    watchlist: data?.watchlist || [],
    rejected: data?.rejected || [],
  }), [data]);

  if (isPending && !data) {
    return <div className="p-8 text-stone-400 animate-pulse">正在汇总真实机会流...</div>;
  }

  const displayItems = buckets[activeTab] || [];
  const summaryLines = data?.summary_lines || [];

  return (
    <div className="space-y-8">
      <section className="rounded-[32px] border border-stone-200 bg-white/90 p-6 shadow-sm md:p-8">
        <div className="flex flex-col gap-5 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">机会流</div>
            <h1 className="mt-2 text-3xl font-black tracking-tight text-slate-900">从盘中扫描到执行池去向，一页看全。</h1>
            <div className="mt-4 space-y-1 text-sm leading-6 text-stone-600">
              {summaryLines.map((line: string, index: number) => <div key={index}>{line}</div>)}
            </div>
          </div>

          <div className="flex items-center space-x-1 rounded-2xl border border-stone-200 bg-stone-50 p-1">
            <TabButton active={activeTab === 'selected'} onClick={() => setActiveTab('selected')} label="执行池" count={buckets.selected.length} />
            <TabButton active={activeTab === 'watchlist'} onClick={() => setActiveTab('watchlist')} label="观察池" count={buckets.watchlist.length} />
            <TabButton active={activeTab === 'rejected'} onClick={() => setActiveTab('rejected')} label="淘汰池" count={buckets.rejected.length} />
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.18fr_0.82fr]">
        <div className="space-y-4">
          {displayItems.length ? displayItems.map((item: any) => (
            <DiscussionCard key={item.symbol} item={item} />
          )) : (
            <div className="rounded-[28px] border border-dashed border-stone-200 bg-white/70 px-6 py-16 text-center text-stone-400">
              当前分类下暂无标的。
            </div>
          )}
        </div>

        <div className="space-y-6">
          <Panel eyebrow="Runtime 推荐" title="top picks">
            <div className="space-y-3">
              {(data?.runtime_top_picks || []).length ? (data?.runtime_top_picks || []).slice(0, 6).map((item: any, index: number) => (
                <div key={`${item.symbol}-${index}`} className="rounded-2xl border border-stone-200 px-4 py-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-bold text-slate-900">{item.symbol}</div>
                      <div className="mt-1 text-xs text-stone-500">{item.assigned_playbook || item.playbook || '未指定战法'}</div>
                    </div>
                    <div className="text-sm font-semibold text-slate-700">{item.selection_score ?? '--'}</div>
                  </div>
                </div>
              )) : <Empty text="runtime top picks 当前为空。" />}
            </div>
          </Panel>

          <Panel eyebrow="盘中扫描" title="快机会摘要">
            <div className="space-y-2 text-sm leading-6 text-stone-600">
              {(data?.fast_opportunity_scan?.summary_lines || []).length ? (
                (data?.fast_opportunity_scan?.summary_lines || []).slice(0, 6).map((line: string, index: number) => (
                  <div key={index}>{line}</div>
                ))
              ) : (
                <Empty text="当前没有新的快机会摘要。" />
              )}
            </div>
          </Panel>
        </div>
      </section>
    </div>
  );
};

const TabButton = ({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) => (
  <button
    type="button"
    onClick={onClick}
    className={[
      'rounded-xl px-5 py-2 text-sm font-bold transition',
      active ? 'bg-slate-900 text-white shadow-sm' : 'text-stone-500 hover:bg-white',
    ].join(' ')}
  >
    {label} <span className="ml-2 text-xs opacity-80">{count}</span>
  </button>
);

const DiscussionCard = ({ item }: { item: any }) => {
  const approved = Boolean(item.approved);
  return (
    <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-xl font-black tracking-tight text-slate-900">{item.symbol}</div>
            <div className="text-sm text-stone-500">{item.name}</div>
            <TonePill status={approved ? 'ok' : item.bucket === 'rejected' ? 'warning' : 'standby'}>
              {item.bucket_label}
            </TonePill>
          </div>
          <div className="mt-3 text-sm leading-7 text-stone-700">{item.headline_reason}</div>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right text-sm">
            <div className="font-bold text-slate-900">{item.selection_score ?? '--'}</div>
            <div className="text-xs text-stone-400">score / rank {item.rank ?? '--'}</div>
          </div>
          <Link
            to={item.detail_path}
            className="inline-flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-50 text-slate-500 transition hover:bg-slate-900 hover:text-white"
          >
            <ArrowUpRight className="h-5 w-5" />
          </Link>
        </div>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <InfoBlock label="来源" value={item.source || 'unknown'} />
        <InfoBlock label="当前战法" value={item.assigned_playbook || '未指定'} />
        <InfoBlock label="风控状态" value={item.risk_gate || '未标注'} />
        <InfoBlock label="执行状态" value={item.approved ? '已通过预检' : item.primary_blocker_label || '未进入执行'} />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <InlineBox
          icon={approved ? CheckCircle2 : ShieldAlert}
          title="阻断 / 下一步"
          body={item.primary_blocker_label || item.recommended_next_action || '当前没有额外阻断说明'}
          tone={approved ? 'text-emerald-600' : 'text-amber-600'}
        />
        <InlineBox
          icon={ShieldAlert}
          title="证据缺口 / 待追问"
          body={[...(item.evidence_gaps || []), ...(item.questions_for_round_2 || [])].slice(0, 2).join('；') || '当前未记录额外缺口'}
          tone="text-stone-600"
        />
      </div>
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

const InfoBlock = ({ label, value }: { label: string; value: string }) => (
  <div className="rounded-2xl border border-stone-200 bg-stone-50/70 px-4 py-4">
    <div className="text-[11px] uppercase tracking-[0.18em] text-stone-400">{label}</div>
    <div className="mt-2 text-sm font-semibold text-slate-900">{value}</div>
  </div>
);

const InlineBox = ({ icon: Icon, title, body, tone }: { icon: any; title: string; body: string; tone: string }) => (
  <div className="rounded-2xl border border-stone-200 px-4 py-4">
    <div className={`inline-flex items-center gap-2 text-sm font-bold ${tone}`}>
      <Icon className="h-4 w-4" />
      <span>{title}</span>
    </div>
    <div className="mt-2 text-sm leading-6 text-stone-600">{body}</div>
  </div>
);

const Empty = ({ text }: { text: string }) => (
  <div className="rounded-2xl border border-dashed border-stone-200 px-4 py-6 text-sm text-stone-400">{text}</div>
);

export default DiscussionPage;
