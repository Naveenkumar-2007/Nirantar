import Link from "next/link";
import { Wordmark } from "@/components/brand";

const ERRORS: Record<string, string> = {
  expired: "That sign-in took too long. Please try again.",
  signin_failed: "We couldn't complete your sign-in. Please try again.",
  session: "Your session ended. Please sign in again.",
};

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ returnTo?: string; error?: string }> }) {
  const { returnTo, error } = await searchParams;
  const rt = returnTo && returnTo.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/";
  const q = `returnTo=${encodeURIComponent(rt)}`;
  return (
    <div className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <Link href="/welcome" className="inline-flex"><Wordmark className="text-2xl" /></Link>
          <p className="mt-1 text-sm text-muted-foreground">Keep recurring revenue flowing — and prove what worked.</p>
        </div>
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm">
          {error && ERRORS[error] && (
            <p role="alert" className="mb-4 rounded-md bg-danger-soft px-3 py-2 text-sm text-danger">{ERRORS[error]}</p>
          )}
          <a href={`/auth/login?${q}`}
            className="block w-full rounded-md bg-primary px-4 py-2.5 text-center text-sm font-medium text-primary-foreground hover:opacity-90">
            Sign in
          </a>
          <a href={`/auth/login?${q}&signup=1`}
            className="mt-3 block w-full rounded-md border border-border px-4 py-2.5 text-center text-sm hover:bg-accent">
            Create an account
          </a>
          <p className="mt-4 text-center text-xs text-muted-foreground">
            Sign-in, password reset and two-step verification are handled by Nirantar&apos;s identity service.
            We never see your password.
          </p>
        </div>
      </div>
    </div>
  );
}
