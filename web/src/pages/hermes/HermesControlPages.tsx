import { useQuery } from '@tanstack/react-query';
import { BookOpenText, Boxes, CalendarClock, Cpu, Database, HardDriveDownload, Library, Network, PanelTopClose, Settings2, UserCog2 } from 'lucide-react';
import {
  fetchHermesAshareOverview,
  fetchHermesGateway,
  fetchHermesMemory,
  fetchHermesModels,
  fetchHermesProfiles,
  fetchHermesSchedules,
  fetchHermesSessions,
  fetchHermesSettings,
  fetchHermesSkills,
  fetchHermesTools,
  fetchHermesWorkspace,
} from '../../api/hermes';
import {
  HermesEmpty,
  HermesMetric,
  HermesPanel,
  HermesPill,
  HermesSectionTitle,
  formatHermesLabel,
  formatHermesServiceUnitName,
  formatHermesTime,
} from '../../components/hermes/HermesCommon';

const SimpleCard = ({ title, subtitle, meta }: { title: string; subtitle?: string; meta?: string }) => (
  <div className="rounded-[24px] border border-[#ede4d8] bg-[#fcfaf7] px-4 py-4">
    <div className="text-base font-bold text-[#201b14]">{title}</div>
    {subtitle ? <div className="mt-2 text-sm leading-6 text-[#71675c]">{subtitle}</div> : null}
    {meta ? <div className="mt-3 text-xs text-[#a19381]">{meta}</div> : null}
  </div>
);

const RegistryHero = ({
  eyebrow,
  title,
  description,
  icon: Icon,
}: {
  eyebrow: string;
  title: string;
  description: string;
  icon: any;
}) => (
  <HermesPanel className="px-5 py-5 md:px-7 md:py-6">
    <div className="flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
      <HermesSectionTitle eyebrow={eyebrow} title={title} description={description} />
      <div className="inline-flex h-16 w-16 items-center justify-center rounded-[24px] border border-[#ede2d2] bg-[#fff8ee] text-[#4a4337]">
        <Icon className="h-7 w-7" />
      </div>
    </div>
  </HermesPanel>
);

export const HermesSessionsPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-sessions'], queryFn: fetchHermesSessions, refetchInterval: 10000 });
  const items = data?.items || [];
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="会话" title="会话仓库" description="统一展示当前 Hermes 控制台会话、活动会话和最后消息摘要。" icon={Library} />
      <div className="grid gap-4 lg:grid-cols-2">
        {items.length === 0 ? <HermesEmpty text="当前没有会话。" /> : items.map((item: any) => (
          <HermesPanel key={item.session_id} className="p-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-xl font-black tracking-tight text-[#1f1a13]">{item.title}</div>
                <div className="mt-2 text-sm leading-6 text-[#71675c]">{item.last_message_preview || '暂无消息摘要'}</div>
              </div>
              <HermesPill tone={data?.active_session_id === item.session_id ? 'ok' : 'neutral'}>{item.model_id}</HermesPill>
            </div>
            <div className="mt-4 flex items-center justify-between text-xs text-[#a19381]">
              <span>{item.profile_id}</span>
              <span>{formatHermesTime(item.updated_at)}</span>
            </div>
          </HermesPanel>
        ))}
      </div>
    </div>
  );
};

export const HermesProfilesPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-profiles'], queryFn: fetchHermesProfiles });
  const items = data?.items || [];
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="角色" title="角色合同" description="这些配置来自仓库中的 hermes/prompts，是真实可复用的角色提示词合同，不是页面内写死介绍。" icon={UserCog2} />
      <div className="grid gap-4 lg:grid-cols-2">
        {items.map((item: any) => (
          <HermesPanel key={item.id} className="p-5">
            <div className="flex items-center justify-between gap-3">
              <div className="text-xl font-black tracking-tight text-[#1f1a13]">{item.name}</div>
              <HermesPill tone="ok">{item.group}</HermesPill>
            </div>
            <div className="mt-3 text-sm leading-7 text-[#71675c]">{item.summary}</div>
            <div className="mt-4 text-xs text-[#a19381]">{item.path}</div>
          </HermesPanel>
        ))}
      </div>
    </div>
  );
};

