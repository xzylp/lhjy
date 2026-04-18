import React from 'react';
import { useParams, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchSymbolDetail } from '../api';
import { ChevronLeft, Info, ShieldX, TrendingUp, Zap } from 'lucide-react';

const SymbolDetailPage = () => {
  const { symbol } = useParams();
  const { data, isLoading } = useQuery({
    queryKey: ['symbolDetail', symbol],
    queryFn: () => fetchSymbolDetail(symbol!),
    enabled: !!symbol,
  });

  if (isLoading) {
    return <div className="p-8 text-gray-400">Aggregating Symbol Intel...</div>;
  }

  const { research, precheck } = data || {};
  const isBlocked = precheck?.execution_gate?.status === 'BLOCKED';

  return (
    <div className="p-8 space-y-8 animate-in fade-in duration-500">
      <Link to="/dashboard/discussion" className="flex items-center text-slate-400 hover:text-slate-900 transition mb-4 group">
        <ChevronLeft className="w-4 h-4 mr-1 group-hover:-translate-x-1 transition-transform" />
        <span className="text-sm font-bold uppercase tracking-widest">返回列表</span>
      </Link>

      <div className="flex flex-col lg:flex-row justify-between items-start gap-8">
        {/* Left: Info Card */}
        <div className="flex-1 space-y-8 w-full">
          <div className="bg-white rounded-3xl p-8 shadow-sm border border-gray-100 relative overflow-hidden">
            <div className="absolute top-0 right-0 p-8">
               <div className={`px-4 py-1 rounded-full text-xs font-black uppercase ${isBlocked ? 'bg-red-50 text-red-600 border border-red-100' : 'bg-green-50 text-green-600 border border-green-100'}`}>
                 {isBlocked ? 'Execution Blocked' : 'Ready to Dispatch'}
               </div>
            </div>
            
            <div className="flex items-center space-x-4 mb-8">
              <div className="w-16 h-16 bg-slate-900 rounded-2xl flex items-center justify-center text-white text-2xl font-black italic">
                {symbol?.substring(0, 2)}
              </div>
              <div>
                <h1 className="text-4xl font-black text-slate-900 tracking-tighter uppercase">{research?.name || symbol}</h1>
                <p className="text-gray-400 font-mono font-medium tracking-widest">{symbol} • {research?.sector || 'A-Share Target'}</p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-8 pt-8 border-t border-gray-50">
               <div>
                 <div className="text-[10px] text-gray-400 uppercase font-black mb-2 tracking-widest">讨论定位</div>
                 <p className="text-slate-700 font-bold">{research?.discussion_role || 'Mainline Candidate'}</p>
               </div>
               <div>
                 <div className="text-[10px] text-gray-400 uppercase font-black mb-2 tracking-widest">核心逻辑</div>
                 <p className="text-slate-700 font-bold">{research?.main_logic || '尚未同步逻辑摘要'}</p>
               </div>
               <div>
                 <div className="text-[10px] text-gray-400 uppercase font-black mb-2 tracking-widest">动能等级</div>
                 <div className="flex space-x-1 mt-1">
                    {[1,2,3,4,5].map(i => <div key={i} className={`w-4 h-1.5 rounded-full ${i <= (research?.momentum_rank || 3) ? 'bg-blue-500' : 'bg-gray-100'}`}></div>)}
                 </div>
               </div>
            </div>
          </div>

          {/* Logic & Research Section */}
          <div className="bg-white rounded-3xl p-8 shadow-sm border border-gray-100">
             <h3 className="font-bold text-slate-800 mb-6 flex items-center">
               <Info className="w-5 h-5 mr-2 text-blue-500" />
               研究底稿与 Agent 归因
             </h3>
             <div className="prose prose-sm max-w-none text-slate-600 leading-relaxed">
                {research?.summary_text || '暂无详细研究摘要数据回显。'}
             </div>
          </div>
        </div>

        {/* Right: Execution & Risk Gate */}
        <div className="w-full lg:w-96 space-y-6">
           <div className="bg-slate-900 rounded-3xl p-8 text-white shadow-2xl">
              <h3 className="text-xs font-black uppercase tracking-widest text-slate-500 mb-6 flex items-center">
                <TrendingUp className="w-4 h-4 mr-2" />
                交易台复核决策
              </h3>
              
              <div className="space-y-6">
                 <div className="bg-white/5 rounded-2xl p-4 border border-white/10">
                    <p className="text-[10px] text-slate-500 uppercase font-bold mb-1">建议动作 (Advice)</p>
                    <p className="text-xl font-black text-blue-400 tracking-tight">{precheck?.trade_advice || 'HOLD / WAIT'}</p>
                 </div>

                 <div className="space-y-4">
                    <div className="flex justify-between text-sm">
                       <span className="text-slate-400">分时信号</span>
                       <span className="font-bold text-green-400">Strong Buy</span>
                    </div>
                    <div className="flex justify-between text-sm">
                       <span className="text-slate-400">价格触发</span>
                       <span className="font-mono text-slate-200">Above {precheck?.trigger_price || '--'}</span>
                    </div>
                    <div className="flex justify-between text-sm">
                       <span className="text-slate-400">止损位</span>
                       <span className="font-mono text-red-400">{precheck?.stop_loss || '--'}</span>
                    </div>
                 </div>

                 <button className="w-full py-4 bg-blue-600 hover:bg-blue-500 text-white rounded-2xl font-black uppercase tracking-widest transition shadow-xl shadow-blue-900/40">
                    Apply Execution
                 </button>
              </div>
           </div>

           {/* Risk Blockers */}
           <div className="bg-white rounded-3xl p-8 shadow-sm border border-red-50">
              <h3 className="text-xs font-black uppercase tracking-widest text-red-400 mb-6 flex items-center">
                <ShieldX className="w-4 h-4 mr-2" />
                风控电子围栏
              </h3>
              <div className="space-y-4">
                 {precheck?.execution_gate?.blockers?.length > 0 ? (
                   precheck.execution_gate.blockers.map((b: string, i: number) => (
                     <div key={i} className="flex items-start space-x-3 text-xs text-slate-600 bg-red-50/50 p-3 rounded-xl border border-red-100/50">
                        <div className="w-1.5 h-1.5 rounded-full bg-red-500 mt-1.5"></div>
                        <p className="leading-relaxed font-medium">{b}</p>
                     </div>
                   ))
                 ) : (
                   <p className="text-xs text-gray-400 italic">No active blockers for this target.</p>
                 )}
              </div>
           </div>
        </div>
      </div>
    </div>
  );
};

export default SymbolDetailPage;
