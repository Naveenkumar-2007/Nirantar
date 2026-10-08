"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { KeyRound, Monitor, Moon, Search, Settings, ShieldCheck, Sun } from "lucide-react";
import {
  CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator, CommandShortcut,
} from "@/components/ui/command";
import { NAV } from "@/components/app-sidebar";

/** Sign-in actions are route handlers that redirect to the identity service: a full page load, not a client route. */
function leave(path: string) {
  window.location.assign(new URL(path, window.location.origin));
}

/** ⌘K / Ctrl+K: jump to any page or run a common action from the keyboard. Navigation only — every action that
 * changes data still goes through its page's confirmation. */
export function CommandMenu() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const { setTheme } = useTheme();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (href: string) => {
    setOpen(false);
    router.push(href);
  };

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}
        className="ml-auto hidden h-8 items-center gap-2 rounded-md border border-border bg-card px-2.5 text-sm text-muted-foreground hover:bg-accent sm:inline-flex"
        aria-label="Search pages and actions">
        <Search className="size-3.5" aria-hidden />
        <span>Search…</span>
        <kbd className="ml-4 rounded border border-border bg-muted px-1.5 font-mono text-[10px]">Ctrl K</kbd>
      </button>
      <CommandDialog open={open} onOpenChange={setOpen} title="Search Nirantar" description="Jump to a page or run an action">
        <CommandInput placeholder="Search pages and actions…" />
        <CommandList>
          <CommandEmpty>Nothing matches.</CommandEmpty>
          {NAV.map((group) => (
            <CommandGroup key={group.label} heading={group.label}>
              {group.items.map(({ href, label, icon: Icon }) => (
                <CommandItem key={href} value={`${group.label} ${label}`} onSelect={() => go(href)}>
                  <Icon className="size-4" />
                  {label}
                </CommandItem>
              ))}
            </CommandGroup>
          ))}
          <CommandSeparator />
          <CommandGroup heading="Account">
            <CommandItem value="settings" onSelect={() => go("/settings")}><Settings className="size-4" />Settings</CommandItem>
            <CommandItem value="two-step verification mfa security" onSelect={() => { setOpen(false); leave("/auth/login?action=mfa&returnTo=/settings"); }}>
              <ShieldCheck className="size-4" />Turn on two-step verification
            </CommandItem>
            <CommandItem value="change password" onSelect={() => { setOpen(false); leave("/auth/login?action=password&returnTo=/settings"); }}>
              <KeyRound className="size-4" />Change password
            </CommandItem>
          </CommandGroup>
          <CommandGroup heading="Theme">
            <CommandItem value="theme light" onSelect={() => { setTheme("light"); setOpen(false); }}><Sun className="size-4" />Light<CommandShortcut>theme</CommandShortcut></CommandItem>
            <CommandItem value="theme dark" onSelect={() => { setTheme("dark"); setOpen(false); }}><Moon className="size-4" />Dark<CommandShortcut>theme</CommandShortcut></CommandItem>
            <CommandItem value="theme system" onSelect={() => { setTheme("system"); setOpen(false); }}><Monitor className="size-4" />System<CommandShortcut>theme</CommandShortcut></CommandItem>
          </CommandGroup>
        </CommandList>
      </CommandDialog>
    </>
  );
}
