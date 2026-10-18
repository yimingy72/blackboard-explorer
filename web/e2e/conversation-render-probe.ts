import { createElement, forwardRef, type ComponentProps, type ComponentRef } from 'react';
import { Bubble } from '@ant-design/x';

let renders = 0;

/** Test-only wrapper: count actual list renders while preserving its DOM and ref. */
export function installBubbleRenderProbe() {
  const Original = Bubble.List;
  const Tracked = forwardRef<ComponentRef<typeof Original>, ComponentProps<typeof Original>>((props, ref) => {
    renders += 1;
    return createElement(Original, { ...props, ref });
  });
  Tracked.displayName = 'BubbleRenderProbe';
  Bubble.List = Tracked as typeof Original;
}

export function bubbleRenderCount() { return renders; }
