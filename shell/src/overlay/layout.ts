import type { OverlayAnchor } from '../lib/types';

export const ANCHORS: { value: OverlayAnchor; label: string; x: number; y: number }[] = [
  { value: 'top-left', label: '左上角', x: 0, y: 0 },
  { value: 'top-center', label: '顶部居中', x: .5, y: 0 },
  { value: 'top-right', label: '右上角', x: 1, y: 0 },
  { value: 'center-left', label: '左侧居中', x: 0, y: .5 },
  { value: 'center', label: '屏幕中央', x: .5, y: .5 },
  { value: 'center-right', label: '右侧居中', x: 1, y: .5 },
  { value: 'bottom-left', label: '左下角', x: 0, y: 1 },
  { value: 'bottom-center', label: '底部居中', x: .5, y: 1 },
  { value: 'bottom-right', label: '右下角', x: 1, y: 1 },
];

export function alignment(anchor: OverlayAnchor) {
  return ANCHORS.find(item => item.value === anchor) ?? ANCHORS[1];
}

/** Card and copied desktop pixels share the same untransformed CSS coordinates. */
export function cardPosition(anchor: OverlayAnchor, width: number, height: number, viewportWidth: number, viewportHeight: number) {
  const { x, y } = alignment(anchor);
  return { x: 12 + Math.max(0, viewportWidth - 24 - width) * x, y: 12 + Math.max(0, viewportHeight - 24 - height) * y };
}
