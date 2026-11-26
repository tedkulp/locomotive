import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap",
  {
    variants: {
      tone: {
        zinc: "bg-zinc-100 text-zinc-700 border-zinc-200",
        green: "bg-emerald-50 text-emerald-700 border-emerald-200",
        amber: "bg-amber-50 text-amber-800 border-amber-200",
        red: "bg-red-50 text-red-700 border-red-200",
        sky: "bg-sky-50 text-sky-700 border-sky-200",
        violet: "bg-violet-50 text-violet-700 border-violet-200",
      },
    },
    defaultVariants: { tone: "zinc" },
  },
);

export function Badge({
  className,
  tone,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
