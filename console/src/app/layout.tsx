import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import { THEME_BOOTSTRAP } from "@/lib/theme-script";
import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "ACVM 운영 콘솔", template: "%s · ACVM" },
  description: "AI 제휴 콘텐츠 자판기(ACVM) 운영 콘솔 — 명령, 승인, HOLD 검토, 대시보드",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f6f3" },
    { media: "(prefers-color-scheme: dark)", color: "#0d0d0d" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ko" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body className="min-h-dvh antialiased">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
