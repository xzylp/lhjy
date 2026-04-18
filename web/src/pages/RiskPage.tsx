import React from 'react';
import { ShieldCheck, Lock, AlertTriangle } from 'lucide-react';
import { MetricCard } from '../components/Common';

const RiskPage = () => {
  return (
    <div className="p-8 space-y-8 animate-in fade-in duration-500">
      <div>
        <h1 className="text-3xl font-black text-slate-900 tracking-tight">风控与电子围栏</h1>
        <p className="text-gray-500 mt-1">实时监控交易准入、持仓限制与合规性围栏触发状态</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <MetricCard title="单票上限" value="¥2.0w" subValue="当前限制" icon={Lock} />
        <MetricCard title="总仓位上限" value="30.0%" subValue="风险预算" icon={ShieldCheck} />
        <MetricCard title="阻断总数" value="0" subValue="今日累计" icon={AlertTriangle} />
      </div>

      <div className="bg-white rounded-2xl p-8 border border-gray-100 shadow-sm">
        <h3 className="font-bold text-slate-800 mb-4">生效规则清单</h3>
        <ul className="space-y-4">
           <li className="flex items-center justify-between p-4 bg-gray-50 rounded-xl">
             <div className="flex items-center space-x-3">
               <div className="w-2 h-2 rounded-full bg-green-500"></div>
               <span className="text-sm font-medium">涨停阻断 (禁止买入)</span>
             </div>
             <span className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">Active</span>
           </li>
           <li className="flex items-center justify-between p-4 bg-gray-50 rounded-xl">
             <div className="flex items-center space-x-3">
               <div className="w-2 h-2 rounded-full bg-green-500"></div>
               <span className="text-sm font-medium">跌停阻断 (禁止卖出)</span>
             </div>
             <span className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">Active</span>
           </li>
        </ul>
      </div>
    </div>
  );
};

export default RiskPage;
