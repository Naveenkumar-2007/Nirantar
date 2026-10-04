"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ComponentProps } from "react";

/** System theme by default; a person's explicit choice (light/dark) is remembered per browser. */
export function ThemeProvider(props: ComponentProps<typeof NextThemesProvider>) {
  return <NextThemesProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange {...props} />;
}
