import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Layout } from './components/Layout';
import OverviewPage from './pages/OverviewPage';
import AgentsPage from './pages/AgentsPage';
import DiscussionPage from './pages/DiscussionPage';
import SymbolDetailPage from './pages/SymbolDetailPage';
import RiskPage from './pages/RiskPage';
import GovernancePage from './pages/GovernancePage';

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
        <Layout>
          <Routes>
            <Route path="/dashboard" element={<Navigate to="/dashboard/overview" replace />} />
            <Route path="/dashboard/overview" element={<OverviewPage />} />
            <Route path="/dashboard/agents" element={<AgentsPage />} />
            <Route path="/dashboard/discussion" element={<DiscussionPage />} />
            <Route path="/dashboard/discussion/:symbol" element={<SymbolDetailPage />} />
            <Route path="/dashboard/risk" element={<RiskPage />} />
            <Route path="/dashboard/governance" element={<GovernancePage />} />
            <Route path="*" element={
              <div className="flex flex-col items-center justify-center h-full text-slate-400">
                <h2 className="text-4xl font-black mb-2">404</h2>
                <p className="uppercase tracking-widest text-xs font-bold">Endpoint Not Found</p>
              </div>
            } />
          </Routes>
        </Layout>
      </Router>
    </QueryClientProvider>
  );
}

export default App;