export const HermesPersonaPage = () => {
  const { data: workspace } = useQuery({ queryKey: ['hermes-workspace'], queryFn: fetchHermesWorkspace });
  const { data: profiles } = useQuery({ queryKey: ['hermes-profiles'], queryFn: fetchHermesProfiles });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="人格" title="平台人格与操作边界" description="Hermes 的人格不是聊天助手，而是控制与编排中台。平台层负责入口、状态、合同、调度和上下文恢复。" icon={BookOpenText} />
      <div className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">工作台说明</div>
          <div className="mt-4 space-y-3">
            {(workspace?.summary_lines || []).map((line: string, index: number) => (
              <SimpleCard key={index} title={`规则 ${index + 1}`} subtitle={line} />
            ))}
          </div>
        </HermesPanel>
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">角色视角</div>
          <div className="mt-4 space-y-3">
            {(profiles?.items || []).slice(0, 5).map((item: any) => (
              <SimpleCard key={item.id} title={item.name} subtitle={item.summary} meta={formatHermesLabel(item.group)} />
            ))}
          </div>
        </HermesPanel>
      </div>
    </div>
  );
};

export const HermesOfficePage = () => {
  const { data } = useQuery({ queryKey: ['hermes-ashare-overview'], queryFn: fetchHermesAshareOverview, refetchInterval: 15000 });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="办公台" title="A 股业务域挂载" description="Hermes 平台层之上挂的是 A 股运行态、讨论、监督、执行、治理能力。这一页只展示真实挂载口径。" icon={PanelTopClose} />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
        <HermesMetric label="交易日" value={data?.trade_date || '--'} hint="当前业务域参考交易日" />
        <HermesMetric label="运行态" value={formatHermesLabel(data?.runtime?.available ? 'available' : 'missing')} hint={data?.runtime?.endpoint} />
        <HermesMetric label="监督" value={formatHermesLabel(data?.supervision?.available ? 'available' : 'missing')} hint={data?.supervision?.endpoint} />
        <HermesMetric label="执行" value={formatHermesLabel(data?.execution?.bridge_status || 'unknown')} hint={data?.execution?.bridge_path || '暂无桥接路径'} />
        <HermesMetric label="治理" value={data?.governance?.score_count || 0} hint={data?.governance?.endpoint} />
        <HermesMetric label="历史底座" value={formatHermesLabel(data?.history?.capabilities?.parquet_enabled ? 'available' : 'missing')} hint={data?.history?.endpoint || '暂无索引入口'} />
      </div>
      <div className="grid gap-5 xl:grid-cols-[1fr_1fr]">
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">运行态 / 讨论 / 监督</div>
          <div className="mt-4 space-y-3">
            <SimpleCard title="运行态" subtitle={(data?.runtime?.summary_lines || []).join('；') || '暂无运行态摘要'} meta={data?.runtime?.endpoint} />
            <SimpleCard title="讨论" subtitle={(data?.discussion?.summary_lines || []).join('；') || '暂无讨论摘要'} meta={data?.discussion?.endpoint} />
            <SimpleCard title="监督" subtitle={(data?.supervision?.summary_lines || []).join('；') || '暂无监督摘要'} meta={data?.supervision?.endpoint} />
          </div>
        </HermesPanel>
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">执行 / 治理 / 历史</div>
          <div className="mt-4 space-y-3">
            <SimpleCard
              title="执行"
              subtitle={`派发=${formatHermesLabel(data?.execution?.dispatch_status || 'unknown')} 预检=${formatHermesLabel(data?.execution?.precheck_status || 'unknown')} 对账=${formatHermesLabel(data?.execution?.reconciliation_status || 'unknown')}`}
              meta={data?.execution?.dispatch_endpoint}
            />
            <SimpleCard
              title="历史底座"
              subtitle={(data?.history?.summary_lines || []).join('；') || '暂无历史底座摘要'}
              meta={data?.history?.endpoint}
            />
            {(data?.governance?.top_agents || []).length === 0 ? (
              <HermesEmpty text="当前没有 Agent 评分摘要。" />
            ) : (
              (data?.governance?.top_agents || []).map((item: any) => (
                <SimpleCard key={item.agent_id} title={item.agent_id} subtitle={`综合分=${item.new_score} Elo=${item.elo_rating}`} meta={item.rating_tier} />
              ))
            )}
          </div>
        </HermesPanel>
      </div>
    </div>
  );
};

export const HermesModelsPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-models'], queryFn: fetchHermesModels });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="模型" title="模型槽位" description="这里展示的是 Hermes 控制台消费的模型槽位与别名，不直接暴露底层 provider 机密配置。" icon={Cpu} />
      <div className="grid gap-4 lg:grid-cols-3">
        {(data?.items || []).map((item: any) => (
          <HermesPanel key={item.id} className="p-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-lg font-black tracking-tight text-[#1f1a13]">{item.name}</div>
                <div className="mt-1 text-sm text-[#6e6458]">{item.display_model}</div>
              </div>
              <HermesPill tone={item.status === 'active' ? 'ok' : 'neutral'}>{formatHermesLabel(item.status)}</HermesPill>
            </div>
            <div className="mt-4 text-sm leading-7 text-[#71675c]">{item.notes}</div>
            <div className="mt-4 text-xs text-[#a19381]">
              {formatHermesLabel(item.provider_name || item.provider_id)} · {formatHermesLabel(item.latency_profile)} · {formatHermesLabel(item.reasoning_profile)} · {formatHermesLabel(item.routing_share || item.source)}
            </div>
          </HermesPanel>
        ))}
      </div>
    </div>
  );
};

