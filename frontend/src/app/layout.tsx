import type { Metadata } from "next";
import Link from "next/link";
import UpdateButton from "@/components/update-button";
import "./globals.css";

export const metadata: Metadata = {
  title: "Auto Blog Dashboard",
  description: "FastAPI + Next.js 기반 자동 블로그 대시보드",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body className="antialiased" suppressHydrationWarning>
        <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_#ebf5ff_0%,_#f4f8f2_40%,_#fcfcf9_100%)] text-slate-900">
          <header className="sticky top-0 z-30 border-b border-slate-200/70 bg-white/90 backdrop-blur">
            <div className="mx-auto flex w-full max-w-6xl items-center justify-between px-4 py-3 sm:px-6">
              <div className="font-[family-name:var(--font-heading)] text-lg font-semibold tracking-tight">
                Auto Blog Control
              </div>
              <nav className="flex items-center gap-2 text-sm font-medium sm:gap-3">
                <UpdateButton />
                <Link
                  href="/"
                  className="rounded-full border border-slate-200 bg-white px-4 py-2 transition hover:border-slate-400"
                >
                  Dashboard
                </Link>
                <Link
                  href="/jobs"
                  className="rounded-full border border-slate-200 bg-white px-4 py-2 transition hover:border-slate-400"
                >
                  Jobs
                </Link>
                <Link
                  href="/settings"
                  className="rounded-full border border-slate-200 bg-white px-4 py-2 transition hover:border-slate-400"
                >
                  Settings
                </Link>
              </nav>
            </div>
          </header>
          <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
