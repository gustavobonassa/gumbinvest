/** Last-resort catch for render-time errors: one broken page or chart must
    not white-screen the whole SPA. Query/network errors are handled closer to
    the data (ErrorState); this only catches exceptions thrown while rendering. */
import { Component, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto max-w-lg py-16 text-center">
          <p className="text-lg font-semibold text-ink">Algo deu errado ao exibir esta página.</p>
          <p className="mt-2 break-words text-sm text-ink-muted">{String(this.state.error)}</p>
          <button type="button" className="btn-ghost mt-6" onClick={() => this.setState({ error: null })}>
            Tentar novamente
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
