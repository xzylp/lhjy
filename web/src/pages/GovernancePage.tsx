import React from 'react';
import { Settings, Sliders, History } from 'lucide-react';

const GovernancePage = () => {
  return (
    <div className="p-8 space-y-8 animate-in fade-in duration-500">
      <div>
        <h1 className="text-3xl font-black text-slate-900 tracking-tight">治理与调参</h1>
        <p className="text-gray-500 mt-1">人机交互协作入口：支持自然语言调参、参数提案审查与历史版本回溯</p>
      </div>

      <div className="bg-slate-900 rounded-3xl p-8 text-white shadow-2xl">
        <div className="flex items-center space-x-3 mb-6">
          <Sliders className="w-5 h-5 text-blue-400" />
          <h3 className="font-bold tracking-widest uppercase text-sm">自然语言调参 (Intents)</h3>
        </div>
        <div className="relative">
          <input 
            type="text" 
            placeholder="例如：调高板块共振因子的权重至 0.8，并排除所有银行股..." 
            className="w-full bg-white/5 border border-white/10 rounded-2xl py-4 px-6 text-slate-200 placeholder:text-slate-600 focus:outline-none focus:ring-2 focus:ring-blue-500/50 transition"
          />
          <button className="absolute right-2 top-2 bottom-2 px-6 bg-blue-600 hover:bg-blue-500 rounded-xl font-bold text-xs uppercase tracking-widest transition">
            Preview
          </button>
        </div>
        <p className="mt-4 text-[10px] text-slate-500 font-medium uppercase tracking-widest text-center">
          Powered by Ashare Natural Language Engine
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        <div className="bg-white rounded-2xl p-6 border border-gray-100 shadow-sm">
           <h3 className="font-bold text-slate-800 mb-4 flex items-center">
             <History className="w-4 h-4 mr-2 text-gray-400" />
             最近参数变更
           </h3>
           <div className="text-sm text-gray-400 italic py-10 text-center">
             暂无近期变更记录
           </div>
        </div>
        <div className="bg-white rounded-2xl p-6 border border-gray-100 shadow-sm">
           <h3 className="font-bold text-slate-800 mb-4 flex items-center">
             <Settings className="w-4 h-4 mr-2 text-gray-400" />
             当前生效配置
           </h3>
           <div className="bg-gray-50 rounded-xl p-4 font-mono text-[10px] text-slate-500">
             {`{ "live_enable": true, "max_equity_ratio": 0.3 }`}
           </div>
        </div>
      </div>
    </div>
  );
};

export default GovernancePage;
