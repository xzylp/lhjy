import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const normalizeStatusTone = (status: string) => {
  const value = status.toLowerCase();
  if (['active', 'running', 'live', 'ok', 'ready', 'connected', 'clear', 'actioned'].includes(value)) {
    return {
      dot: 'bg-emerald-500 shadow-sm shadow-emerald-200',
      text: 'text-emerald-700',
      pill: 'bg-emerald-50 text-emerald-700 border border-emerald-100',
    };
  }
  if (['warning', 'dry-run', 'degraded', 'needs_work', 'pending', 'working', 'standby'].includes(value)) {
    return {
      dot: 'bg-amber-500 shadow-sm shadow-amber-200',
      text: 'text-amber-700',
      pill: 'bg-amber-50 text-amber-700 border border-amber-100',
    };
  }
  return {
    dot: 'bg-rose-500 shadow-sm shadow-rose-200',
    text: 'text-rose-700',
    pill: 'bg-rose-50 text-rose-700 border border-rose-100',
  };
};

export const StatusBadge = ({ status, className }: { status: string; className?: string }) => {
  const tone = normalizeStatusTone(status);

  return (
    <div className={cn("flex items-center space-x-2", className)}>
      <div className={cn("w-2 h-2 rounded-full", tone.dot)}></div>
      <span className={cn("text-xs font-bold uppercase tracking-tight", tone.text)}>{status}</span>
    </div>
  );
};

export const MetricCard = ({ title, value, subValue, icon: Icon }: { title: string; value: string | number; subValue?: string; icon?: any }) => (
  <div className="bg-white/90 p-5 rounded-2xl shadow-sm border border-stone-200">
    <div className="flex justify-between items-start mb-2">
      <span className="text-stone-500 text-xs font-semibold uppercase tracking-[0.2em]">{title}</span>
      {Icon && <Icon className="w-4 h-4 text-stone-400" />}
    </div>
    <div className="text-2xl font-bold text-slate-900">{value}</div>
    {subValue && <div className="text-xs text-stone-500 mt-1 leading-relaxed">{subValue}</div>}
  </div>
);

export const TonePill = ({ status, children }: { status: string; children?: string }) => {
  const tone = normalizeStatusTone(status);
  return (
    <span className={cn('inline-flex items-center rounded-full px-3 py-1 text-[10px] font-bold uppercase tracking-[0.22em]', tone.pill)}>
      {children || status}
    </span>
  );
};
