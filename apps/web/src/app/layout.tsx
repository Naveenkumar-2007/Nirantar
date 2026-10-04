import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Nirantar", template: "%s · Nirantar" },
  description: "Keep recurring revenue flowing — and prove what worked.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f6f3" },
    { media: "(prefers-color-scheme: dark)", color: "#0f0f0e" },
  ],
};

/** Shell only: theme, tooltips, toasts. The console (sidebar, account) lives in app/(console)/layout.tsx; sign-in and
 * onboarding are full-page screens outside it. `suppressHydrationWarning`: next-themes sets the class before React
 * hydrates (no flash of the wrong theme). */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`} suppressHydrationWarning>
      <body className="min-h-screen">
        <ThemeProvider>
          <TooltipProvider delayDuration={200}>
            {children}
            <Toaster richColors closeButton />
          </TooltipProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
