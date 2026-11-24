import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Database, LogOut } from "lucide-react";
import { useState, type FormEvent } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { ApiError, demoLogin, login, logout, register, type Me } from "../api";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card } from "./ui/card";
import { cn } from "./ui/cn";

interface Props { me: Me | null; authOpen: boolean; onAuthOpen: () => void; onAuthClose: () => void }

const linkClass = ({ isActive }: { isActive: boolean }) =>
  cn("whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium", isActive ? "bg-indigo-50 text-indigo-700" : "text-slate-600 hover:bg-slate-100");

export function Navbar({ me, authOpen, onAuthOpen, onAuthClose }: Props) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const signOut = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      queryClient.clear();
      queryClient.setQueryData(["me"], null);
      navigate("/");
    },
  });

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex min-h-14 max-w-7xl flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 md:h-14 md:flex-nowrap md:gap-4 md:py-0">
        <div className="flex shrink-0 items-center gap-2 font-semibold">
          <Database className="h-5 w-5 text-indigo-600" aria-hidden /> DocSQL
        </div>
        <nav
          className={cn(
            "flex gap-1",
            me && "order-3 w-full border-t border-slate-100 pt-2 md:order-none md:w-auto md:border-t-0 md:pt-0",
          )}
          aria-label="Main"
        >
          <NavLink to="/" end className={linkClass}>Workspace</NavLink>
          {me && <NavLink to="/history" className={linkClass}>History</NavLink>}
          {me?.user.role === "ADMIN" && <NavLink to="/metrics" className={linkClass}>Metrics</NavLink>}
        </nav>
        <div className="ml-auto flex shrink-0 items-center gap-2 text-sm sm:gap-3">
          {me ? (
            <>
              {me.quota_limit !== null && (
                <Badge tone={me.quota_remaining === 0 ? "red" : "indigo"}>
                  <span className="whitespace-nowrap">
                    {me.quota_remaining}/{me.quota_limit}
                    <span className="sm:hidden">&nbsp;left</span>
                    <span className="hidden sm:inline">&nbsp;queries left today</span>
                  </span>
                </Badge>
              )}
              <span className="hidden text-slate-600 lg:inline">{me.user.full_name}</span>
              <Badge><span className="whitespace-nowrap">{me.user.role}</span></Badge>
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0 whitespace-nowrap px-2 sm:px-3"
                aria-label="Sign out"
                onClick={() => signOut.mutate()}
                disabled={signOut.isPending}
              >
                <LogOut className="h-4 w-4" aria-hidden />
                <span className="hidden sm:inline">Sign out</span>
              </Button>
            </>
          ) : (
            <Button size="sm" className="shrink-0" onClick={onAuthOpen}>Sign in</Button>
          )}
        </div>
      </div>
      {authOpen && !me && <AuthModal onClose={onAuthClose} />}
    </header>
  );
}

function AuthModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [form, setForm] = useState({ email: "", password: "", full_name: "" });
  const [error, setError] = useState<string | null>(null);

  const auth = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSuccess: async () => {
      setError(null);
      await queryClient.invalidateQueries();
      onClose();
    },
    onError: (e: unknown) => setError(e instanceof ApiError ? e.message : "Something went wrong"),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    auth.mutate(() =>
      mode === "login" ? login(form.email, form.password) : register(form.email, form.password, form.full_name),
    );
  };
  const field = (key: keyof typeof form) => ({
    value: form[key],
    onChange: (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value }),
    className: "h-9 w-full rounded-md border border-slate-300 px-3 text-sm",
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" role="dialog" aria-modal="true" aria-label="Sign in">
      <Card className="w-full max-w-sm space-y-4 p-6">
        <div className="flex gap-2">
          {(["login", "register"] as const).map((m) => (
            <button key={m} type="button" onClick={() => setMode(m)}
              className={cn("flex-1 border-b-2 pb-2 text-sm font-medium", mode === m ? "border-indigo-600 text-indigo-700" : "border-transparent text-slate-500")}>
              {m === "login" ? "Sign in" : "Create account"}
            </button>
          ))}
        </div>
        <form onSubmit={submit} className="space-y-3">
          {mode === "register" && <input aria-label="Full name" placeholder="Full name" required {...field("full_name")} />}
          <input aria-label="Email" type="email" placeholder="Email" required autoComplete="email" {...field("email")} />
          <input aria-label="Password" type="password" placeholder="Password (min 8 characters)" required minLength={8}
            autoComplete={mode === "login" ? "current-password" : "new-password"} {...field("password")} />
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <Button type="submit" className="w-full" disabled={auth.isPending}>
            {mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>
        <div className="space-y-2 border-t border-slate-200 pt-4">
          <p className="text-xs text-slate-500">No account? Try a demo user (no password needed).</p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" className="flex-1" onClick={() => auth.mutate(() => demoLogin("ANALYST"))}>Demo analyst</Button>
            <Button variant="outline" size="sm" className="flex-1" onClick={() => auth.mutate(() => demoLogin("ADMIN"))}>Demo admin</Button>
          </div>
        </div>
        <Button variant="ghost" size="sm" className="w-full" onClick={onClose}>Close</Button>
      </Card>
    </div>
  );
}
