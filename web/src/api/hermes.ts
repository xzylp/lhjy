import { default as api } from '../api';

export const fetchHermesWorkspace = async () => {
  const { data } = await api.get('/system/hermes/workspace');
  return data;
};

export const fetchHermesSessions = async () => {
  const { data } = await api.get('/system/hermes/sessions');
  return data;
};

export const createHermesSession = async (payload: { title?: string; profile_id?: string; model_id?: string }) => {
  const { data } = await api.post('/system/hermes/sessions', payload);
  return data;
};

export const fetchHermesSession = async (sessionId: string) => {
  const { data } = await api.get(`/system/hermes/sessions/${sessionId}`);
  return data;
};

export const appendHermesMessage = async (payload: { session_id: string; role?: string; content: string }) => {
  const { data } = await api.post('/system/hermes/messages', payload);
  return data;
};

export const previewHermesCommand = async (payload: { command: string; session_id?: string }) => {
  const { data } = await api.post('/system/hermes/command/preview', payload);
  return data;
};

export const executeHermesCommand = async (payload: { command: string; session_id?: string }) => {
  const { data } = await api.post('/system/hermes/command/execute', payload);
  return data;
};

export const askHermesNaturalLanguage = async (payload: { question: string; trade_date?: string; source?: string; bot_role?: string }) => {
  const { data } = await api.post('/system/feishu/ask', {
    question: payload.question,
    trade_date: payload.trade_date,
    source: payload.source || 'hermes_chat',
    bot_role: payload.bot_role || 'main',
    notify: false,
    force: false,
  });
  return data;
};

export const fetchHermesProfiles = async () => {
  const { data } = await api.get('/system/hermes/profiles');
  return data;
};

export const fetchHermesModels = async () => {
  const { data } = await api.get('/system/hermes/models');
  return data;
};

export const fetchHermesSkills = async () => {
  const { data } = await api.get('/system/hermes/skills');
  return data;
};

export const fetchHermesMemory = async () => {
  const { data } = await api.get('/system/hermes/memory');
  return data;
};

export const fetchHermesTools = async () => {
  const { data } = await api.get('/system/hermes/tools');
  return data;
};

export const fetchHermesSchedules = async () => {
  const { data } = await api.get('/system/hermes/schedules');
  return data;
};

export const fetchHermesGateway = async () => {
  const { data } = await api.get('/system/hermes/gateway');
  return data;
};

export const fetchHermesSettings = async () => {
  const { data } = await api.get('/system/hermes/settings');
  return data;
};

export const fetchHermesActivity = async () => {
  const { data } = await api.get('/system/hermes/activity');
  return data;
};

export const fetchHermesAshareOverview = async () => {
  const { data } = await api.get('/system/hermes/integrations/ashare/overview');
  return data;
};
