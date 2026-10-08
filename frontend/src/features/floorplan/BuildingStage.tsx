import { Canvas } from '@react-three/fiber';
import BuildingScene, { type BuildingSceneProps } from './BuildingScene';

/** The building rung's canvas, behind the same lazy boundary as the room's. */
export default function BuildingStage(props: BuildingSceneProps & { label: string }) {
  const { label, ...scene } = props;
  return (
    <Canvas frameloop="demand" dpr={[1, 2]} shadows={false}
            gl={{ antialias: true, powerPreference: 'high-performance' }}
            onPointerMissed={() => scene.onHover(null)} aria-label={label}>
      <BuildingScene {...scene} />
    </Canvas>
  );
}
