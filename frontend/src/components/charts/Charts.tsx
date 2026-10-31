/** Recharts wrappers in the EURUS light palette (copper brand, no neon). */

import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Bar,
  BarChart,
} from "recharts";

const AXIS_STYLE = {
  fontSize: 11,
  fill: "#81766D",
  fontFamily: "Inter, system-ui, sans-serif",
};

const TOOLTIP_STYLE = {
  borderRadius: 12,
  border: "1px solid #E8E1D9",
  boxShadow: "0 4px 16px rgba(51,42,36,0.10)",
  fontSize: 12,
  fontFamily: "Inter, system-ui, sans-serif",
  color: "#332A24",
  background: "#FFFFFF",
  padding: "8px 12px",
};

/** Area trend (run volume over time). */
export function ActivityArea({
  data,
  xKey,
  yKey,
  height = 240,
  color = "#995329",
  emptyLabel = "No activity recorded yet.",
}: {
  data: { label: string; value: number }[];
  xKey?: string;
  yKey?: string;
  height?: number;
  color?: string;
  emptyLabel?: string;
}) {
  if (data.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-dashed border-line text-[13px] text-muted"
        style={{ height }}
      >
        {emptyLabel}
      </div>
    );
  }
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
          <defs>
            <linearGradient id="eurusAreaFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.22} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#F0EBE4" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey={xKey ?? "label"}
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: "#E8E1D9" }}
            interval="preserveStartEnd"
            minTickGap={24}
          />
          <YAxis
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={false}
            allowDecimals={false}
            width={44}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            cursor={{ stroke: "#B0703F", strokeDasharray: "4 4" }}
          />
          <Area
            type="monotone"
            dataKey={yKey ?? "value"}
            stroke={color}
            strokeWidth={2.2}
            fill="url(#eurusAreaFill)"
            activeDot={{ r: 4, fill: color, stroke: "#FFFFFF", strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface DonutSlice {
  label: string;
  value: number;
  color: string;
}

/** Donut meter with a center label (e.g. run outcome ratio). */
export function DonutMeter({
  slices,
  centerValue,
  centerLabel,
  size = 148,
  centerTone = "ok",
}: {
  slices: DonutSlice[];
  centerValue: string;
  centerLabel: string;
  size?: number;
  centerTone?: "ok" | "warn" | "danger" | "copper";
}) {
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const centerColor =
    centerTone === "ok" ? "#278866" : centerTone === "warn" ? "#B45309" : centerTone === "danger" ? "#B3261E" : "#995329";
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <ResponsiveContainer>
        <PieChart>
          <Pie
            data={total > 0 ? slices : [{ label: "none", value: 1, color: "#F0EBE4" }]}
            dataKey="value"
            nameKey="label"
            innerRadius="72%"
            outerRadius="96%"
            paddingAngle={total > 0 ? 2 : 0}
            stroke="none"
            isAnimationActive={false}
          >
            {(total > 0 ? slices : [{ label: "none", value: 1, color: "#F0EBE4" }]).map(
              (slice, index) => (
                <Cell key={index} fill={slice.color} />
              ),
            )}
          </Pie>
          <Tooltip contentStyle={TOOLTIP_STYLE} />
        </PieChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-[26px] font-bold leading-none text-ink">{centerValue}</span>
        <span className="mt-1 text-xs font-semibold" style={{ color: centerColor }}>
          {centerLabel}
        </span>
      </div>
    </div>
  );
}

/** Vertical bar series (e.g. runs per day). */
export function BarSeries({
  data,
  height = 220,
  color = "#995329",
  emptyLabel = "No data yet.",
}: {
  data: { label: string; value: number }[];
  height?: number;
  color?: string;
  emptyLabel?: string;
}) {
  if (data.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-dashed border-line text-[13px] text-muted"
        style={{ height }}
      >
        {emptyLabel}
      </div>
    );
  }
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
          <CartesianGrid stroke="#F0EBE4" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="label"
            tick={AXIS_STYLE}
            tickLine={false}
            axisLine={{ stroke: "#E8E1D9" }}
            interval="preserveStartEnd"
            minTickGap={16}
          />
          <YAxis tick={AXIS_STYLE} tickLine={false} axisLine={false} allowDecimals={false} width={44} />
          <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "rgba(153,83,41,0.06)" }} />
          <Bar dataKey="value" fill={color} radius={[5, 5, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
