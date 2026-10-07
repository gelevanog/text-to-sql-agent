import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement>;
const base = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" } as const;

export const LogoMark = (p: P) => (
  <svg viewBox="0 0 32 32" {...p}>
    <path d="M9 8v16M14 8v16M19 8v16M24 8v16" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
    <path d="M6 21L27 11" stroke="#d9480f" strokeWidth="2.6" strokeLinecap="round" />
  </svg>
);
export const AskIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M4 5h16v11H8l-4 4z" /><path d="M9 10h6" /></svg>
);
export const SchemaIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></svg>
);
export const EvalIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></svg>
);
export const AuditIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M6 3h9l4 4v14H6z" /><path d="M9 11h7M9 15h7M9 7h3" /></svg>
);
export const ShieldIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M12 3l8 3v6c0 4.5-3.3 8-8 9-4.7-1-8-4.5-8-9V6z" /><path d="M9 12l2 2 4-4" /></svg>
);
export const BlockIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><circle cx="12" cy="12" r="9" /><path d="M5.6 5.6l12.8 12.8" /></svg>
);
export const QuestionIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14" /><path d="M12 17.5h.01" /></svg>
);
export const CheckIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
);
export const SendIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M5 12h13M13 6l6 6-6 6" /></svg>
);
export const StarIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M12 4l2.4 5 5.6.6-4.2 3.8 1.2 5.6L12 16.2 7 19l1.2-5.6L4 9.6 9.6 9z" /></svg>
);
export const SparkIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6" /></svg>
);
export const KeyIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M17 6l3 3" /></svg>
);
export const LockIcon = (p: P) => (
  <svg viewBox="0 0 24 24" {...base} {...p}><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>
);
