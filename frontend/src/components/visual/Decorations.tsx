import { useId } from "react";

export type DecorationProps = {
  className?: string;
};

export function OrbitalLines({ className = "" }: DecorationProps) {
  return (
    <svg
      viewBox="0 0 900 520"
      fill="none"
      aria-hidden="true"
      className={`pointer-events-none select-none ${className}`}
    >
      <g stroke="#B9794D" strokeWidth="1.4" opacity=".34">
        <path d="M-70 410C130 30 350-90 560 40s290 270 430 310" />
        <path d="M-90 450C130 80 365-20 575 95s280 260 425 330" />
        <path d="M-120 490C100 150 360 45 590 150s280 245 430 340" />
        <path d="M20 -80c90 180 190 340 350 600M140-90c80 190 160 360 280 610M270-80c60 200 110 390 190 620M410-60c30 210 60 420 100 620" />
        <path d="M930-70C730 120 620 270 500 540M820-80C680 120 590 310 460 560M700-80C610 130 520 330 420 560" />
      </g>
      <g fill="#C27A45" opacity=".8">
        <circle cx="700" cy="105" r="4" />
        <circle cx="180" cy="330" r="3" />
        <circle cx="490" cy="220" r="2.5" />
      </g>
      <g stroke="#C27A45" strokeWidth="2" opacity=".7">
        <path d="M700 82v46m-23-23h46M182 309v42m-21-21h42" />
      </g>
    </svg>
  );
}

export function PlanetOrbit({ className = "" }: DecorationProps) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <svg
      viewBox="0 0 640 480"
      fill="none"
      aria-hidden="true"
      className={`pointer-events-none select-none ${className}`}
    >
      <defs>
        <radialGradient id={`planet${uid}`} cx="34%" cy="25%" r="78%">
          <stop offset="0" stopColor="#F8D9B6" />
          <stop offset=".42" stopColor="#D99A65" />
          <stop offset=".78" stopColor="#A95D2D" />
          <stop offset="1" stopColor="#713B20" />
        </radialGradient>
        <linearGradient id={`ring${uid}`} x1="42" y1="210" x2="590" y2="350" gradientUnits="userSpaceOnUse">
          <stop stopColor="#E9B88D" stopOpacity=".15" />
          <stop offset=".48" stopColor="#995329" stopOpacity=".9" />
          <stop offset="1" stopColor="#F3C9A5" stopOpacity=".22" />
        </linearGradient>
        <filter id={`shadow${uid}`} x="100" y="70" width="450" height="390" colorInterpolationFilters="sRGB">
          <feGaussianBlur stdDeviation="18" />
        </filter>
        <clipPath id={`disc${uid}`}>
          <circle cx="326" cy="222" r="124" />
        </clipPath>
      </defs>
      <ellipse cx="328" cy="370" rx="178" ry="30" fill="#8A4A27" opacity=".16" filter={`url(#shadow${uid})`} />
      <ellipse cx="326" cy="222" rx="124" ry="124" fill={`url(#planet${uid})`} />
      <g clipPath={`url(#disc${uid})`} opacity=".24" stroke="#713B20" strokeWidth="2">
        <path d="M180 170c70 35 186 38 293-3M176 210c80 28 193 31 299-7M190 260c74 18 177 20 268-10M220 310c65 7 132 3 208-20" />
        <path d="M245 95c-40 70-44 182 8 260M315 92c-20 85-16 178 16 260M390 115c-5 73 12 142 48 205" />
      </g>
      <ellipse
        cx="326"
        cy="222"
        rx="278"
        ry="84"
        transform="rotate(-17 326 222)"
        stroke={`url(#ring${uid})`}
        strokeWidth="14"
      />
      <path d="M63 304c74-89 164-134 263-134 100 0 181 31 249 93" stroke="#F7D8BC" strokeWidth="2" opacity=".65" />
      <path d="M78 329c100-65 194-91 285-80 75 9 135 33 191 70" stroke="#995329" strokeWidth="2" opacity=".5" />
      <circle cx="112" cy="151" r="4" fill="#C27A45" />
      <circle cx="535" cy="118" r="3" fill="#C27A45" />
      <path
        d="M505 64v22m-11-11h22M145 70v16m-8-8h16"
        stroke="#B9794D"
        strokeWidth="2"
        strokeLinecap="round"
        opacity=".8"
      />
    </svg>
  );
}

