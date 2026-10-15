import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { isMock } from './lib/bridge';
import { Overlay } from './overlay/Overlay';
import './styles.css';

document.documentElement.classList.toggle('preview', isMock);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Overlay />
  </StrictMode>,
);
