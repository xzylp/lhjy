import React from 'react';
import { useParams, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ChevronLeft, Info, ShieldX, TrendingUp } from 'lucide-react';
import { fetchSymbolDeskBrief } from '../api';
import { TonePill } from '../components/Common';

const SymbolDetailPage = () => {
  const { symbol } = useParams();
  const { data, isLoading } = useQuery({
    queryKey: ['symbolDeskBrief', symbol],
    queryFn: () => fetchSymbolDeskBrief(symbol!),
    enabled: Boolean(symbol),
    refetchInterval: 15000,
  });

  if (isLoading) {
    return <div className="p-8 text-stone-400">正在聚合个股工作台事实...</div>;
  }

  const tradeAdvice = data?.trade_advice || {};
  const blockers = data?.blockers || [];
  const nextActions = data?.next_actions || [];
  const riskNotes = data?.risk_notes || [];
  const summaryLines = data?.summary_lines || [];
  const position = data?.position || {};
  const relatedCandidates = data?.related_candidates || [];

  return (
    <div className="space-y-8">
      <Link to="/dashboard/discussion" className="inline-flex items-center text-stone-400 transition hover:text-slate-900">
        <ChevronLeft className="mr-1 h-4 w-4" />
        <span className="text-sm font-bold uppercase tracking-[0.18em]">返回机会流</span>
      </Link>

      <div className="grid gap-8 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="space-y-8">
          <section className="rounded-[32px] border border-stone-200 bg-white/90 p-8 shadow-sm">
            <div className="flex flex-wrap items-center gap-3">
              <TonePill status={tradeAdvice?.recommendation_level || 'unknown'}>
                {tradeAdvice?.recommendation_level || 'unknown'}
              </TonePill>
              <TonePill status={blockers.length ? 'warning' : 'ok'}>
                {blockers.length ? '存在阻断' : '无硬阻断'}
              </TonePill>
            </div>
            <h1 className="mt-5 text-4xl font-black tracking-tight text-slate-900">{data?.name || symbol}</h1>
            <div className="mt-2 font-mono text-sm text-stone-400">{symbol}</div>
            <div className="mt-6 rounded-3xl border border-stone-200 bg-stone-50/70 p-5">
              <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">交易结论</div>
              <div className="mt-2 text-2xl font-black tracking-tight text-slate-900">
                {tradeAdvice?.summary || '当前还没有成熟交易结论。'}
              </div>
              <div className="mt-3 text-sm text-stone-600">立场：{tradeAdvice?.stance || '--'}</div>
            </div>
          </section>

          <section className="rounded-[32px] border border-stone-200 bg-white/90 p-8 shadow-sm">
            <div className="flex items-center gap-3">
              <Info className="h-5 w-5 text-blue-500" />
              <div>
                <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">研究与盘面事实</div>
                <div className="mt-1 text-2xl font-black tracking-tight text-slate-900">结构化底稿</div>
              </div>
            </div>
            <div className="mt-5 space-y-3 text-sm leading-7 text-stone-700">
              {summaryLines.length ? summaryLines.map((line: string, index: number) => <div key={index}>{line}</div>) : (
                <div>当前还没有补足结构化底稿。</div>
              )}
            </div>
          </section>
        </div>

        <div className="space-y-6">
          <Panel title="交易台建议" eyebrow="Desk Advice" icon={TrendingUp}>
            <InfoRow label="建议级别" value={tradeAdvice?.recommendation_level || '--'} />
            <InfoRow label="立场" value={tradeAdvice?.stance || '--'} />
            <InfoRow label="持仓数量" value={position?.quantity ? String(position.quantity) : '当前未持仓'} />
            <div className="mt-4 space-y-2 text-sm leading-6 text-stone-600">
              {nextActions.length ? nextActions.map((line: string, index: number) => <div key={index}>{line}</div>) : <div>当前没有额外下一步建议。</div>}
            </div>
          </Panel>

          <Panel title="阻断与风险提示" eyebrow="Risk" icon={ShieldX}>
            <div className="space-y-3">
              {blockers.length ? blockers.map((item: string, index: number) => (
                <div key={index} className="rounded-2xl border border-rose-100 bg-rose-50/60 px-4 py-4 text-sm leading-6 text-rose-700">
                  {item}
                </div>
              )) : (
                <div className="rounded-2xl border border-emerald-100 bg-emerald-50/60 px-4 py-4 text-sm text-emerald-700">
                  当前没有执行侧硬阻断。
                </div>
              )}
              {riskNotes.length ? riskNotes.map((item: string, index: number) => (
                <div key={`risk-${index}`} className="rounded-2xl border border-stone-200 px-4 py-4 text-sm leading-6 text-stone-600">
                  {item}
                </div>
              )) : null}
            </div>
          </Panel>

          <Panel title="相关候选" eyebrow="Comparables" icon={Info}>
            <div className="space-y-3">
              {relatedCandidates.length ? relatedCandidates.map((item: any) => (
                <div key={item.symbol} className="rounded-2xl border border-stone-200 px-4 py-4">
                  <div className="text-sm font-bold text-slate-900">{item.symbol} {item.name || ''}</div>
                  <div className="mt-2 text-xs leading-6 text-stone-500">
                    {item.final_status || 'unknown'} / risk {item.risk_gate || '--'} / audit {item.audit_gate || '--'}
                  </div>
                </div>
              )) : <div className="text-sm text-stone-400">当前没有可比较的相关候选。</div>}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
};

const Panel = ({
  eyebrow,
  title,
  icon: Icon,
  children,
}: {
  eyebrow: string;
  title: string;
  icon: any;
  children: React.ReactNode;
}) => (
  <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
    <div className="flex items-center gap-3">
      <Icon className="h-5 w-5 text-stone-500" />
      <div>
        <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">{eyebrow}</div>
        <div className="mt-1 text-2xl font-black tracking-tight text-slate-900">{title}</div>
      </div>
    </div>
    <div className="mt-5">{children}</div>
  </div>
);

const InfoRow = ({ label, value }: { label: string; value: string }) => (
  <div className="flex items-center justify-between rounded-2xl border border-stone-200 px-4 py-3">
    <div className="text-sm text-stone-500">{label}</div>
    <div className="text-sm font-bold text-slate-900">{value}</div>
  </div>
);

export default SymbolDetailPage;
