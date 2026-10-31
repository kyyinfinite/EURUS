/** Form controls: Input, Textarea, Select (native), SearchInput. */

import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { Search, X } from "lucide-react";
import { useId } from "react";

export function Input({
  label,
  hint,
  className = "",
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { label?: string; hint?: string }) {
  const id = useId();
  return (
    <label htmlFor={id} className="block">
      {label && <span className="mb-1.5 block text-[13px] font-medium text-ink">{label}</span>}
      <input id={id} className={`w-full ${className}`} {...rest} />
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export function Textarea({
  label,
  hint,
  className = "",
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: string; hint?: string }) {
  const id = useId();
  return (
    <label htmlFor={id} className="block">
      {label && <span className="mb-1.5 block text-[13px] font-medium text-ink">{label}</span>}
      <textarea id={id} className={`w-full ${className}`} {...rest} />
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export function Select({
  label,
  children,
  className = "",
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement> & { label?: string }) {
  const id = useId();
  return (
    <label htmlFor={id} className="block">
      {label && <span className="mb-1.5 block text-[13px] font-medium text-ink">{label}</span>}
      <select id={id} className={`w-full appearance-none pr-8 ${className}`} {...rest}>
        {children}
      </select>
    </label>
  );
}

/** Rounded search field with clear affordance. */
export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
  ariaLabel,
  className = "",
  autoFocus,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  autoFocus?: boolean;
}) {
  return (
    <div className={`relative ${className}`}>
      <Search
        size={15}
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
      />
      <input
        type="search"
        value={value}
        autoFocus={autoFocus}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel ?? placeholder}
        className="w-full !rounded-full !py-2 !pl-9 !pr-8 !text-[13px]"
      />
      {value && (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => onChange("")}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-faint transition-colors hover:text-ink"
        >
          <X size={13} />
        </button>
      )}
    </div>
  );
}

/** Field wrapper for RHF-driven forms. */
export function FormField({
  label,
  error,
  hint,
  children,
}: {
  label: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <span className="mb-1.5 block text-[13px] font-medium text-ink">{label}</span>
      {children}
      {error ? (
        <p className="mt-1 text-xs font-medium text-danger">{error}</p>
      ) : hint ? (
        <p className="mt-1 text-xs text-muted">{hint}</p>
      ) : null}
    </div>
  );
}
