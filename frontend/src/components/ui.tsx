import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Health, MeetingStatus, Provider } from "../api";
import { AlertIcon, CheckIcon, ChevronDownIcon, CloseIcon, CopyIcon } from "./icons";

// ── Switch & rows ──────────────────────────────────────────────────────

export function Switch({
  checked,
  onChange,
  disabled,
  label,
  id,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label?: string;
  id?: string;
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`group relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors duration-200 disabled:cursor-not-allowed ${
        checked ? "bg-forest" : "bg-[var(--switch-off)]"
      }`}
    >
      <span
        className={`absolute left-[2px] h-5 w-5 rounded-full bg-white shadow-[0_1px_3px_rgba(22,40,30,0.22),0_1px_1px_rgba(22,40,30,0.08)] transition-[transform,width] duration-300 ease-spring group-active:w-[23px] ${
          checked ? "translate-x-5 group-active:translate-x-[17px]" : "translate-x-0"
        }`}
      />
    </button>
  );
}

/** A setting: icon well, label and hint on the left, its control on the right. */
export function ControlRow({
  icon,
  label,
  hint,
  htmlFor,
  disabled,
  wrap,
  className = "",
  children,
}: {
  icon?: ReactNode;
  label: ReactNode;
  hint?: ReactNode;
  htmlFor?: string;
  disabled?: boolean;
  wrap?: boolean;
  className?: string;
  children?: ReactNode;
}) {
  const text = (
    <>
      <span className="block text-label font-medium leading-5 text-ink">{label}</span>
      {hint && <span className="mt-0.5 block text-hint leading-[1.45] text-ink-soft">{hint}</span>}
    </>
  );
  return (
    <div
      className={`flex items-center justify-between gap-x-5 gap-y-2 py-3.5 transition-opacity ${wrap ? "flex-wrap" : ""} ${
        disabled ? "opacity-50" : ""
      } ${className}`}
    >
      <div className="flex min-w-0 items-center gap-3.5">
        {icon && <span className="well">{icon}</span>}
        {htmlFor ? (
          <label htmlFor={htmlFor} className={`min-w-0 ${disabled ? "" : "cursor-pointer"}`}>
            {text}
          </label>
        ) : (
          <div className="min-w-0">{text}</div>
        )}
      </div>
      {children}
    </div>
  );
}

export function SwitchRow({
  icon,
  label,
  hint,
  checked,
  disabled,
  onChange,
  className,
}: {
  icon?: ReactNode;
  label: string;
  hint?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
  className?: string;
}) {
  const id = useId();
  return (
    <ControlRow icon={icon} label={label} hint={hint} htmlFor={id} disabled={disabled} className={className}>
      <Switch id={id} checked={checked} onChange={onChange} disabled={disabled} />
    </ControlRow>
  );
}

// ── Select: a styled, animated listbox ─────────────────────────────────

export interface Option {
  value: string;
  label: string;
  hint?: string;
}

