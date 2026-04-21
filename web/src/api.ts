import axios from 'axios';

const api = axios.create({
  baseURL: import.meta.env.DEV ? '' : '/',
});

export const fetchReadiness = async () => {
  const { data } = await api.get('/system/readiness');
  return data;
};

export const fetchAccountState = async () => {
  const { data } = await api.get('/system/account-state');
  return data;
};

export const fetchSupervisionBoard = async () => {
  const { data } = await api.get('/system/agents/supervision-board');
  return data;
};

export const fetchOperationsComponents = async () => {
  const { data } = await api.get('/system/operations/components');
  return data;
};

export const fetchOperationsHealthCheck = async () => {
  const { data } = await api.get('/system/operations/health-check');
  return data;
};

export const fetchClientBrief = async () => {
  const { data } = await api.get('/system/discussions/client-brief');
  return data;
};

export const fetchMissionControl = async () => {
  const { data } = await api.get('/system/dashboard/mission-control');
  return data;
};

export const fetchOpportunityFlow = async () => {
  const { data } = await api.get('/system/dashboard/opportunity-flow');
  return data;
};

export const fetchPortfolioEfficiency = async () => {
  const { data } = await api.get('/system/portfolio/efficiency');
  return data;
};

export const fetchParameterConsistency = async () => {
  const { data } = await api.get('/system/deployment/parameter-consistency');
  return data;
};

export const fetchParamProposals = async () => {
  const { data } = await api.get('/system/params/proposals');
  return data;
};

export const fetchAgentScores = async (scoreDate?: string) => {
  const { data } = await api.get('/system/agent-scores', {
    params: scoreDate ? { score_date: scoreDate } : undefined,
  });
  return data;
};

export const fetchFeishuBots = async () => {
  const { data } = await api.get('/system/feishu/bots');
  return data;
};

export const fetchSymbolDeskBrief = async (symbol: string) => {
  const { data } = await api.get(`/system/symbols/${symbol}/desk-brief`);
  return data;
};

export const fetchSymbolDetail = async (symbol: string) => fetchSymbolDeskBrief(symbol);

export const previewNaturalLanguageAdjustment = async (instruction: string) => {
  const { data } = await api.post('/system/adjustments/natural-language', {
    instruction,
    apply: false,
    preview: true,
    notify: false,
  });
  return data;
};

export const fetchAuditLogs = async (limit = 20) => {
  try {
    const { data } = await api.get(`/system/audit?limit=${limit}`);
    return data;
  } catch (err) {
    const { data } = await api.get(`/system/audits?limit=${limit}`);
    return data;
  }
};

export const updateConfig = async (updates: Record<string, any>) => {
  const { data } = await api.post('/system/config', updates);
  return data;
};

export const restartService = async (service: string) => {
  const { data } = await api.post(`/system/operations/service/restart?service=${service}`);
  return data;
};

export const fetchServiceStatus = async () => {
  const { data } = await api.get('/system/status/services');
  return data;
};

export default api;
