import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchClientBrief } from '../api';
import { Link } from 'react-router-dom';
import { ArrowUpRight, ShieldAlert, CheckCircle2 } from 'lucide-react';

const DiscussionPage = () => {
  const [activeTab, setActiveTab] = useState<'selected' | 'watchlist' | 'rejected'>('selected');
  const { data: brief, isPending } = useQuery({
    queryKey: ['clientBrief'],
    queryFn: fetchClientBrief,
    refetchInterval: 30000,
  });

  if (isPending && !brief) {
    return <div className="p-8 text-gray-400 animate-pulse font-medium">Loading Discussion Stream...</div>;
  }

  const items = brief?.candidates || [];
  const selected = items.filter((i: any) => i.disposition === 'SELECTED' || i.action === 'BUY' || i.pool_membership?.execution_pool);
  const watchlist = items.filter((i: any) => i.disposition === 'WATCHLIST' || i.pool_membership?.watchlist || (i.selection_score > 75 && i.disposition !== 'REJECTED'));
  const rejected = items.filter((i: any) => i.disposition === 'REJECTED');

  const displayData = activeTab === 'selected' ? selected : activeTab === 'watchlist' ? watchlist : rejected;

  return (
    <div className="p-8 space-y-8">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-black text-slate-900 tracking-tight">机会票讨论</h1>
          <p className="text-gray-500 mt-1">展示从全市场扫描到 Agent 内部辩论收敛后的候选清单 ({brief?.trade_date || 'Today'})</p>
          <p className="text-sm text-slate-500 mt-3">讨论面自动刷新中，每 30 秒同步一次，不再要求人工点刷新。</p>
        </div>
      </div>

      <div className="flex items-center space-x-1 bg-white p-1 rounded-xl shadow-sm border border-gray-100 w-fit">
        <TabButton active={activeTab === 'selected'} onClick={() => setActiveTab('selected')} label="核心推荐" count={selected.length} color="text-green-600" />
        <TabButton active={activeTab === 'watchlist'} onClick={() => setActiveTab('watchlist')} label="重点关注" count={watchlist.length} color="text-blue-600" />
        <TabButton active={activeTab === 'rejected'} onClick={() => setActiveTab('rejected')} label="已排除" count={rejected.length} color="text-gray-400" />
      </div>

      <div className="grid grid-cols-1 gap-4">
        {displayData.map((item: any, idx: number) => (
          <DiscussionItemCard key={item.symbol || idx} item={item} />
        ))}
        {displayData.length === 0 && (
          <div className="py-20 text-center bg-white rounded-2xl border border-dashed border-gray-200">
            <p className="text-gray-400 italic">当前分类下无数据。最近更新：{new Date().toLocaleTimeString()}</p>
          </div>
        )}
      </div>
    </div>
  );
};

const TabButton = ({ active, onClick, label, count, color }: any) => (
  <button 
    onClick={onClick}
    className={`
      px-6 py-2 rounded-lg text-sm font-bold transition-all duration-200 flex items-center space-x-2
      ${active ? 'bg-slate-900 text-white shadow-lg shadow-slate-200' : 'text-slate-400 hover:bg-gray-50'}
    `}
  >
    <span className={active ? 'text-white' : color}>{label}</span>
    <span className={`px-2 py-0.5 rounded-full text-[10px] ${active ? 'bg-slate-700' : 'bg-gray-100'}`}>{count}</span>
  </button>
);

const DiscussionItemCard = ({ item }: { item: any }) => {
  const isSelected = item.disposition === 'SELECTED' || item.action === 'BUY' || item.pool_membership?.execution_pool;
  
  return (
    <div className="bg-white rounded-2xl p-6 shadow-sm border border-gray-100 hover:shadow-md transition group">
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
        <div className="flex items-center space-x-4">
          <div className="w-12 h-12 bg-slate-50 rounded-xl flex items-center justify-center font-mono font-black text-slate-400 group-hover:bg-blue-50 group-hover:text-blue-500 transition uppercase">
            {item.symbol?.substring(0, 2) || '??'}
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h3 className="font-bold text-lg text-slate-800">{item.name || '未知标的'}</h3>
              <span className="font-mono text-sm text-gray-400">{item.symbol}</span>
            </div>
            <div className="flex items-center space-x-3 mt-1">
              <span className="text-[10px] bg-slate-100 px-2 py-0.5 rounded font-bold text-slate-500 uppercase">{item.resolved_sector || '通用板块'}</span>
              <div className="flex items-center text-[10px] text-blue-600 font-bold uppercase">
                <ArrowUpRight className="w-3 h-3 mr-0.5" />
                Score: {item.selection_score}
              </div>
            </div>
          </div>
        </div>

        <div className="flex-1 px-4 border-l border-gray-50">
          <div className="text-[10px] text-gray-400 uppercase font-bold mb-1 tracking-widest">推荐理由 / 状态</div>
          <p className="text-sm text-slate-600 line-clamp-2 leading-relaxed">
            {item.headline_reason || item.selected_reason || 'Agent 待命记录中，尚未产出具体归因。'}
          </p>
        </div>

        <div className="flex items-center space-x-4">
          <div className="text-right">
             <div className="flex items-center justify-end space-x-1">
                {isSelected ? <CheckCircle2 className="w-4 h-4 text-green-500" /> : <ShieldAlert className="w-4 h-4 text-amber-500" />}
                <span className={`text-xs font-bold ${isSelected ? 'text-green-600' : 'text-amber-600'}`}>{item.disposition || 'PENDING'}</span>
             </div>
             <p className="text-[10px] text-gray-400 mt-1 uppercase font-bold tracking-tighter">Risk: {item.risk_gate?.status || item.risk_gate || 'PASS'}</p>
          </div>
          <Link 
            to={`/dashboard/discussion/${item.symbol}`}
            className="p-3 bg-slate-50 text-slate-400 rounded-xl hover:bg-slate-900 hover:text-white transition shadow-sm"
          >
            <ArrowUpRight className="w-5 h-5" />
          </Link>
        </div>
      </div>
    </div>
  );
};

export default DiscussionPage;