export const HermesSkillsPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-skills'], queryFn: fetchHermesSkills });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="技能" title="能力模块" description="当前技能来源于 Hermes 提示词合同与 cron 合同，可被控制台、调度器和多 agent 编排复用。" icon={Boxes} />
      <div className="grid gap-4 lg:grid-cols-2">
        {(data?.items || []).map((item: any) => (
          <HermesPanel key={item.id} className="p-5">
            <div className="flex items-center justify-between gap-3">
              <div className="text-xl font-black tracking-tight text-[#1f1a13]">{item.name}</div>
              <HermesPill tone="neutral">{formatHermesLabel(item.capability_type)}</HermesPill>
            </div>
            <div className="mt-3 text-sm leading-7 text-[#71675c]">{item.summary}</div>
            <div className="mt-4 flex items-center justify-between text-xs text-[#a19381]">
              <span>{formatHermesLabel(item.group)}</span>
              <span>{item.path}</span>
            </div>
          </HermesPanel>
        ))}
      </div>
    </div>
  );
};

export const HermesMemoryPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-memory'], queryFn: fetchHermesMemory });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="记忆" title="记忆与书签" description="优先展示最近指令、系统书签和最新运行摘要，帮助控制台快速恢复上下文。" icon={Database} />
      <div className="grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">最近指令</div>
          <div className="mt-4 space-y-3">
            {(data?.recent_commands || []).length === 0 ? <HermesEmpty text="当前活动会话还没有用户命令。" /> : (data?.recent_commands || []).map((item: string, index: number) => (
              <SimpleCard key={index} title={`指令 ${index + 1}`} subtitle={item} />
            ))}
          </div>
        </HermesPanel>
        <div className="space-y-5">
          <HermesPanel className="p-5">
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">书签</div>
            <div className="mt-4 space-y-3">
              {(data?.bookmarks || []).map((item: any) => (
                <SimpleCard key={item.id} title={item.label} subtitle={formatHermesLabel(item.available ? 'available' : 'missing')} meta={item.path} />
              ))}
            </div>
          </HermesPanel>
          <HermesPanel className="p-5">
            <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">备注</div>
            <div className="mt-4 space-y-3">
              {[...(data?.workspace_notes || []), ...(data?.runtime_notes || []), ...(data?.review_notes || [])].slice(0, 8).map((item: string, index: number) => (
                <SimpleCard key={index} title={`备注 ${index + 1}`} subtitle={item} />
              ))}
            </div>
          </HermesPanel>
        </div>
      </div>
    </div>
  );
};

export const HermesToolsPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-tools'], queryFn: fetchHermesTools });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="工具" title="工具目录" description="这一层是 Hermes 的通用能力接口，页面、调度器和业务域都应该从这里消费，而不是各自散连底层接口。" icon={HardDriveDownload} />
      <div className="grid gap-4 lg:grid-cols-2">
        {(data?.items || []).map((item: any) => (
          <HermesPanel key={item.id} className="p-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-xl font-black tracking-tight text-[#1f1a13]">{item.name}</div>
                <div className="mt-1 text-xs uppercase tracking-[0.18em] text-[#a19381]">{formatHermesLabel(item.category)}</div>
              </div>
              <HermesPill tone="neutral">{item.method}</HermesPill>
            </div>
            <div className="mt-3 text-sm leading-7 text-[#71675c]">{item.summary}</div>
            <div className="mt-4 text-xs text-[#a19381]">{item.endpoint}</div>
          </HermesPanel>
        ))}
      </div>
    </div>
  );
};

export const HermesSchedulesPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-schedules'], queryFn: fetchHermesSchedules });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="调度" title="调度与计划" description="这里统一展示 scheduler 中的正式任务和 Hermes 自治 cron 合同，方便平台层统一调度。" icon={CalendarClock} />
      <div className="grid gap-4 lg:grid-cols-2">
        {(data?.items || []).map((item: any) => (
          <HermesPanel key={`${item.id}-${item.handler}`} className="p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="text-lg font-black tracking-tight text-[#1f1a13]">{item.name}</div>
              <HermesPill tone={item.source === 'scheduler_registry' ? 'ok' : 'warning'}>{formatHermesLabel(item.status)}</HermesPill>
            </div>
            <div className="mt-3 text-sm leading-7 text-[#71675c]">{item.summary || item.handler}</div>
            <div className="mt-4 flex items-center justify-between text-xs text-[#a19381]">
              <span>{item.cron}</span>
              <span>{formatHermesLabel(item.source)}</span>
            </div>
          </HermesPanel>
        ))}
      </div>
    </div>
  );
};

