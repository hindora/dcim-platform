import { memo } from 'react';
import { NodeResizer, useStore } from '@xyflow/react';

/** A room, drawn behind the devices in it.
 *
 *  Only on the site view. A whole-site graph is eight rooms of chains, and
 *  without the rectangles the reader has to infer where the Network Room ends
 *  and the UPS Room begins from the names on 155 boxes. The device plane draws
 *  the same rectangles for the same reason.
 *
 *  The rectangle takes no pointer events - it lies over a region of canvas the
 *  operator still has to pan across. The LABEL is the handle: click it to pick
 *  the room up, or to select it and get the resize grips.
 */
function RoomNode({ data, selected, width }: {
  data: { name: string; count: number; width: number };
  selected?: boolean;
  width?: number;
}) {
  // The canvas scales everything it draws, and a whole site fits at about
  // 15%: a 1px border lands on a sixth of a pixel and an 11px label on under
  // two. Both are divided back out, so the rectangle and its name stay the
  // same size on screen whatever the zoom - which is the point of them, since
  // at that zoom they are the only thing still readable.
  const zoom = useStore((s) => s.transform[2]);
  const k = 1 / Math.max(zoom, 0.02);

  // ...but only up to the room's own width, which is the LIVE width - a room
  // that has been resized names itself at the size it is now, not the size the
  // layout gave it. Three narrow rooms sit side by side in a band, and a label
  // that keeps growing while the box it names shrinks ends up written across
  // its neighbours: UPS ROOM, GENERATOR ROOM and MECHANICAL ROOM were one
  // illegible line.
  const chars = data.name.length + 3;          // the count chip, roughly
  const w = width ?? data.width ?? 0;
  const fit = (w * 0.95) / (chars * 0.62);
  const size = Math.max(6, Math.min(11 * k, fit || 11 * k, 130));

  return (
    <div className={selected ? 'cn-room is-on' : 'cn-room'}
         style={{ borderWidth: Math.min(k, 8) }}>
      {/* Grips only while the room is picked: eight rooms all wearing eight
          handles is a mesh of dots over the drawing. NOT scaled against the
          zoom - the library already keeps its handles a constant size on
          screen, and multiplying that by 1/zoom at 15% drew four blue slabs
          the size of a rack row. */}
      <NodeResizer isVisible={Boolean(selected)} minWidth={80} minHeight={60}
                   lineClassName="cn-room-line" handleClassName="cn-room-grip" />

      {/* Above the rectangle, not inside it: inside, the label is written
          over the first row of devices at any zoom where it is readable. */}
      <span className="cn-room-name"
            style={{ fontSize: `${size}px`, bottom: '100%',
                     marginBottom: size * 0.3, left: 2 * k, gap: size * 0.5 }}>
        {data.name}
        <span className="cn-room-count"
              style={{ padding: `0 ${size * 0.4}px`, borderRadius: size }}>
          {data.count}
        </span>
      </span>
    </div>
  );
}

export default memo(RoomNode);
