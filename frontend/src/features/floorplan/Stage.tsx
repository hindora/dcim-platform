import { Canvas } from '@react-three/fiber';
import RoomScene, { type SceneProps } from './Scene';

/** The WebGL canvas and the scene inside it, behind one lazy import so
 *  three.js, react-three-fiber and drei load only when the viewer opens. */
export default function Stage(props: SceneProps & { label: string }) {
  const { label, ...scene } = props;
  return (
    <Canvas frameloop={scene.mode === 'fpv' ? 'always' : 'demand'} dpr={[1, 2]} shadows={false}
            gl={{ antialias: true, powerPreference: 'high-performance' }}
            onPointerMissed={() => { if (scene.mode !== 'fpv') scene.onSelect(null); }}
            aria-label={label}>
      <RoomScene {...scene} />
    </Canvas>
  );
}
