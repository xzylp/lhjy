import axios from 'axios';

const api = axios.create({
  baseURL: import.meta.env.DEV ? '' : '/', // 在生产环境下，API 与前端同源
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

export const fetchSymbolDetail = async (symbol: string) => {
  const [research, precheck] = await Promise.all([
    api.get(`/system/research/summary?symbol=${symbol}`),
    api.get(`/system/discussions/execution-precheck?symbol=${symbol}`),
  ]);
  return {
    research: research.data,
    precheck: precheck.data,
  };
};

export const fetchAuditLogs = async (limit = 20) => {
  // 同时兼容 /system/audit 和 /system/audits
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
