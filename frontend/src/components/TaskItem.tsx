import { CheckIcon } from "./icons";

/** "Dana: fix the login bug" -> owner "Dana", rest "fix the login bug". */
function splitOwner(text: string): [string | null, string] {
  const m = text.match(/^([^:]{1,32}):\s*(.+)$/);
  return m && m[1].split(/\s+/).length <= 4 ? [m[1], m[2]] : [null, text];
}

export default function TaskItem({ text, done, onToggle }: { text: string; done: boolean; onToggle: (done: boolean) => void }) {
  const [owner, rest] = splitOwner(text);
  return (
    <label className="group flex cursor-pointer items-start gap-3 rounded-md px-2 py-2 transition-colors duration-150 hover:bg-forest/[.04]">
      <button
        type="button"
        role="checkbox"
        aria-checked={done}
        onClick={() => onToggle(!done)}
        className={`mt-[3px] flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-xs border-[1.5px] transition-[background-color,border-color] duration-200 ${
          done ? "border-forest bg-forest text-on-accent" : "border-line-strong bg-surface group-hover:border-forest/60"
        }`}
      >
        {done && <CheckIcon size={12} strokeWidth={3} className="animate-check-pop" />}
      </button>
      <span className={`text-body leading-relaxed transition-colors duration-200 ${done ? "text-ink-soft line-through decoration-ink-faint/70" : "text-ink"}`}>
        {owner && <span className="mr-1.5 font-semibold">{owner}</span>}
        {rest}
      </span>
    </label>
  );
}