export function Select({
  value,
  onChange,
  options,
  disabled,
  className = "",
  label,
  size = "md",
}: {
  value: string;
  onChange: (v: string) => void;
  options: Option[];
  disabled?: boolean;
  className?: string;
  label: string;
  size?: "sm" | "md";
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const typed = useRef({ text: "", at: 0 });
  const id = useId();
  const current = options.findIndex((o) => o.value === value);

  const place = useCallback(() => {
    if (trigger.current) setRect(trigger.current.getBoundingClientRect());
  }, []);
  const show = () => {
    place();
    setActive(Math.max(0, current));
    setOpen(true);
  };
  const close = useCallback((refocus = true) => {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  }, []);
  const choose = (i: number) => {
    const o = options[i];
    if (o && o.value !== value) onChange(o.value);
    close();
  };

  useEffect(() => {
    if (!open) return;
    list.current?.focus({ preventScroll: true });
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!list.current?.contains(t) && !trigger.current?.contains(t)) close(false);
    };
    const onScroll = (e: Event) => {
      if (!list.current?.contains(e.target as Node)) place();
    };
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", place);
    };
  }, [open, place, close]);

  useEffect(() => {
    if (open) list.current?.querySelector(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const onListKey = (e: ReactKeyboardEvent) => {
    const last = options.length - 1;
    if (e.key === "ArrowDown") setActive((a) => Math.min(last, a + 1));
    else if (e.key === "ArrowUp") setActive((a) => Math.max(0, a - 1));
    else if (e.key === "Home") setActive(0);
    else if (e.key === "End") setActive(last);
    else if (e.key === "Enter" || e.key === " ") choose(active);
    else if (e.key === "Escape") close();
    else if (e.key === "Tab") return close(false);
    else if (e.key.length === 1) {
      // Type-ahead, like a native select.
      const now = Date.now();
      typed.current.text = (now - typed.current.at < 700 ? typed.current.text : "") + e.key.toLowerCase();
      typed.current.at = now;
      const hit = options.findIndex((o) => o.label.toLowerCase().startsWith(typed.current.text));
      if (hit >= 0) setActive(hit);
    } else return;
    e.preventDefault();
  };

  let style: CSSProperties = {};
  let up = false;
  if (rect) {
    const width = Math.max(rect.width, 200);
    const below = window.innerHeight - rect.bottom - 12;
    up = below < Math.min(300, options.length * 36 + 12) && rect.top > below;
    style = {
      left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
      width,
      maxHeight: Math.min(300, (up ? rect.top : below) - 8),
      transformOrigin: up ? "bottom" : "top",
      ...(up ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
    };
  }

  return (
    <>
      <button
        ref={trigger}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={label}
        onClick={() => (open ? close(false) : show())}
        onKeyDown={(e) => {
          if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
            e.preventDefault();
            show();
          }
        }}
        className={`field flex min-w-0 items-center justify-between gap-2 text-left ${size === "sm" ? "py-[5px] text-ui" : ""} ${className}`}
      >
        <span className="min-w-0 truncate">{options[current]?.label ?? ""}</span>
        <ChevronDownIcon size={16} className={`shrink-0 text-ink-faint transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </button>
      {open &&
        rect &&
        createPortal(
          <ul
            ref={list}
            id={id}
            role="listbox"
            tabIndex={-1}
            aria-label={label}
            aria-activedescendant={`${id}-${active}`}
            onKeyDown={onListKey}
            style={style}
            className="fixed z-[60] animate-pop overflow-y-auto rounded-md border border-line bg-surface p-1 shadow-lg outline-none"
          >
            {options.map((o, i) => {
              const selected = o.value === value;
              return (
                <li
                  key={o.value}
                  id={`${id}-${i}`}
                  data-i={i}
                  role="option"
                  aria-selected={selected}
                  onPointerMove={() => active !== i && setActive(i)}
                  onClick={() => choose(i)}
                  className={`flex cursor-pointer items-start gap-2 rounded-sm px-2.5 py-2 text-ui leading-5 transition-colors duration-100 ${
                    i === active ? "bg-forest/[.07]" : ""
                  }`}
                >
                  <CheckIcon size={16} className={`mt-0.5 shrink-0 text-forest ${selected ? "" : "invisible"}`} />
                  <span className="min-w-0 flex-1">
                    <span className={`block truncate ${selected ? "font-medium" : ""}`}>{o.label}</span>
                    {o.hint && <span className="block truncate text-meta text-ink-soft">{o.hint}</span>}
                  </span>
                </li>
              );
            })}
          </ul>,
          document.body,
        )}
    </>
  );
}

export function ProviderSelect({
  value,
  onChange,
  health,
  disabled,
  className = "w-52",
}: {
  value: Provider;
  onChange: (p: Provider) => void;
  health: Health | null;
  disabled?: boolean;
  className?: string;
}) {
  const primary = health?.summary.label;
  const fallback = health?.fallback?.label;
  return (
    <Select
      value={value}
      disabled={disabled}
      onChange={(v) => onChange(v as Provider)}
      label="Summary model"
      size="sm"
      className={className}
      options={[
        { value: "auto", label: primary ? `Default · ${primary}` : "Default provider", hint: fallback ? `Falls back to ${fallback}` : undefined },
        ...(health?.providers ?? []).map((p) => ({ value: p.id, label: p.label, hint: p.kind === "local" ? "Runs locally" : "Cloud" })),
      ]}
    />
  );
}

export function TemplateSelect({
  value,
  onChange,
  health,
  disabled,
  className = "w-44",
}: {
  value: string;
  onChange: (t: string) => void;
  health: Health | null;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <Select
      value={value}
      disabled={disabled}
      onChange={onChange}
      label="Summary template"
      size="sm"
      className={className}
      options={[{ value: "", label: "Default template" }, ...(health?.templates ?? []).map((t) => ({ value: t.id, label: t.name }))]}
    />
  );
}

// ── Segmented control with a sliding thumb ─────────────────────────────

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  kind = "radio",
  label,
  size = "md",
}: {
  value: T;
  options: { id: T; label: ReactNode; icon?: ReactNode }[];
  onChange: (v: T) => void;
  kind?: "radio" | "tab";
  label?: string;
  size?: "sm" | "md";
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState<{ left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const box = wrap.current;
    if (!box) return;
    const measure = () => {
      const el = box.querySelector<HTMLElement>(`[data-id="${value}"]`);
      if (el) setThumb({ left: el.offsetLeft, width: el.offsetWidth });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(box);
    return () => ro.disconnect();
  }, [value, options.length]);

  const move = (e: ReactKeyboardEvent) => {
    const i = options.findIndex((o) => o.id === value);
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
    if (next === null) return;
    e.preventDefault();
    const o = options[(next + options.length) % options.length];
    onChange(o.id);
    wrap.current?.querySelector<HTMLElement>(`[data-id="${o.id}"]`)?.focus();
  };

  return (
    <div
      ref={wrap}
      role={kind === "tab" ? "tablist" : "radiogroup"}
      aria-label={label}
      onKeyDown={move}
      className="relative inline-flex max-w-full rounded-md border border-line bg-surface-subtle/80 p-[3px]"
    >
      {thumb && (
        <span
          aria-hidden="true"
          className="absolute bottom-[3px] top-[3px] rounded-sm bg-surface shadow-sm ring-1 ring-line transition-[left,width] duration-300 ease-out"
          style={{ left: thumb.left, width: thumb.width }}
        />
      )}
      {options.map((o) => {
        const on = o.id === value;
        return (
          <button
            key={o.id}
            data-id={o.id}
            type="button"
            role={kind}
            tabIndex={on ? 0 : -1}
            aria-checked={kind === "radio" ? on : undefined}
            aria-selected={kind === "tab" ? on : undefined}
            onClick={() => onChange(o.id)}
            className={`relative z-10 inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm px-3 font-medium transition-colors duration-200 ${
              size === "sm" ? "h-7 text-hint" : "h-8 text-ui"
            } ${on ? "text-ink" : "text-ink-soft hover:text-ink"}`}
          >
            {o.icon && <span className={`transition-colors ${on ? "text-forest" : ""}`}>{o.icon}</span>}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/** Animates open and closed to its content's height; hidden content leaves the tab order. */
export function Collapse({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <div
      className={`grid transition-[grid-template-rows,opacity,visibility] duration-300 ease-out ${
        open ? "visible grid-rows-[1fr] opacity-100" : "invisible grid-rows-[0fr] opacity-0"
      }`}
    >
      <div className="min-h-0 overflow-hidden">{children}</div>
    </div>
  );
}

// ── Small pieces ───────────────────────────────────────────────────────

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={`h-3.5 w-3.5 animate-spin ${className}`} aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.2" strokeWidth="2.5" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden="true" className={`skeleton ${className}`} />;
}

export function Pulse({ tone = "trail" }: { tone?: "trail" | "sky" | "forest" }) {
  const bg = { trail: "bg-trail", sky: "bg-sky", forest: "bg-forest" }[tone];
  return (
    <span className="relative flex h-2 w-2 shrink-0">
      <span className={`absolute inset-0 animate-ring-pulse rounded-full ${bg}`} />
      <span className={`relative h-2 w-2 rounded-full ${bg}`} />
    </span>
  );
}

const STATUS_CHIP: Partial<Record<MeetingStatus, { label: string; cls: string; dot: string; live?: boolean }>> = {
  recording: { label: "Recording", cls: "bg-trail-soft text-trail-deep", dot: "bg-trail", live: true },
  queued: { label: "Queued", cls: "bg-surface-subtle text-ink-soft", dot: "bg-ink-faint" },
  ready_transcribe: { label: "Needs you", cls: "bg-sun-soft text-sun-deep", dot: "bg-sun" },
  ready_summarize: { label: "Needs you", cls: "bg-sun-soft text-sun-deep", dot: "bg-sun" },
  waiting_confirm: { label: "Needs memory", cls: "bg-sun-soft text-sun-deep", dot: "bg-sun" },
  transcribing: { label: "Transcribing", cls: "bg-sky-soft text-sky-deep", dot: "bg-sky", live: true },
  summarizing: { label: "Summarizing", cls: "bg-sky-soft text-sky-deep", dot: "bg-sky", live: true },
  error: { label: "Failed", cls: "bg-trail-soft text-trail-deep", dot: "bg-trail" },
};

export function StatusChip({ status }: { status: MeetingStatus }) {
  const s = STATUS_CHIP[status];
  if (!s) return null;
  return (
    <span className={`chip ${s.cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot} ${s.live ? "animate-soft-pulse" : ""}`} />
      {s.label}
    </span>
  );
}

/** Colored by role (You = sky, Them = berry); shows the person's name if you've set one. */
export function SpeakerChip({ speaker, name }: { speaker: string; name?: string }) {
  const cls =
    speaker === "You" ? "bg-sky-soft text-sky-deep" : speaker === "Them" ? "bg-berry-soft text-berry-deep" : "bg-surface-subtle text-ink-soft";
  return <span className={`chip max-w-[8rem] truncate ${cls}`}>{name || speaker}</span>;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigator.clipboard.writeText(text).then(() => setCopied(true))}>
      {copied ? <CheckIcon size={16} className="animate-check-pop text-forest" /> : <CopyIcon size={16} />}
      {copied ? "Copied" : label}
    </button>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-8 flex animate-enter flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="mt-1.5 text-body leading-relaxed text-ink-soft">{subtitle}</p>}
      </div>
      {actions}
    </header>
  );
}

// ── Tooltips ───────────────────────────────────────────────────────────

/**
 * One shared tooltip for every element with a data-tip attribute, shown on hover (after a short
 * delay, instantly when moving between tipped elements) and on keyboard focus.
 */
export function TooltipLayer() {
  const [tip, setTip] = useState<{ text: string; x: number; y: number; below: boolean } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let timer = 0;
    let current: HTMLElement | null = null;
    let visible = false;
    const hide = () => {
      clearTimeout(timer);
      current = null;
      visible = false;
      setTip(null);
    };
    const show = (el: HTMLElement, delay: number) => {
      clearTimeout(timer);
      current = el;
      timer = window.setTimeout(() => {
        if (!el.isConnected || !el.dataset.tip) return;
        const r = el.getBoundingClientRect();
        const below = r.top < 56;
        visible = true;
        setTip({ text: el.dataset.tip, x: r.left + r.width / 2, y: below ? r.bottom + 8 : r.top - 8, below });
      }, delay);
    };
    const over = (e: PointerEvent) => {
      if (e.pointerType === "touch") return;
      const el = (e.target as Element).closest?.<HTMLElement>("[data-tip]") ?? null;
      if (el === current) return;
      if (el) show(el, visible ? 60 : 480);
      else if (current) hide();
    };
    const focusIn = (e: FocusEvent) => {
      const el = e.target as HTMLElement;
      if (el.dataset?.tip && el.matches(":focus-visible")) show(el, 200);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && hide();
    document.addEventListener("pointerover", over);
    document.addEventListener("focusin", focusIn);
    document.addEventListener("focusout", hide);
    document.addEventListener("pointerdown", hide, true);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("blur", hide);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("pointerover", over);
      document.removeEventListener("focusin", focusIn);
      document.removeEventListener("focusout", hide);
      document.removeEventListener("pointerdown", hide, true);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("blur", hide);
    };
  }, []);

  // Keep it on screen.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el || !tip) return;
    const w = el.offsetWidth;
    el.style.left = `${Math.max(8, Math.min(tip.x - w / 2, window.innerWidth - w - 8))}px`;
  }, [tip]);

  if (!tip) return null;
  return createPortal(
    <div
      ref={box}
      role="tooltip"
      className="pointer-events-none fixed z-[70]"
      style={{ top: tip.y, left: tip.x, transform: tip.below ? undefined : "translateY(-100%)" }}
    >
      <div className="tooltip">{tip.text}</div>
    </div>,
    document.body,
  );
}

// ── Confirm dialog ─────────────────────────────────────────────────────

interface ConfirmOptions {
  title: string;
  body: string;
  confirmLabel: string;
  tone?: "danger" | "primary";
}

/** Promise-based replacement for window.confirm, styled like the rest of the app. */
export function useConfirm(): [(o: ConfirmOptions) => Promise<boolean>, ReactNode] {
  const [state, setState] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  const confirm = useCallback((o: ConfirmOptions) => new Promise<boolean>((resolve) => setState({ ...o, resolve })), []);

  const close = useCallback(
    (v: boolean) => {
      state?.resolve(v);
      setState(null);
    },
    [state],
  );

  useEffect(() => {
    if (!state) return;
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, close]);

  const dialog = state && (
    <div
      className="fixed inset-0 z-50 flex animate-fade-in items-center justify-center bg-[var(--scrim)] p-5 backdrop-blur-[3px]"
      onMouseDown={(e) => e.target === e.currentTarget && close(false)}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-body"
        className="w-full max-w-[400px] animate-dialog-in rounded-xl border border-line bg-surface p-6 shadow-lg"
      >
        <div className={`well mb-4 h-10 w-10 rounded-md ${state.tone === "danger" ? "bg-trail-soft text-trail-deep" : "well-forest"}`}>
          <AlertIcon size={20} />
        </div>
        <h2 id="confirm-title" className="text-lead font-semibold tracking-[-0.01em]">
          {state.title}
        </h2>
        <p id="confirm-body" className="mt-1.5 text-label leading-relaxed text-ink-soft">
          {state.body}
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <button ref={cancelRef} className="btn btn-md btn-ghost" onClick={() => close(false)}>
            Cancel
          </button>
          <button className={`btn btn-md ${state.tone === "danger" ? "btn-record" : "btn-primary"}`} onClick={() => close(true)}>
            {state.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );

  return [confirm, dialog];
}

// ── Toast ──────────────────────────────────────────────────────────────

export interface ToastState {
  tone: "error" | "success";
  text: string;
}

export function Toast({ toast, onClose }: { toast: ToastState | null; onClose: () => void }) {
  useEffect(() => {
    if (!toast || toast.tone === "error") return; // errors stay until dismissed
    const t = setTimeout(onClose, 3400);
    return () => clearTimeout(t);
  }, [toast, onClose]);

  if (!toast) return null;
  const error = toast.tone === "error";
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-40 flex justify-center px-5 lg:pl-[calc(288px+1.25rem)]">
      <div
        role={error ? "alert" : "status"}
        className="pointer-events-auto flex max-w-lg animate-enter items-start gap-3 rounded-lg border border-line bg-surface py-3 pl-3 pr-2 text-label shadow-lg"
      >
        <span className={`well h-7 w-7 ${error ? "bg-trail-soft text-trail-deep" : "well-forest"}`}>
          {error ? <AlertIcon size={16} /> : <CheckIcon size={16} />}
        </span>
        <span className="min-w-0 self-center break-words leading-relaxed">{toast.text}</span>
        <button onClick={onClose} className="icon-btn h-7 w-7 shrink-0" aria-label="Dismiss">
          <CloseIcon size={16} />
        </button>
      </div>
    </div>
  );
}
