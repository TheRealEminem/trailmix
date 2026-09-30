// Hand-built SVG artwork: the brand mark, the landscape, and the contour-line motif used across the app.
import { useEffect, useId, useRef } from "react";
import type { CSSProperties, ReactNode } from "react";

const reducedMotion = () =>
  typeof window !== "undefined" && (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);

/** Brand mark: a forest tile with a low sun, a winding trail and two elevation lines. */
export function Logo({ size = 36 }: { size?: number }) {
  const id = useId();
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" className="shrink-0">
      <defs>
        <linearGradient id={`${id}-bg`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2F7B58" />
          <stop offset="1" stopColor="#1B4E37" />
        </linearGradient>
        <linearGradient id={`${id}-sun`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#F8CB5A" />
          <stop offset="1" stopColor="#EE8540" />
        </linearGradient>
        <clipPath id={`${id}-clip`}>
          <rect width="64" height="64" rx="16" />
        </clipPath>
      </defs>
      <g clipPath={`url(#${id}-clip)`}>
        <rect width="64" height="64" fill={`url(#${id}-bg)`} />
        <path d="M0 42 C 13 34, 26 34, 38 39 S 56 38, 64 34 V64 H0Z" fill="#fff" fillOpacity="0.07" />
        <g fill="none" stroke="#F4EEDC" strokeLinecap="round" strokeWidth="1.3">
          <path d="M-2 49 C 12 43, 26 44, 38 48 S 56 47, 66 43" strokeOpacity="0.2" />
          <path d="M-2 56 C 14 51, 28 52, 40 55 S 56 54, 66 51" strokeOpacity="0.13" />
        </g>
        <circle cx="43" cy="21" r="7.5" fill={`url(#${id}-sun)`} />
        <path d="M19 66 C 21 56, 33 55, 29.5 47.5 S 35.5 38.5, 45 35.5" fill="none" stroke="#F6EBCF" strokeWidth="4.4" strokeLinecap="round" />
      </g>
      <rect x="0.5" y="0.5" width="63" height="63" rx="15.5" fill="none" stroke="#fff" strokeOpacity="0.16" />
    </svg>
  );
}

// ── Landscape ─────────────────────────────────────────────────────────

const DAY = {
  skyTop: "#D5E5EB",
  skyMid: "#E8EEE8",
  skyLow: "#F6F0E1",
  sunA: "#F8CF66",
  sunB: "#F0A640",
  glow: "#F5B83A",
  mist: "#FFFFFF",
  mistOpacity: 0.5,
  ridgeTop: "#B5CDC1",
  ridgeLow: "#D2E0D2",
  contour: "#FFFFFF",
  contourOpacity: 0.45,
  pineFar: "#8FB29D",
  midTop: "#8DB699",
  midLow: "#A6C7A8",
  pineMid: "#3E7C5D",
  nearTop: "#679B7B",
  nearLow: "#588D6C",
  trail: "#F4E8CB",
  trailEdge: "#D9C79C",
  trailDash: "#C4AF80",
  fore: "#2B6449",
  pineFore: "#23563E",
  hiker: "#E86636",
  hikerRing: "#FFFDF6",
};

const DUSK: typeof DAY = {
  skyTop: "#151D2C",
  skyMid: "#222A3F",
  skyLow: "#443D58",
  sunA: "#F3E9CA",
  sunB: "#E4D6AE",
  glow: "#F1E6C6",
  mist: "#9A93B8",
  mistOpacity: 0.14,
  ridgeTop: "#39485E",
  ridgeLow: "#314156",
  contour: "#C9D3E6",
  contourOpacity: 0.12,
  pineFar: "#2D3E51",
  midTop: "#2A3D4D",
  midLow: "#263846",
  pineMid: "#192C32",
  nearTop: "#1F333B",
  nearLow: "#1B2D34",
  trail: "#8D8370",
  trailEdge: "#6A6252",
  trailDash: "#5A5345",
  fore: "#13242A",
  pineFore: "#0E1D21",
  hiker: "#EE7445",
  hikerRing: "#F1E6C6",
};

/** A slender two-tier pine silhouette standing on (x, y). */
const pine = (x: number, y: number, h: number) => {
  const w = h * 0.36;
  const t = y - h * 0.42;
  return `M${x} ${y - h}L${x + w * 0.62} ${t}L${x + w * 0.34} ${t}L${x + w} ${y}L${x - w} ${y}L${x - w * 0.34} ${t}L${x - w * 0.62} ${t}Z`;
};

const RIDGE_LINE = "M-20 148 C 40 132, 100 118, 170 126 C 235 133, 280 110, 350 102 C 420 94, 465 118, 530 124 C 600 130, 650 104, 715 108 C 765 111, 800 122, 820 124";
const TRAIL = [
  "M404 272 C 420 250, 370 240, 402 224",
  "M402 224 C 434 208, 478 210, 458 196",
  "M458 196 C 438 182, 490 176, 522 166",
];
const TRAIL_FULL = "M404 272 C 420 250, 370 240, 402 224 C 434 208, 478 210, 458 196 C 438 182, 490 176, 522 166";
const STARS = [[60, 34], [132, 22], [206, 62], [262, 30], [338, 56], [418, 22], [470, 70], [660, 40], [712, 88], [760, 30], [108, 92], [300, 88], [540, 30]];

/**
 * The trail, in layers. With `parallax`, the layers drift a few pixels with the pointer (nearer layers more).
 * With `walking`, a hiker sets off up the trail.
 */
export function TrailScene({
  walking = false,
  dusk = false,
  parallax = false,
  className = "",
}: {
  walking?: boolean;
  dusk?: boolean;
  parallax?: boolean;
  className?: string;
}) {
  const id = useId();
  const svg = useRef<SVGSVGElement>(null);
  const P = dusk ? DUSK : DAY;
  const animate = walking && !reducedMotion();

  useEffect(() => {
    if (!parallax || reducedMotion()) return;
    let raf = 0;
    const onMove = (e: PointerEvent) => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const el = svg.current;
        if (!el) return;
        el.style.setProperty("--px", ((e.clientX / window.innerWidth - 0.5) * -2).toFixed(3));
        el.style.setProperty("--py", ((e.clientY / window.innerHeight - 0.5) * -2).toFixed(3));
      });
    };
    window.addEventListener("pointermove", onMove);
    return () => {
      window.removeEventListener("pointermove", onMove);
      cancelAnimationFrame(raf);
    };
  }, [parallax]);

  // Depth factor -> at most ~5px of travel for the foreground.
  const layer = (depth: number): CSSProperties | undefined =>
    parallax
      ? {
          transform: `translate3d(calc(var(--px, 0) * ${(depth * 1.3).toFixed(2)}px), calc(var(--py, 0) * ${(depth * 0.5).toFixed(2)}px), 0)`,
          transition: "transform 1.1s cubic-bezier(0.2, 0.8, 0.2, 1)",
        }
      : undefined;

  return (
    <svg
      ref={svg}
      viewBox="0 0 800 260"
      preserveAspectRatio="xMidYMax slice"
      className={className}
      role="img"
      aria-label={dusk ? "A trail winding into the hills at dusk" : "A trail winding into green hills on a summer day"}
    >
      <defs>
        <linearGradient id={`${id}-sky`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={P.skyTop} />
          <stop offset="0.5" stopColor={P.skyMid} />
          <stop offset="0.85" stopColor={P.skyLow} />
        </linearGradient>
        <radialGradient id={`${id}-glow`}>
          <stop offset="0" stopColor={P.glow} stopOpacity={dusk ? 0.22 : 0.4} />
          <stop offset="1" stopColor={P.glow} stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`${id}-sun`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={P.sunA} />
          <stop offset="1" stopColor={P.sunB} />
        </linearGradient>
        <linearGradient id={`${id}-mist`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={P.mist} stopOpacity="0" />
          <stop offset="0.5" stopColor={P.mist} stopOpacity={P.mistOpacity} />
          <stop offset="1" stopColor={P.mist} stopOpacity="0" />
        </linearGradient>
        <linearGradient id={`${id}-ridge`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0.35" stopColor={P.ridgeTop} />
          <stop offset="0.75" stopColor={P.ridgeLow} />
        </linearGradient>
        <linearGradient id={`${id}-mid`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0.55" stopColor={P.midTop} />
          <stop offset="0.9" stopColor={P.midLow} />
        </linearGradient>
        <linearGradient id={`${id}-near`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0.75" stopColor={P.nearTop} />
          <stop offset="1" stopColor={P.nearLow} />
        </linearGradient>
        <mask id={`${id}-moon`}>
          <circle cx="592" cy="80" r="19" fill="#fff" />
          <circle cx="600" cy="74" r="17" fill="#000" />
        </mask>
        <path id={`${id}-trail`} d={TRAIL_FULL} />
      </defs>

      <rect x="-20" y="-20" width="840" height="300" fill={`url(#${id}-sky)`} />

      {dusk && (
        <g fill="#F1E6C6" style={layer(0.2)}>
          {STARS.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={i % 3 ? 0.9 : 1.4} opacity={i % 2 ? 0.55 : 0.8} />
          ))}
        </g>
      )}

      {/* sun (or moon), ringed like an elevation map */}
      <g style={layer(0.5)}>
        <circle cx="592" cy="84" r="130" fill={`url(#${id}-glow)`} />
        <g fill="none" stroke={P.glow} strokeWidth="1">
          <circle cx="592" cy="84" r="38" opacity={dusk ? 0.12 : 0.24} />
          <circle cx="592" cy="84" r="54" opacity={dusk ? 0.07 : 0.13} />
        </g>
        {dusk ? (
          <circle cx="592" cy="80" r="19" fill={`url(#${id}-sun)`} mask={`url(#${id}-moon)`} />
        ) : (
          <circle cx="592" cy="84" r="24" fill={`url(#${id}-sun)`} />
        )}
      </g>

      <rect x="-20" y="104" width="840" height="76" fill={`url(#${id}-mist)`} style={layer(0.6)} />

      {/* far ridge with its contour lines */}
      <g style={layer(1)}>
        <path d={`${RIDGE_LINE} V280 H-20Z`} fill={`url(#${id}-ridge)`} />
        <g fill="none" stroke={P.contour} strokeWidth="1" strokeOpacity={P.contourOpacity}>
          <path d={RIDGE_LINE} transform="translate(0 11)" />
          <path d={RIDGE_LINE} transform="translate(0 22)" opacity="0.7" />
          <path d={RIDGE_LINE} transform="translate(0 33)" opacity="0.45" />
        </g>
        <g fill={P.pineFar}>
          {[[338, 105, 15], [350, 107, 19], [362, 106, 13], [690, 111, 15], [702, 113, 12]].map(([x, y, h], i) => (
            <path key={i} d={pine(x, y, h)} />
          ))}
        </g>
      </g>

      {/* middle hills */}
      <g style={layer(1.8)}>
        <path
          d="M-20 184 C 70 160, 150 152, 245 166 C 325 178, 395 150, 485 146 C 575 142, 635 164, 715 160 C 765 157, 800 150, 820 151 V280 H-20Z"
          fill={`url(#${id}-mid)`}
        />
        <g fill={P.pineMid} stroke={P.pineMid} strokeWidth="1.2" strokeLinejoin="round">
          {[[84, 170, 34], [100, 172, 26], [114, 170, 30], [600, 158, 32], [616, 160, 24], [476, 150, 16]].map(([x, y, h], i) => (
            <path key={i} d={pine(x, y, h)} />
          ))}
        </g>
      </g>

      {/* near hills */}
      <g style={layer(2.6)}>
        <path
          d="M-20 214 C 85 198, 190 202, 300 209 C 400 216, 470 197, 560 195 C 650 193, 720 204, 820 199 V280 H-20Z"
          fill={`url(#${id}-near)`}
        />
      </g>

      {/* the trail narrows as it climbs away */}
      <g style={layer(2.3)} fill="none" strokeLinecap="round">
        {TRAIL.map((d, i) => (
          <path key={`e${i}`} d={d} stroke={P.trailEdge} strokeWidth={[13, 10, 7][i]} strokeOpacity="0.6" />
        ))}
        {TRAIL.map((d, i) => (
          <path key={`t${i}`} d={d} stroke={P.trail} strokeWidth={[10, 7.5, 5][i]} />
        ))}
        <use
          href={`#${id}-trail`}
          stroke={P.trailDash}
          strokeWidth="1.4"
          strokeDasharray="2 10"
          className={animate ? "animate-trail-walk" : ""}
        />
        {animate && (
          <g>
            <circle r="11" fill={P.hiker} opacity="0.18">
              <animateMotion dur="16s" repeatCount="indefinite">
                <mpath href={`#${id}-trail`} />
              </animateMotion>
              <animate attributeName="r" values="8;13;8" dur="2s" repeatCount="indefinite" />
            </circle>
            <circle r="4.5" fill={P.hiker} stroke={P.hikerRing} strokeWidth="2">
              <animateMotion dur="16s" repeatCount="indefinite">
                <mpath href={`#${id}-trail`} />
              </animateMotion>
            </circle>
          </g>
        )}
      </g>

      {/* foreground framing */}
      <g style={layer(4)}>
        <path d="M-20 262 V 222 C 30 216, 88 226, 150 262 Z" fill={P.fore} />
        <path d="M820 262 V 214 C 760 212, 700 230, 650 262 Z" fill={P.fore} />
        <g fill={P.pineFore} stroke={P.pineFore} strokeWidth="1.2" strokeLinejoin="round">
          {[[30, 232, 56], [56, 238, 40], [752, 226, 60], [776, 232, 44]].map(([x, y, h], i) => (
            <path key={i} d={pine(x, y, h)} />
          ))}
        </g>
      </g>
    </svg>
  );
}

// ── Contour lines ─────────────────────────────────────────────────────

/** Nested, gently wobbling rings around a peak, like a trail map. */
function rings(cx: number, cy: number, n: number, base: number, step: number, sx: number, sy: number, seed: number) {
  return Array.from({ length: n }, (_, ring) => {
    const r0 = base + ring * step;
    let d = "";
    for (let k = 0; k <= 120; k++) {
      const t = (k / 120) * Math.PI * 2;
      const wob =
        1 +
        0.075 * Math.sin(3 * t + seed + ring * 0.16) +
        0.045 * Math.cos(5 * t - seed * 1.7 + ring * 0.11) +
        0.022 * Math.sin(8 * t + ring * 0.25);
      d += `${k ? "L" : "M"}${(cx + Math.cos(t) * r0 * wob * sx).toFixed(1)} ${(cy + Math.sin(t) * r0 * wob * sy).toFixed(1)}`;
    }
    return { d: d + "Z", index: ring % 5 === 4 };
  });
}

const PEAKS = [...rings(1400, 110, 17, 34, 34, 1.35, 1, 0.4), ...rings(220, 940, 13, 44, 38, 1.25, 0.9, 2.1)];

/** The app's atmosphere: a soft environmental gradient, two faint glows and watermark-faint contour lines. */
export function Ambient() {
  return (
    <div aria-hidden="true" className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
      <div className="absolute inset-0" style={{ background: "linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 42%)" }} />
      <div className="absolute inset-0" style={{ background: "radial-gradient(55% 50% at 64% 62%, var(--glow-green), transparent 70%)" }} />
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(34% 26% at 60% 4%, var(--glow-warm), transparent 72%), radial-gradient(26% 22% at 82% 12%, var(--glow-orange), transparent 70%)",
        }}
      />
      <svg
        viewBox="0 0 1600 1000"
        preserveAspectRatio="xMidYMid slice"
        className="absolute inset-0 h-full w-full"
        style={{
          WebkitMaskImage: "radial-gradient(90% 90% at 70% 30%, #000 30%, transparent 85%)",
          maskImage: "radial-gradient(90% 90% at 70% 30%, #000 30%, transparent 85%)",
        }}
      >
        <g fill="none" stroke="var(--contour)">
          {PEAKS.map((p, i) => (
            <path key={i} d={p.d} strokeWidth={p.index ? 1.6 : 1} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
      </svg>
    </div>
  );
}

const BADGE_RINGS = rings(32, 32, 3, 17, 6.5, 1, 1, 1.2);

/** Empty-state mark: an icon at the summit of a few contour rings. */
export function ContourBadge({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`relative mx-auto flex h-20 w-20 items-center justify-center ${className}`}>
      <svg viewBox="0 0 64 64" className="absolute inset-0 h-full w-full text-forest" aria-hidden="true">
        <g fill="none" stroke="currentColor" strokeWidth="1">
          {BADGE_RINGS.map((r, i) => (
            <path key={i} d={r.d} opacity={[0.22, 0.14, 0.08][i]} vectorEffect="non-scaling-stroke" />
          ))}
        </g>
      </svg>
      <span className="well well-forest relative h-10 w-10 rounded-md">{children}</span>
    </div>
  );
}