export function FloatingCube({ className = "", animated = false }: DecorationProps & { animated?: boolean }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <svg
      viewBox="0 0 480 480"
      fill="none"
      aria-hidden="true"
      className={`pointer-events-none select-none ${animated ? "animate-float-soft" : ""} ${className}`}
    >
      <defs>
        <linearGradient id={`top${uid}`} x1="150" y1="90" x2="320" y2="230" gradientUnits="userSpaceOnUse">
          <stop stopColor="#F9E5D2" />
          <stop offset="1" stopColor="#D99A65" />
        </linearGradient>
        <linearGradient id={`left${uid}`} x1="110" y1="180" x2="240" y2="390" gradientUnits="userSpaceOnUse">
          <stop stopColor="#D99A65" />
          <stop offset="1" stopColor="#995329" />
        </linearGradient>
        <linearGradient id={`right${uid}`} x1="240" y1="180" x2="370" y2="380" gradientUnits="userSpaceOnUse">
          <stop stopColor="#B87545" />
          <stop offset="1" stopColor="#713B20" />
        </linearGradient>
        <filter id={`blur${uid}`} x="45" y="330" width="390" height="110" filterUnits="userSpaceOnUse">
          <feGaussianBlur stdDeviation="17" />
        </filter>
      </defs>
      <ellipse cx="240" cy="382" rx="138" ry="24" fill="#713B20" opacity=".2" filter={`url(#blur${uid})`} />
      <g stroke="#F9E7D7" strokeOpacity=".72" strokeWidth="2" strokeLinejoin="round">
        <path d="m240 66 139 78-139 80-139-80 139-78Z" fill={`url(#top${uid})`} />
        <path d="M101 144v157l139 80V224L101 144Z" fill={`url(#left${uid})`} />
        <path d="M379 144v157l-139 80V224l139-80Z" fill={`url(#right${uid})`} />
      </g>
      <path d="m240 66 0 158m139-80-139 80-139-80" stroke="#FFF6ED" strokeOpacity=".8" strokeWidth="2" />
      <path d="m146 145 94-54 94 54-94 54-94-54Z" stroke="#FFF6ED" strokeOpacity=".4" />
      <path d="m240 224 0 157" stroke="#F8DCC4" strokeOpacity=".42" />
      <circle cx="385" cy="89" r="5" fill="#C27A45" />
      <path d="M385 67v44m-22-22h44" stroke="#C27A45" strokeWidth="2" opacity=".7" />
    </svg>
  );
}

export function SparkleSet({ className = "" }: DecorationProps) {
  return (
    <svg
      viewBox="0 0 360 240"
      fill="none"
      aria-hidden="true"
      className={`pointer-events-none select-none ${className}`}
    >
      <g fill="#C27A45">
        <path d="M72 10c8 34 18 44 52 52-34 8-44 18-52 52-8-34-18-44-52-52 34-8 44-18 52-52Z" />
        <path d="M254 28c5 22 12 29 34 34-22 5-29 12-34 34-5-22-12-29-34-34 22-5 29-12 34-34Z" opacity=".72" />
        <circle cx="160" cy="166" r="5" opacity=".75" />
        <circle cx="310" cy="190" r="3" />
      </g>
      <path
        d="M160 130v72m-36-36h72M310 164v52m-26-26h52"
        stroke="#B9794D"
        strokeWidth="2"
        opacity=".6"
      />
    </svg>
  );
}

export function WarmWave({ className = "" }: DecorationProps) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <svg
      viewBox="0 0 1440 520"
      fill="none"
      aria-hidden="true"
      preserveAspectRatio="none"
      className={`pointer-events-none select-none ${className}`}
    >
      <defs>
        <linearGradient id={`waveA${uid}`} x1="150" y1="80" x2="1220" y2="460" gradientUnits="userSpaceOnUse">
          <stop stopColor="#F8E4D2" />
          <stop offset=".52" stopColor="#E9B88D" />
          <stop offset="1" stopColor="#995329" />
        </linearGradient>
        <linearGradient id={`waveB${uid}`} x1="80" y1="120" x2="1320" y2="490" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FFF6ED" />
          <stop offset=".6" stopColor="#D99A65" />
          <stop offset="1" stopColor="#713B20" />
        </linearGradient>
      </defs>
      <path
        d="M0 345c180-130 310-160 470-85s245 125 410 65 330-180 560-95v290H0V345Z"
        fill={`url(#waveA${uid})`}
        opacity=".65"
      />
      <path
        d="M0 405c205-110 350-90 510-25s270 80 440 5 330-135 490-70v205H0V405Z"
        fill={`url(#waveB${uid})`}
        opacity=".72"
      />
      <path
        d="M0 345c180-130 310-160 470-85s245 125 410 65 330-180 560-95"
        stroke="#FFF7EF"
        strokeWidth="3"
        opacity=".75"
      />
      <path
        d="M0 405c205-110 350-90 510-25s270 80 440 5 330-135 490-70"
        stroke="#B9794D"
        strokeWidth="2"
        opacity=".5"
      />
    </svg>
  );
}

