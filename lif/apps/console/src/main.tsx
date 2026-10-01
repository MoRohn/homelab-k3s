import { render } from 'preact';
import '@/styles/tokens.css';
import '@/styles/base.css';
import '@/ui/ui.css';
import { App } from './app';
import { applyTheme } from '@/shell/theme';
import { initPwa } from './pwa';

applyTheme();
// Before the first render: the browser's install prompt fires early and only once.
initPwa();
render(<App />, document.getElementById('app') as HTMLElement);
