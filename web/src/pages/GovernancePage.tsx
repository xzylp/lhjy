import React, { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Bot, History, Settings, Sliders } from 'lucide-react';
import {
  fetchAgentScores,
  fetchFeishuBots,
  fetchParamProposals,
  fetchParameterConsistency,
  previewNaturalLanguageAdjustment,
} from '../api';
import { TonePill } from '../components/Common';

const GovernancePage = () => {
  const [instruction, setInstruction] = useState('把测试仓位改到四成，保留逆回购底仓。');
  const { data: proposals } = useQuery({
    queryKey: ['paramProposals'],
    queryFn: fetchParamProposals,
    refetchInterval: 15000,
  });
  const { data: scores } = useQuery({
    queryKey: ['agentScores'],
    queryFn: () => fetchAgentScores(),
    refetchInterval: 15000,
  });
  const { data: bots } = useQuery({
    queryKey: ['feishuBots'],
    queryFn: fetchFeishuBots,
    refetchInterval: 20000,
  });
  const { data: consistency } = useQuery({
    queryKey: ['parameterConsistency'],
    queryFn: fetchParameterConsistency,
    refetchInterval: 30000,
  });

  const previewMutation = useMutation({
    mutationFn: (text: string) => previewNaturalLanguageAdjustment(text),
  });

  const proposalItems = proposals?.items || [];
  const scoreItems = scores?.items || [];
  const botItems = bots?.items || [];
  const consistencyChecks = consistency?.checks || [];

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-3xl font-black tracking-tight text-slate-900">治理与调参</h1>
        <p className="mt-2 text-sm leading-7 text-stone-500">
          这里展示真实参数提案、Agent 评分、三机器人状态和自然语言调参预判，不再保留静态壳子。
        </p>
      </section>

      <section className="rounded-[32px] border border-stone-200 bg-slate-900 p-8 text-white shadow-2xl">
        <div className="flex items-center gap-3">
          <Sliders className="h-5 w-5 text-amber-300" />
          <div className="text-sm font-bold tracking-[0.2em] text-stone-300 uppercase">自然语言调参预判</div>
        </div>
        <div className="mt-5 space-y-4">
          <textarea
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
            className="min-h-[120px] w-full rounded-3xl border border-white/10 bg-white/5 px-5 py-4 text-sm leading-7 text-white outline-none placeholder:text-stone-500"
          />
          <button
            type="button"
            onClick={() => previewMutation.mutate(instruction)}
            disabled={!instruction.trim() || previewMutation.isPending}
            className="rounded-2xl bg-amber-300 px-5 py-3 text-sm font-black text-slate-900 transition hover:bg-amber-200 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {previewMutation.isPending ? '正在预判' : '预判本次调参'}
          </button>
        </div>
        <div className="mt-5 rounded-3xl border border-white/10 bg-white/5 p-5 text-sm leading-7 text-stone-200">
          {(previewMutation.data?.reply_lines || previewMutation.data?.summary_lines || []).length ? (
            (previewMutation.data?.reply_lines || previewMutation.data?.summary_lines || []).map((line: string, index: number) => (
              <div key={index}>{line}</div>
            ))
          ) : (
            <div>这里会显示真实参数服务返回的预判结果。</div>
          )}
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <Panel title="最近参数提案" eyebrow="Proposals" icon={History}>
          <div className="space-y-3">
            {proposalItems.length ? proposalItems.slice(0, 8).map((item: any, index: number) => (
              <div key={`${item.param_key}-${index}`} className="rounded-2xl border border-stone-200 px-4 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm font-bold text-slate-900">{item.param_key}</div>
                  <TonePill status={item.status || 'unknown'}>{item.status || 'unknown'}</TonePill>
                </div>
                <div className="mt-2 text-sm text-stone-600">新值：{String(item.new_value ?? '--')}</div>
                <div className="mt-1 text-xs text-stone-500">发起人：{item.proposed_by || '--'} / 生效期：{item.effective_period || '--'}</div>
              </div>
            )) : <Empty text="当前还没有参数提案记录。" />}
          </div>
        </Panel>

        <Panel title="Agent 评分与三机器人" eyebrow="Agents / Bots" icon={Bot}>
          <div className="grid gap-5 lg:grid-cols-2">
            <div className="space-y-3">
              {scoreItems.length ? scoreItems.slice(0, 6).map((item: any, index: number) => (
                <div key={`${item.agent_id}-${index}`} className="rounded-2xl border border-stone-200 px-4 py-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-sm font-bold text-slate-900">{item.agent_id}</div>
                    <div className="text-sm font-semibold text-slate-700">{item.new_score ?? item.score ?? '--'}</div>
                  </div>
                  <div className="mt-2 text-xs text-stone-500">
                    胜率 {item.win_rate ?? '--'} / 权重 {item.weight_value ?? '--'} / 连续亏损 {item.consecutive_losses ?? '--'}
                  </div>
                </div>
              )) : <Empty text="当前没有 Agent 评分数据。" />}
            </div>

            <div className="space-y-3">
              {botItems.length ? botItems.map((item: any) => (
                <div key={item.role} className="rounded-2xl border border-stone-200 px-4 py-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-sm font-bold text-slate-900">{item.bot_name || item.label}</div>
                    <TonePill status={item.reported_status || 'unknown'}>{item.reported_status || 'unknown'}</TonePill>
                  </div>
                  <div className="mt-2 text-xs leading-6 text-stone-500">{item.routing_scope}</div>
                </div>
              )) : <Empty text="当前没有飞书机器人状态。" />}
            </div>
          </div>
        </Panel>
      </section>

      <section className="rounded-[28px] border border-stone-200 bg-white/90 p-6 shadow-sm">
        <div className="flex items-center gap-3">
          <Settings className="h-5 w-5 text-stone-500" />
          <div>
            <div className="text-[11px] uppercase tracking-[0.24em] text-stone-400">参数一致性</div>
            <div className="mt-1 text-2xl font-black tracking-tight text-slate-900">部署前自检</div>
          </div>
        </div>
        <div className="mt-5 space-y-3">
          {consistencyChecks.length ? consistencyChecks.slice(0, 10).map((item: any) => (
            <div key={item.name} className="rounded-2xl border border-stone-200 px-4 py-4">
              <div className="flex items-center justify-between gap-3">
                <div className="text-sm font-bold text-slate-900">{item.name}</div>
                <TonePill status={item.status || 'unknown'}>{item.status || 'unknown'}</TonePill>
              </div>
              <div className="mt-2 text-sm leading-6 text-stone-600">{item.detail || '暂无细节'}</div>
            </div>
          )) : <Empty text="当前没有参数一致性自检结果。" />}
        </div>
      </section>
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

const Empty = ({ text }: { text: string }) => (
  <div className="rounded-2xl border border-dashed border-stone-200 px-4 py-6 text-sm text-stone-400">{text}</div>
);

export default GovernancePage;