export function GlassPanelFrame({ className = "" }: DecorationProps) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <svg
      viewBox="0 0 800 500"
      fill="none"
      aria-hidden="true"
      preserveAspectRatio="none"
      className={`pointer-events-none select-none ${className}`}
    >
      <defs>
        <linearGradient id={`stroke${uid}`} x1="40" y1="20" x2="760" y2="480" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FFFFFF" stopOpacity=".95" />
          <stop offset=".48" stopColor="#D99A65" stopOpacity=".72" />
          <stop offset="1" stopColor="#995329" stopOpacity=".45" />
        </linearGradient>
        <linearGradient id={`wash${uid}`} x1="0" y1="0" x2="800" y2="500" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FFFDF9" stopOpacity=".72" />
          <stop offset="1" stopColor="#E7C4A7" stopOpacity=".18" />
        </linearGradient>
      </defs>
      <rect x="12" y="12" width="776" height="476" rx="34" fill={`url(#wash${uid})`} stroke={`url(#stroke${uid})`} strokeWidth="2" />
      <path d="M42 14h230M530 486h220" stroke="#FFF" strokeWidth="3" strokeLinecap="round" opacity=".8" />
      <path d="M14 86v-34q0-40 40-40h54M786 414v34q0 40-40 40h-54" stroke="#B9794D" strokeWidth="2" opacity=".38" />
      <path d="M45 46h110" stroke="#995329" strokeWidth="1" opacity=".2" />
    </svg>
  );
}

export function TerminalDecoration({ className = "" }: DecorationProps) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <svg
      viewBox="0 0 760 420"
      fill="none"
      aria-hidden="true"
      className={`pointer-events-none select-none ${className}`}
    >
      <defs>
        <linearGradient id={`bg${uid}`} x1="0" y1="0" x2="760" y2="420" gradientUnits="userSpaceOnUse">
          <stop stopColor="#30251F" />
          <stop offset="1" stopColor="#120F0D" />
        </linearGradient>
        <linearGradient id={`line${uid}`} x1="30" y1="30" x2="730" y2="390" gradientUnits="userSpaceOnUse">
          <stop stopColor="#D99A65" stopOpacity=".8" />
          <stop offset="1" stopColor="#995329" stopOpacity=".08" />
        </linearGradient>
      </defs>
      <rect x="8" y="8" width="744" height="404" rx="24" fill={`url(#bg${uid})`} stroke={`url(#line${uid})`} strokeWidth="2" />
      <path d="M9 72h742" stroke="#D99A65" strokeOpacity=".22" />
      <circle cx="38" cy="40" r="8" fill="#D98787" />
      <circle cx="66" cy="40" r="8" fill="#D9B879" />
      <circle cx="94" cy="40" r="8" fill="#86C99B" />
      <g stroke="#B9794D" strokeOpacity=".22" strokeWidth="1">
        <path d="M500 72 740 312M560 72 740 252M620 72 740 192" />
        <path d="M740 100 530 310M740 160 590 310" />
      </g>
      <path
        d="m62 170 22 20-22 20M112 210h78"
        stroke="#D99A65"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M62 270h260M62 296h190M62 322h310" stroke="#F3D7BF" strokeOpacity=".22" strokeWidth="3" strokeLinecap="round" />
      <circle cx="676" cy="340" r="34" stroke="#D99A65" strokeOpacity=".45" strokeWidth="2" />
      <path d="M676 320v40m-20-20h40" stroke="#D99A65" strokeWidth="2" opacity=".6" />
    </svg>
  );
}

export function OrbitalBackdrop({ className = "" }: DecorationProps) {
  return (
    <div className={`pointer-events-none absolute inset-0 overflow-hidden ${className}`} aria-hidden="true">
      <OrbitalLines className="absolute left-1/2 top-1/2 h-auto w-[130%] min-w-[720px] max-w-[1100px] -translate-x-1/2 -translate-y-1/2 opacity-[0.14]" />
    </div>
  );
}
