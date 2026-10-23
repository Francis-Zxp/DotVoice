/* Keeps a rendering bug from leaving an empty (black) window: the error is
   logged to the core, and the subtree remounts shortly after. */
import { Component, type ReactNode } from 'react';
import { call } from '../lib/bridge';

interface Props { name: string; children: ReactNode; fallback?: ReactNode }
interface State { error: Error | null; key: number }

export class Boundary extends Component<Props, State> {
  state: State = { error: null, key: 0 };
  private timer = 0;
  private failures: number[] = [];
  private lastMessage = '';

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    const now = Date.now();
    this.failures = this.failures.filter((t) => now - t < 10000).concat(now);
    // The same error again is not logged again: ui-errors.log keeps only its tail.
    if (error.message !== this.lastMessage) {
      const detail = `${error.message}\n${(info.componentStack ?? '').split('\n').slice(0, 6).join('\n')}`;
      call('log_ui_error', { where: this.props.name, detail }).catch(() => {});
    }
    this.lastMessage = error.message;
    window.clearTimeout(this.timer);
    // A persistent failure stays on the fallback; switching pages remounts it.
    if (this.failures.length > 3) return;
    this.timer = window.setTimeout(() => this.setState((s) => ({ error: null, key: s.key + 1 })), 600);
  }

  componentWillUnmount() {
    window.clearTimeout(this.timer);
  }

  render() {
    if (this.state.error) return this.props.fallback ?? null;
    return <div key={this.state.key} style={{ display: 'contents' }}>{this.props.children}</div>;
  }
}
