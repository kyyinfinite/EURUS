/** EURUS brand mark: a restrained geometric emblem — an eastward arrow
 *  (Eurus, the Greek god of the east wind) held in a hexagonal frame.
 *  Bronze stroke, copper accent, obsidian-safe. */

export function EurusMark({ size = 32, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <path
        d="M24 3.5 41.6 13.7v20.6L24 44.5 6.4 34.3V13.7L24 3.5Z"
        stroke="currentColor"
        strokeWidth="2"
        className="text-bronze/70"
      />
      <path
        d="M15 17.5h13M15 24h18M15 30.5h10"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        className="text-copper"
      />
      <path
        d="M33 27.5l4.5 3-4.5 3"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="text-bronze"
      />
    </svg>
  );
}

export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span
      className={`select-none font-sans text-base font-semibold tracking-[0.32em] text-ink ${className}`}
    >
      EURUS
    </span>
  );
}

export function BrandLockup({ collapsed = false }: { collapsed?: boolean }) {
  if (collapsed) {
    return <EurusMark size={30} />;
  }
  return (
    <span className="flex items-center gap-2.5">
      <EurusMark size={30} />
      <span className="flex flex-col leading-none">
        <Wordmark className="text-sm" />
        <span className="mt-1 text-3xs uppercase tracking-[0.22em] text-faint">Agent Workspace</span>
      </span>
    </span>
  );
}
