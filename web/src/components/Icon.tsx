import type { SVGProps } from 'react';

const paths = {
  back: 'M19 12H5m6-6-6 6 6 6',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  send: 'M12 19V5m-6 6 6-6 6 6',
  close: 'm6 6 12 12M6 18 18 6',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  refresh: 'M20 7v5h-5M4 17v-5h5M6.2 6.2A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.8 5.8',
  file: 'M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5v5h5M8 13h8M8 17h5',
  download: 'M12 3v12m-5-5 5 5 5-5M5 16v4h14v-4',
  external: 'M14 3h7v7m0-7L10 14M10 3H4v17h16v-6',
  chevron: 'm9 5 7 7-7 7',
  plus: 'M12 5v14M5 12h14',
  check: 'm5 12 4 4L19 6',
  terminal: 'm4 6 5 6-5 6m9 0h7',
  search: 'M19 19l-4-4M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0',
  copy: 'M9 9h11v11H9V9M5 15H3V3h12v2',
} as const;

export default function Icon({ name, ...props }: SVGProps<SVGSVGElement> & { name: keyof typeof paths }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" data-ui-icon {...props}><path d={paths[name]} /></svg>;
}
