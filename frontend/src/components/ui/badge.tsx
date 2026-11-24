import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "./cn";

const badgeVariants = cva("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", {
  variants: {
    tone: {
      slate: "bg-slate-100 text-slate-700",
      indigo: "bg-indigo-100 text-indigo-700",
      green: "bg-emerald-100 text-emerald-700",
      amber: "bg-amber-100 text-amber-800",
      red: "bg-red-100 text-red-700",
    },
  },
  defaultVariants: { tone: "slate" },
});

type Props = HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

export function Badge({ className, tone, ...props }: Props) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
