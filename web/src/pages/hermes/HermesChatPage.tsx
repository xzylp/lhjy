import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bolt, CirclePlay, Plus, SendHorizontal, Sparkles } from 'lucide-react';
import {
  appendHermesMessage,
  askHermesNaturalLanguage,
  createHermesSession,
  executeHermesCommand,
  fetchHermesActivity,
  fetchHermesSession,
  fetchHermesSessions,
  fetchHermesWorkspace,
  previewHermesCommand,
} from '../../api/hermes';
import {
  HermesEmpty,
  HermesMetric,
  HermesPanel,
  HermesPill,
  HermesSectionTitle,
  formatHermesLabel,
  formatHermesRole,
  formatHermesTime,
} from '../../components/hermes/HermesCommon';

const QUICK_COMMANDS = ['查看状态', '查看角色', '查看技能', '查看工具', '查看调度', '查看 A股 集成'];

const shouldUseNaturalLanguageFallback = (command: string, previewResult: any) => {
  const text = command.trim();
  if (!text) {
    return false;
  }
  const resolvedTarget = String(previewResult?.resolved?.target || '').trim().toLowerCase();
  const helpIntent = resolvedTarget === 'help';
  const explicitCommandPrefixes = ['查看', '打开', '列出', '切到', 'show', 'open', 'list'];
  const looksLikeExplicitCommand = explicitCommandPrefixes.some((prefix) => text.startsWith(prefix));
  return helpIntent || !looksLikeExplicitCommand;
};

