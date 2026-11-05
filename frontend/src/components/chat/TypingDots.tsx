export function TypingDots({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-[3px] ${className}`} aria-hidden="true">
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className="h-1 w-1 animate-dot-bounce rounded-full bg-current"
          style={{ animationDelay: `${index * 0.15}s` }}
        />
      ))}
    </span>
  );
}
