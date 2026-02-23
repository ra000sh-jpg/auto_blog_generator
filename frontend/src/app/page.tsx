"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { DashboardRenewal } from "@/components/dashboard-renewal";
import { fetchOnboardingStatus } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let isMounted = true;
    async function checkStatus() {
      try {
        const response = await fetchOnboardingStatus();
        if (!isMounted) return;
        if (!response.completed) {
          router.replace("/onboarding");
        } else {
          setChecking(false);
        }
      } catch {
        if (!isMounted) return;
        setChecking(false);
      }
    }
    checkStatus();
    return () => {
      isMounted = false;
    };
  }, [router]);

  if (checking) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <p className="text-sm text-slate-500">대시보드 상태를 확인하는 중입니다...</p>
      </div>
    );
  }

  return <DashboardRenewal />;
}
