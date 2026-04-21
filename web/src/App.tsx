import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Layout } from './components/Layout';
import { HermesLayout } from './components/hermes/HermesLayout';
import OverviewPage from './pages/OverviewPage';
import AgentsPage from './pages/AgentsPage';
import DiscussionPage from './pages/DiscussionPage';
import SymbolDetailPage from './pages/SymbolDetailPage';
import RiskPage from './pages/RiskPage';
import GovernancePage from './pages/GovernancePage';
import HermesChatPage from './pages/hermes/HermesChatPage';
import {
  HermesGatewayPage,
  HermesMemoryPage,
  HermesModelsPage,
  HermesOfficePage,
  HermesPersonaPage,
  HermesProfilesPage,
  HermesSchedulesPage,
  HermesSessionsPage,
  HermesSettingsPage,
  HermesSkillsPage,
  HermesToolsPage,
} from './pages/hermes/HermesControlPages';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <Routes>
          <Route path="/dashboard" element={<Navigate to="/dashboard/overview" replace />} />
          <Route path="/dashboard/overview" element={<Layout><OverviewPage /></Layout>} />
          <Route path="/dashboard/agents" element={<Layout><AgentsPage /></Layout>} />
          <Route path="/dashboard/discussion" element={<Layout><DiscussionPage /></Layout>} />
          <Route path="/dashboard/discussion/:symbol" element={<Layout><SymbolDetailPage /></Layout>} />
          <Route path="/dashboard/risk" element={<Layout><RiskPage /></Layout>} />
          <Route path="/dashboard/governance" element={<Layout><GovernancePage /></Layout>} />

          <Route path="/dashboard/hermes" element={<Navigate to="/dashboard/hermes/chat" replace />} />
          <Route path="/dashboard/hermes/chat" element={<HermesLayout><HermesChatPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/sessions" element={<HermesLayout><HermesSessionsPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/profiles" element={<HermesLayout><HermesProfilesPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/persona" element={<HermesLayout><HermesPersonaPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/office" element={<HermesLayout><HermesOfficePage /></HermesLayout>} />
          <Route path="/dashboard/hermes/models" element={<HermesLayout><HermesModelsPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/skills" element={<HermesLayout><HermesSkillsPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/memory" element={<HermesLayout><HermesMemoryPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/tools" element={<HermesLayout><HermesToolsPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/schedules" element={<HermesLayout><HermesSchedulesPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/gateway" element={<HermesLayout><HermesGatewayPage /></HermesLayout>} />
          <Route path="/dashboard/hermes/settings" element={<HermesLayout><HermesSettingsPage /></HermesLayout>} />

          <Route path="*" element={
            <div className="flex min-h-screen flex-col items-center justify-center text-slate-400">
              <h2 className="text-4xl font-black mb-2">404</h2>
              <p className="uppercase tracking-widest text-xs font-bold">Endpoint Not Found</p>
            </div>
          } />
        </Routes>
      </Router>
    </QueryClientProvider>
  );
}

export default App;
