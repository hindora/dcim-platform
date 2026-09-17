import { memo } from 'react';

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
  return (
    <div className="cn-room">
      <span className="cn-room-name">
        {data.name}
        <span className="cn-room-count">{data.count}</span>
      </span>
    </div>
  );
}

export default memo(RoomNode);
