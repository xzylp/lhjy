import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AlertCircle, Bot, Clock3, Siren, Sparkles } from 'lucide-react';
import { fetchSupervisionBoard } from '../api';
import { StatusBadge, TonePill } from '../components/Common';

const AgentsPage = () => {
  const { data: board, isPending } = useQuery({
    queryKey: ['supervisionBoard'],
    queryFn: fetchSupervisionBoard,
    refetchInterval: 10000,
  });

  const agents = board?.items || [];
  const attentionItems = board?.attention_items || [];
  const grouped = useMemo(() => {
    return {
      active: agents.filter((item: any) => ['working', 'ok', 'active'].includes(String(item.status))),
      attention: agents.filter((item: any) => ['overdue', 'needs_work', 'error'].includes(String(item.status))),
      waiting: agents.filter((item: any) => !['working', 'ok', 'active', 'overdue', 'needs_work', 'error'].includes(String(item.status))),
    };
  }, [agents]);

  if (isPending && !board) {
    return <div className="p-8 text-stone-500 animate-pulse">正在同步 Agent 履职台...</div>;
  }

  return (
    <div className="space-y-8">
      <section className="rounded-[32px] border border-stone-300 bg-[linear-gradient(135deg,_#fff7e7_0%,_#f5edda_48%,_#efe4d1_100%)] p-6 shadow-sm md:p-8">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="text-[11px] uppercase tracking-[0.24em] text-stone-500">Agent 履职与催办</div>
            <h1 className="mt-2 text-3xl font-black tracking-tight text-slate-900">不是看他们在不在线，而是看他们有没有干出事实。</h1>
            <p className="mt-4 max-w-3xl text-sm leading-7 text-stone-700">
              这里展示的是履职痕迹、覆盖率、超时原因和催办依据。监督的重点是 Agent 是否根据市场状态主动产出提案、质询、风控意见和执行反馈，而不是机械调用几个工具。
            </p>
            <div className="mt-4 inline-flex items-center rounded-2xl border border-stone-300 bg-white/75 px-4 py-3 text-sm font-medium text-stone-700">
              履职看板自动刷新中，每 10 秒同步一次。
            </div>
          </div>
        </div>

        <div className="mt-6 grid gap-4 md:grid-cols-4">
          <SummaryBox icon={Sparkles} label="活跃履职" value={grouped.active.length} detail="有最近产出或事实痕迹" />
          <SummaryBox icon={Siren} label="待催办" value={attentionItems.length} detail="建议机器人自动提醒" />
          <SummaryBox icon={Clock3} label="轮次状态" value={board?.cycle_state || 'idle'} detail={`round ${board?.round || '--'}`} />
          <SummaryBox icon={Bot} label="监督摘要" value={board?.notify_recommended ? '建议催办' : '稳定'} detail={board?.summary_lines?.[0] || '暂无摘要'} />
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">催办与升级</div>
              <h2 className="mt-2 text-2xl font-black tracking-tight text-slate-900">当前需要被追着干活的角色</h2>
            </div>
            <TonePill status={board?.notify_recommended ? 'warning' : 'ok'}>
              {board?.notify_recommended ? '机器人催办中' : '无需升级'}
            </TonePill>
          </div>

          <div className="mt-6 space-y-4">
            {attentionItems.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-stone-300 px-4 py-10 text-center text-stone-500">
                当前没有需要升级催办的 Agent。
              </div>
            ) : (
              attentionItems.map((item: any) => (
                <div key={item.agent_id} className="rounded-3xl border border-rose-200 bg-rose-50/70 p-5">
                  <div className="flex flex-wrap items-center gap-3">
                    <div className="text-base font-black text-slate-900">{item.agent_id}</div>
                    <TonePill status={item.status}>{item.status}</TonePill>
                    <StatusBadge status={item.acknowledged ? 'acknowledged' : 'unacked'} />
                  </div>
                  <div className="mt-3 space-y-2">
                    {(item.reasons || []).slice(0, 3).map((reason: string, index: number) => (
                      <div key={`${item.agent_id}-${index}`} className="flex items-start gap-2 text-sm leading-6 text-stone-700">
                        <AlertCircle className="mt-1 h-4 w-4 shrink-0 text-rose-500" />
                        <span>{reason}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
          <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">履职明细</div>
          <div className="mt-6 grid gap-4 md:grid-cols-2">
            {agents.map((agent: any) => (
              <AgentCard key={agent.agent_id} agent={agent} />
            ))}
          </div>
        </div>
      </section>
    </div>
  );
};

const SummaryBox = ({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: any;
  label: string;
  value: string | number;
  detail: string;
}) => (
  <div className="rounded-[28px] border border-stone-300/80 bg-white/70 p-5">
    <div className="flex items-center justify-between">
      <div className="text-[11px] uppercase tracking-[0.22em] text-stone-400">{label}</div>
      <Icon className="h-4 w-4 text-amber-700" />
    </div>
    <div className="mt-3 text-2xl font-black tracking-tight text-slate-900">{value}</div>
    <div className="mt-2 text-sm leading-6 text-stone-600">{detail}</div>
  </div>
);

const AgentCard = ({ agent }: { agent: any }) => {
  const reasons = (agent.reasons || []).slice(0, 3);
  const activitySignals = (agent.activity_signals || []).slice(0, 3);

  return (
    <div className="rounded-[28px] border border-stone-200 bg-stone-50/70 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-lg font-black tracking-tight text-slate-900">{agent.agent_id}</div>
          <div className="mt-1 text-xs uppercase tracking-[0.22em] text-stone-400">{agent.role || 'agent role'}</div>
        </div>
        <TonePill status={agent.status}>{agent.status}</TonePill>
      </div>

      <div className="mt-4 rounded-2xl bg-white px-4 py-4">
        <div className="text-[11px] uppercase tracking-[0.2em] text-stone-400">当前履职口径</div>
        <div className="mt-2 text-sm leading-6 text-stone-700">{agent.activity_label || '暂无活动标签'}</div>
      </div>

      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <div className="text-[11px] uppercase tracking-[0.2em] text-stone-400">事实依据</div>
          <div className="mt-2 space-y-2">
            {activitySignals.length === 0 ? (
              <div className="text-sm text-stone-500">暂无结构化活动信号</div>
            ) : (
              activitySignals.map((signal: any, index: number) => (
                <div key={`${agent.agent_id}-signal-${index}`} className="rounded-2xl bg-white px-3 py-3 text-sm text-stone-700">
                  <div className="font-semibold text-slate-800">{signal.source || 'unknown'}</div>
                  <div className="mt-1 text-xs text-stone-500">{signal.last_active_at || '无时间戳'}</div>
                </div>
              ))
            )}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.2em] text-stone-400">催办原因</div>
          <div className="mt-2 space-y-2">
            {reasons.length === 0 ? (
              <div className="text-sm text-stone-500">当前没有催办理由</div>
            ) : (
              reasons.map((reason: string, index: number) => (
                <div key={`${agent.agent_id}-reason-${index}`} className="rounded-2xl bg-white px-3 py-3 text-sm leading-6 text-stone-700">
                  {reason}
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between text-xs text-stone-500">
        <span>最后活跃：{agent.last_active_at ? agent.last_active_at.replace('T', ' ').slice(5, 19) : '无'}</span>
        <span>信号数：{agent.activity_signal_count || 0}</span>
      </div>
    </div>
  );
};

export default AgentsPage;