const HermesChatPage = () => {
  const queryClient = useQueryClient();
  const [activeSessionId, setActiveSessionId] = useState('');
  const [command, setCommand] = useState('');
  const [previewResult, setPreviewResult] = useState<any>(null);

  const { data: workspace } = useQuery({
    queryKey: ['hermes-workspace'],
    queryFn: fetchHermesWorkspace,
    refetchInterval: 15000,
  });
  const { data: sessions } = useQuery({
    queryKey: ['hermes-sessions'],
    queryFn: fetchHermesSessions,
    refetchInterval: 10000,
  });
  const { data: activity } = useQuery({
    queryKey: ['hermes-activity'],
    queryFn: fetchHermesActivity,
    refetchInterval: 8000,
  });

  useEffect(() => {
    if (!activeSessionId) {
      const nextSessionId = sessions?.active_session_id || workspace?.active_session_id || '';
      if (nextSessionId) {
        setActiveSessionId(nextSessionId);
      }
    }
  }, [activeSessionId, sessions, workspace]);

  const { data: sessionDetail } = useQuery({
    queryKey: ['hermes-session', activeSessionId],
    queryFn: () => fetchHermesSession(activeSessionId),
    enabled: Boolean(activeSessionId),
    refetchInterval: 8000,
  });

  const createSessionMutation = useMutation({
    mutationFn: () => createHermesSession({ title: '新对话', profile_id: 'runtime_scout', model_id: 'workspace-default' }),
    onSuccess: (result) => {
      setActiveSessionId(result.item.session_id);
      queryClient.invalidateQueries({ queryKey: ['hermes-sessions'] });
      queryClient.invalidateQueries({ queryKey: ['hermes-workspace'] });
      queryClient.invalidateQueries({ queryKey: ['hermes-activity'] });
    },
  });

  const previewMutation = useMutation({
    mutationFn: (nextCommand: string) => previewHermesCommand({ command: nextCommand, session_id: activeSessionId }),
    onSuccess: (result) => setPreviewResult(result),
  });

  const executeMutation = useMutation({
    mutationFn: async (nextCommand: string) => {
      const sessionId = activeSessionId || String(sessions?.active_session_id || workspace?.active_session_id || '');
      if (shouldUseNaturalLanguageFallback(nextCommand, previewResult)) {
        const answer = await askHermesNaturalLanguage({
          question: nextCommand,
          bot_role: 'main',
          source: 'hermes_chat',
        });
        if (sessionId) {
          await appendHermesMessage({ session_id: sessionId, role: 'user', content: nextCommand });
          await appendHermesMessage({
            session_id: sessionId,
            role: 'assistant',
            content: (answer?.answer_lines || []).join('\n') || '当前没有可用回答。',
          });
        }
        return { mode: 'natural_language', answer };
      }
      const result = await executeHermesCommand({ command: nextCommand, session_id: sessionId });
      return { mode: 'command', result };
    },
    onSuccess: () => {
      setCommand('');
      setPreviewResult(null);
      queryClient.invalidateQueries({ queryKey: ['hermes-sessions'] });
      queryClient.invalidateQueries({ queryKey: ['hermes-session', activeSessionId] });
      queryClient.invalidateQueries({ queryKey: ['hermes-activity'] });
      queryClient.invalidateQueries({ queryKey: ['hermes-workspace'] });
    },
  });

  const sessionItems = sessions?.items || [];
  const messages = sessionDetail?.item?.messages || [];
  const activityItems = activity?.items || [];

  const summaryMetrics = useMemo(() => workspace?.counts || {}, [workspace]);

  return (
    <div className="space-y-6">
      <HermesPanel className="overflow-hidden px-5 py-5 md:px-7 md:py-6">
        <div className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
          <div>
            <HermesSectionTitle
              eyebrow="Hermes 工作台"
              title="今天要处理什么？"
              description="这里不是单纯聊天窗口，而是 Hermes 的通用控制与编排入口。可以直接查询能力、调度、角色、工具，也可以切到办公台查看 A 股业务域。"
            />
            <div className="mt-5 flex flex-wrap gap-2">
              {QUICK_COMMANDS.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="rounded-full border border-[#eadfcc] bg-white px-4 py-2 text-sm text-[#665b4d] transition hover:bg-[#fff7ea]"
                  onClick={() => setCommand(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <HermesMetric label="会话数" value={summaryMetrics.sessions || 0} hint="当前工作区会话数" />
            <HermesMetric label="角色数" value={summaryMetrics.profiles || 0} hint="角色合同与提示词" />
            <HermesMetric label="工具数" value={summaryMetrics.tools || 0} hint="通用与业务集成工具" />
            <HermesMetric label="调度数" value={summaryMetrics.schedules || 0} hint="调度与 cron 合同" />
          </div>
        </div>
      </HermesPanel>

      <div className="grid gap-5 xl:grid-cols-[0.78fr_1.6fr_0.82fr]">
        <HermesPanel className="p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-[0.16em] text-[#9f9385]">会话</div>
              <div className="mt-1 text-xl font-black tracking-tight text-[#211b14]">会话列表</div>
            </div>
            <button
              type="button"
              className="inline-flex h-10 w-10 items-center justify-center rounded-2xl border border-[#ecdfcf] bg-[#fff8ee] text-[#6d604e]"
              onClick={() => createSessionMutation.mutate()}
            >
              <Plus className="h-4 w-4" />
            </button>
          </div>

          <div className="mt-4 space-y-2">
            {sessionItems.length === 0 ? (
              <HermesEmpty text="当前还没有 Hermes 会话。" />
            ) : (
              sessionItems.map((item: any) => (
                <button
                  key={item.session_id}
                  type="button"
                  onClick={() => setActiveSessionId(item.session_id)}
                  className={[
                    'w-full rounded-[22px] border px-4 py-4 text-left transition',
                    activeSessionId === item.session_id
                      ? 'border-[#d2e1ff] bg-[#edf4ff]'
                      : 'border-[#eee4d8] bg-[#fcfbf8] hover:bg-white',
                  ].join(' ')}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="font-semibold text-[#201a13]">{item.title}</div>
                    <HermesPill tone={activeSessionId === item.session_id ? 'ok' : 'neutral'}>
                      {item.model_id}
                    </HermesPill>
                  </div>
                  <div className="mt-2 line-clamp-2 text-sm leading-6 text-[#74695c]">{item.last_message_preview || '暂无消息'}</div>
                  <div className="mt-3 text-xs text-[#a19381]">{formatHermesTime(item.updated_at)}</div>
                </button>
              ))
            )}
          </div>
        </HermesPanel>

        <HermesPanel className="flex min-h-[720px] flex-col">
          <div className="border-b border-[#f0e5d7] px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.16em] text-[#9f9385]">对话</div>
              <div className="mt-1 text-2xl font-black tracking-tight text-[#1f1913]">
                  {sessionDetail?.item?.title || '新对话'}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <HermesPill tone="ok">{formatHermesLabel(workspace?.run_mode || 'unknown')}</HermesPill>
                <HermesPill tone="warning">{workspace?.live_enabled ? '实盘开启' : '实盘关闭'}</HermesPill>
              </div>
            </div>
          </div>

          <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5">
            {messages.length === 0 ? (
              <div className="flex h-full items-center justify-center">
                <HermesEmpty text="输入控制指令，Hermes 会返回真实能力域或业务域数据。" />
              </div>
            ) : (
              messages.map((message: any) => (
                <div
                  key={message.message_id}
                  className={[
                    'max-w-[88%] rounded-[24px] px-4 py-4 text-sm leading-7',
                    message.role === 'assistant'
                      ? 'border border-[#eee4d8] bg-[#fffdfa] text-[#3c352e]'
                      : 'ml-auto bg-[#1f2635] text-white',
                  ].join(' ')}
                >
                  <div className="mb-1 text-[10px] uppercase tracking-[0.18em] opacity-70">{formatHermesRole(message.role)}</div>
                  <div className="whitespace-pre-wrap">{message.content}</div>
                </div>
              ))
            )}
          </div>

          <div className="border-t border-[#f0e5d7] px-5 py-4">
            {previewResult?.resolved ? (
              <div className="mb-3 flex items-center gap-2 text-sm text-[#665b4d]">
                <Sparkles className="h-4 w-4 text-[#de9a00]" />
                <span>
                  预判将命中：{previewResult.resolved.label}
                  {shouldUseNaturalLanguageFallback(command, previewResult) ? '；本次会转主控自然语言链路' : ''}
                </span>
              </div>
            ) : null}
            <div className="rounded-[26px] border border-[#eadfcc] bg-[#fffdf9] p-2">
              <div className="flex items-end gap-2">
                <textarea
                  value={command}
                  onChange={(event) => setCommand(event.target.value)}
                  placeholder="输入控制指令，例如：查看工具、查看调度、查看 A股 集成"
                  className="min-h-[86px] flex-1 resize-none border-0 bg-transparent px-3 py-3 text-sm leading-7 text-[#362f27] outline-none placeholder:text-[#b5a89b]"
                />
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="inline-flex h-12 items-center justify-center rounded-2xl border border-[#eadfcc] bg-[#fff7ea] px-4 text-sm font-semibold text-[#715b30]"
                    onClick={() => previewMutation.mutate(command)}
                    disabled={!command.trim()}
                  >
                    <CirclePlay className="mr-2 h-4 w-4" />
                    预判
                  </button>
                  <button
                    type="button"
                    className="inline-flex h-12 items-center justify-center rounded-2xl bg-[#dde9ff] px-4 text-sm font-semibold text-[#21457e]"
                    onClick={() => executeMutation.mutate(command)}
                    disabled={!command.trim()}
                  >
                    <SendHorizontal className="mr-2 h-4 w-4" />
                    执行
                  </button>
                </div>
              </div>
            </div>
          </div>
        </HermesPanel>

        <HermesPanel className="p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[11px] uppercase tracking-[0.16em] text-[#9f9385]">事件</div>
            <div className="mt-1 text-xl font-black tracking-tight text-[#211b14]">事件流</div>
          </div>
            <HermesPill tone="neutral">{activityItems.length} 条</HermesPill>
          </div>

          <div className="mt-4 space-y-3">
            {activityItems.length === 0 ? (
              <HermesEmpty text="当前还没有 Hermes 事件记录。" />
            ) : (
              activityItems.slice(0, 12).map((item: any) => (
                <div key={item.event_id} className="rounded-[22px] border border-[#eee4d8] bg-[#fcfbf8] px-4 py-4">
                  <div className="flex items-center gap-2">
                    <Bolt className="h-4 w-4 text-[#111827]" />
                    <div className="text-sm font-semibold text-[#201a13]">{item.title}</div>
                  </div>
                  <div className="mt-2 text-sm leading-6 text-[#72675b]">{item.detail}</div>
                  <div className="mt-3 flex items-center justify-between text-xs text-[#a19381]">
                    <span>{item.kind}</span>
                    <span>{formatHermesTime(item.created_at)}</span>
                  </div>
                </div>
              ))
            )}
          </div>
        </HermesPanel>
      </div>
    </div>
  );
};

export default HermesChatPage;