export const HermesGatewayPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-gateway'], queryFn: fetchHermesGateway, refetchInterval: 15000 });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="网关" title="外部网关与桥接" description="统一展示 Go 平台、Windows 网关、飞书以及执行桥健康状态。" icon={Network} />
      <div className="grid gap-4 md:grid-cols-3">
        <HermesMetric label="Go 平台" value={formatHermesLabel(data?.go_platform?.enabled ? 'enabled' : 'disabled')} hint={data?.go_platform?.base_url} />
        <HermesMetric label="Windows 网关" value={data?.windows_gateway?.token_configured ? '已配置' : '缺失'} hint={data?.windows_gateway?.base_url || '未配置'} />
        <HermesMetric label="桥接健康" value={formatHermesLabel(data?.bridge_health?.overall_status || 'unknown')} hint={data?.bridge_health?.bridge_path || '暂无桥接路径'} />
      </div>
      <div className="grid gap-5 xl:grid-cols-[1fr_1fr]">
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">服务单元</div>
          <div className="mt-4 space-y-3">
            {(data?.service_units || []).map((item: any) => (
              <SimpleCard key={item.id} title={formatHermesServiceUnitName(item.id)} subtitle={item.unit} />
            ))}
          </div>
        </HermesPanel>
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">飞书 / 桥接说明</div>
          <div className="mt-4 space-y-3">
            <SimpleCard
              title="飞书"
              subtitle={`主群=${data?.feishu?.chat_id_configured ? '已配置' : '未配置'} 重要群=${data?.feishu?.important_chat_id_configured ? '已配置' : '未配置'} 监督群=${data?.feishu?.supervision_chat_id_configured ? '已配置' : '未配置'}`}
              meta={data?.feishu?.control_plane_base_url || '未配置控制平面地址'}
            />
            <SimpleCard title="桥接" subtitle={(data?.bridge_health?.summary_lines || []).join('；') || '暂无桥接摘要'} meta={data?.bridge_health?.reported_at || ''} />
          </div>
        </HermesPanel>
      </div>
    </div>
  );
};

export const HermesSettingsPage = () => {
  const { data } = useQuery({ queryKey: ['hermes-settings'], queryFn: fetchHermesSettings });
  return (
    <div className="space-y-6">
      <RegistryHero eyebrow="设置" title="平台配置快照" description="这里只展示 Hermes / 控制面的有效配置摘要，不直接暴露敏感信息。" icon={Settings2} />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <HermesMetric label="运行模式" value={formatHermesLabel(data?.run_mode)} hint={data?.environment || '--'} />
        <HermesMetric label="执行模式" value={formatHermesLabel(data?.execution_mode)} hint={data?.execution_plane || '--'} />
        <HermesMetric label="行情模式" value={formatHermesLabel(data?.market_mode)} hint={data?.service?.host ? `${data?.service?.host}:${data?.service?.port}` : '--'} />
        <HermesMetric label="实盘开关" value={formatHermesLabel(data?.live_trade_enabled ? 'on' : 'off')} hint={data?.app_name || '--'} />
      </div>
      <div className="grid gap-5 xl:grid-cols-[1fr_1fr]">
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">路径</div>
          <div className="mt-4 space-y-3">
            <SimpleCard title="工作目录" subtitle={data?.workspace} />
            <SimpleCard title="存储根目录" subtitle={data?.storage_root} />
            <SimpleCard title="日志目录" subtitle={data?.logs_dir} />
          </div>
        </HermesPanel>
        <HermesPanel className="p-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[#a19381]">服务快照</div>
          <div className="mt-4 space-y-3">
            <SimpleCard title="主机 / 端口" subtitle={`${data?.service?.host || '--'}:${data?.service?.port || '--'}`} />
            <SimpleCard title="应用名称" subtitle={data?.app_name} meta={`环境=${data?.environment}`} />
            {(data?.hermes_model_policy?.providers || []).map((item: any) => (
              <SimpleCard
                key={item.provider_id}
                title={item.provider_name || item.provider_id}
                subtitle={`model=${item.model || '--'} slots=${(item.assigned_slots || []).join(', ') || '--'}`}
                meta={`route=${data?.hermes_model_policy?.routing_policy || '--'} · url=${item.base_url_configured ? '已配置' : '未配置'} · key=${item.credential_configured ? '已配置' : '未配置'}`}
              />
            ))}
          </div>
        </HermesPanel>
      </div>
    </div>
  );
};
