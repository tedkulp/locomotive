import { History, Play, Radio, Settings, TrainFront, CalendarClock } from "lucide-react";
import { NavLink, Outlet } from "react-router";

import { RecentRuns } from "@/components/RecentRuns";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Launch", icon: Play, end: true },
  { to: "/runs/current", label: "Current Run", icon: Radio },
  { to: "/history", label: "Run history", icon: History },
  { to: "/schedules", label: "Schedules", icon: CalendarClock },
  { to: "/status", label: "Status", icon: Settings },
];

export function Layout() {
  return (
    <div className="flex h-screen">
      <aside className="w-56 shrink-0 border-r border-border bg-card flex flex-col">
        <div className="px-4 h-14 flex items-center gap-2 font-semibold tracking-tight">
          <TrainFront className="size-5" /> locomotive
        </div>
        <nav className="px-2 space-y-0.5 text-sm">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2",
                  isActive ? "bg-muted font-medium" : "text-muted-foreground hover:bg-muted/60",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="flex-1 min-w-0 overflow-auto">
        <Outlet />
      </main>
      <RecentRuns />
    </div>
  );
}
