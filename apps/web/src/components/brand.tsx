/** The Nirantar mark: one unbroken loop (निरंतर, "continuous") — revenue that keeps flowing. Inherits currentColor. */
export function BrandMark({ className = "size-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true" fill="none">
      <path d="M8.5 11.2c-3 0-5 2.2-5 4.8s2 4.8 5 4.8c4.6 0 10.4-9.6 15-9.6 3 0 5 2.2 5 4.8s-2 4.8-5 4.8c-4.6 0-10.4-9.6-15-9.6Z"
        stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 font-semibold tracking-tight ${className}`}>
      <BrandMark className="size-6 text-primary" />
      Nirantar
    </span>
  );
}
