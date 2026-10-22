/**
 * The three navigation cards down the right of the home page: the three ways
 * of looking at the estate, which is what the VISUALIZATION menu offers too.
 *
 * Each card carries a photograph of the page it opens - a crop of the real
 * network map, floor map and world map, taken from this product with its own
 * data - so the card shows what the reader will get, not an illustration of
 * it. One crop per palette: a dark screenshot on a light page reads as a
 * broken image, so the light theme gets the same view shot in light.
 *
 * The pictures are backgrounds rather than <img>s on purpose. A browser
 * fetches a hidden <img>, so two images per card would cost both downloads
 * on every visit; a background whose rule does not apply is never requested.
 * They are decorative - the heading says what the card is - so nothing is
 * lost to a screen reader.
 */

import type { CSSProperties } from 'react';
import { Link } from 'react-router-dom';

import networkTree from '../../assets/home/network-map-tree.jpg';
import floorDark from '../../assets/home/floor-map-dark.jpg';
import floorLight from '../../assets/home/floor-map-light.jpg';
import worldDark from '../../assets/home/world-map-dark.jpg';
import worldLight from '../../assets/home/world-map-light.jpg';

const CARDS = [
  {
    // The power chain from the utility down to the RPPs, shown whole: the tree is
    // the point, and cropping it to the card's band would cut off its top and its
    // leaves. One image for both palettes - it is the canvas's own dark studio.
    to: '/connectivity', title: 'Network map', dark: networkTree, light: networkTree, whole: true,
    body: 'Every device and the links between them, room by room, with the power, cooling, network and management paths as layers.',
  },
  {
    to: '/floorplan', title: 'Floor map', dark: floorDark, light: floorLight, lift: true,
    body: 'The site in 3D, from the compound through the building to a rack and the device in its U - live, or at any moment in the past.',
  },
  {
    to: '/world', title: 'World map', dark: worldDark, light: worldLight,
    body: 'Every site on the map with its worst open alarm and its outdoor air, and the weather and hazard warnings around it.',
  },
];

export function RailCards() {
  return (
    <>
      {CARDS.map(({ to, title, body, dark, light, lift, whole }) => (
        <Link key={title} className="rail-card" to={to}>
          <div className={`visual shot${lift ? ' lift' : ''}${whole ? ' whole' : ''}`} aria-hidden
               style={{ '--shot-dark': `url(${dark})`, '--shot-light': `url(${light})` } as CSSProperties} />
          <div className="body">
            <h3>{title}</h3>
            <p>{body}</p>
            <span className="go" aria-hidden>→</span>
          </div>
        </Link>
      ))}
    </>
  );
}
