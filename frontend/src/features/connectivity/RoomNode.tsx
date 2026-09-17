import { memo } from 'react';
import { useStore } from '@xyflow/react';

/** A room, drawn behind the devices in it.
 *
 *  Only on the site view. A whole-site graph is eight rooms of chains, and
 *  without the rectangles the reader has to infer where the Network Room ends
 *  and the UPS Room begins from the names on 155 boxes. The device plane draws
 *  the same rectangles for the same reason.
 *
 *  Not selectable and not draggable: it is a label for a region, not a thing
 *  in the estate, and dragging it would say otherwise.
 */
function RoomNode({ data }: { data: { name: string; count: number } }) {
  // The canvas scales everything it draws, and a whole site fits at about
  // 15%: a 1px border lands on a sixth of a pixel and an 11px label on under
  // two. Both are divided back out, so the rectangle and its name stay the
  // same size on screen whatever the zoom - which is the point of them, since
  // at that zoom they are the only thing still readable.
  const zoom = useStore((s) => s.transform[2]);
  const k = 1 / Math.max(zoom, 0.02);

  return (
    <div className="cn-room" style={{ borderWidth: Math.min(k, 8) }}>
      <span className="cn-room-name"
            style={{ fontSize: `${Math.min(11 * k, 130)}px`,
                     top: 6 * k, left: 12 * k, gap: 6 * k }}>
        {data.name}
        <span className="cn-room-count"
              style={{ padding: `0 ${5 * k}px`, borderRadius: 8 * k }}>
          {data.count}
        </span>
      </span>
    </div>
  );
}

export default memo(RoomNode);
